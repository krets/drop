import os
import sqlite3
from datetime import datetime, timedelta, timezone

import config
from services import gdrive
from services.retention import select_keep
from tests.conftest import ALIGN, new_device


def make_share(admin, drive, **kw):
    folder = drive.add_folder("Backups")
    r = admin.post("/admin/api/backups", json={"name": "nightly", "gdrive_folder_id": folder, **kw})
    assert r.status_code == 201, r.text
    return r.json(), folder


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def put(client, token, name, data=b"payload", **kw):
    return client.put(f"/api/backup/{name}", content=data, headers=auth(token), **kw)


def sql(query, *args):
    db = sqlite3.connect(config.DB_PATH)
    try:
        out = db.execute(query, args).fetchall()
        db.commit()
        return out
    finally:
        db.close()


def backdate(days_ago_by_name: dict[str, float]):
    now = datetime.now(timezone.utc)
    for name, days in days_ago_by_name.items():
        sql("UPDATE backup_files SET created_at = ? WHERE filename = ?", (now - timedelta(days=days)).isoformat(), name)


def put_aged(client, token, name, days_ago):
    """Upload, then pretend it happened `days_ago` days back before the next upload prunes anything."""
    r = put(client, token, name)
    assert r.status_code == 201, r.text
    backdate({name: days_ago})
    return r


# --- retention policy --------------------------------------------------------------------------

def day(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


def test_keeps_last_upload_per_day_for_n_days():
    files = [("a1", day(2026, 1, 1, 1)), ("a2", day(2026, 1, 1, 23)), ("b", day(2026, 1, 2)), ("c", day(2026, 1, 3))]
    assert select_keep(files, 2, 0, 0, 0) == {"b", "c"}
    assert select_keep(files, 3, 0, 0, 0) == {"a2", "b", "c"}  # latest of the day wins


def test_monthly_and_yearly_keep_the_first_of_each_period():
    files = [(f"{y}-{m:02}-{d:02}", day(y, m, d)) for y in (2024, 2025, 2026) for m in range(1, 13) for d in (5, 20)]
    keep = select_keep(files, 0, 0, 12, 2)
    assert keep >= {"2026-12-20"}  # newest is always kept
    monthly = {f for f in keep if f.endswith("-05") and f.startswith("2026")}
    assert len(monthly) == 12 and "2026-12-05" in monthly and "2026-01-05" in monthly
    assert "2025-12-05" not in keep and "2025-02-05" not in keep  # outside the 12 months, not a year's first
    assert {"2025-01-05", "2026-01-05"} <= keep and "2024-01-05" not in keep


def test_weekly_and_empty_and_newest_always_kept():
    assert select_keep([], 7, 4, 12, 10) == set()
    files = [(str(i), day(2026, 1, 1) + timedelta(days=i)) for i in range(30)]
    assert "29" in select_keep(files, 0, 0, 0, 0)
    assert len(select_keep(files, 0, 3, 0, 0) - {"29"}) == 3


def test_default_policy_size_is_bounded():
    """A daily backup for ten years never keeps more than 7 + 12 + 10 files (plus the newest)."""
    start = day(2016, 1, 1)
    files = [(str(i), start + timedelta(days=i)) for i in range(3650)]
    assert len(select_keep(files, 7, 0, 12, 10)) <= 7 + 12 + 10 + 1


# --- admin -------------------------------------------------------------------------------------

def test_admin_creates_lists_adjusts_and_revokes(admin, drive):
    out, folder = make_share(admin, drive)
    assert out["token"].startswith("bk_") and out["token"] in out["curl"]
    (row,) = admin.get("/admin/api/backups").json()
    assert row["gdrive_folder_path"] == "Backups" and row["files"] == 0
    assert (row["keep_daily"], row["keep_weekly"], row["keep_monthly"], row["keep_yearly"]) == (7, 4, 12, 10)
    assert "token" not in row and "token_hash" not in row
    assert out["token"] not in sql("SELECT token_hash FROM backups")[0][0]  # only a hash is stored

    r = admin.patch(f"/admin/api/backups/{out['id']}", json={"name": "renamed", "keep_daily": 3})
    assert r.status_code == 200 and r.json()["keep_daily"] == 3
    assert admin.patch(f"/admin/api/backups/{out['id']}", json={}).status_code == 422
    assert admin.patch(f"/admin/api/backups/{out['id']}",
                       json={"keep_daily": 0, "keep_weekly": 0, "keep_monthly": 0, "keep_yearly": 0}).status_code == 422

    assert admin.post(f"/admin/api/backups/{out['id']}/revoke").json() == {"ok": True}
    assert admin.patch(f"/admin/api/backups/{out['id']}", json={"name": "x"}).status_code == 409
    assert admin.post("/admin/api/backups/nope/revoke").status_code == 404


def test_admin_endpoints_need_login_and_a_real_folder(app, admin, drive):
    assert new_device(app).post("/admin/api/backups", json={}).status_code == 401
    r = admin.post("/admin/api/backups", json={"name": "x", "gdrive_folder_id": "missing"})
    assert r.status_code == 400
    r = admin.post("/admin/api/backups", json={"name": "x", "gdrive_folder_id": drive.add_folder(),
                                                "keep_daily": 0, "keep_weekly": 0, "keep_monthly": 0, "keep_yearly": 0})
    assert r.status_code == 422


# --- the token API -----------------------------------------------------------------------------

def test_put_streams_into_the_folder_and_lists(admin, drive):
    out, folder = make_share(admin, drive)
    data = os.urandom(ALIGN * 2 + 17)
    r = admin.put("/api/backup/db-2026-11-08.sql.gz", content=data,
                  headers={**auth(out["token"]), "Content-Type": "application/gzip"})
    assert r.status_code == 201, r.text
    (f,) = drive.named("db-2026-11-08.sql.gz")
    assert f["parents"] == [folder] and f["content"] == data and f["mimeType"] == "application/gzip"
    assert [n for n, _ in drive.chunks] == [ALIGN, ALIGN, 17]

    body = admin.get("/api/backup", headers=auth(out["token"])).json()
    assert body["name"] == "nightly" and [x["filename"] for x in body["files"]] == ["db-2026-11-08.sql.gz"]
    assert body["files"][0]["size"] == len(data) and body["retention"]["keep_monthly"] == 12
    (row,) = admin.get("/admin/api/backups").json()
    assert row["files"] == 1 and row["bytes"] == len(data) and row["last_upload_at"]


def test_token_is_required_and_revocation_is_immediate(admin, drive):
    out, _ = make_share(admin, drive)
    assert put(admin, "bk_wrong", "a.bin").status_code == 401
    assert admin.put("/api/backup/a.bin", content=b"x").status_code == 401
    assert admin.get("/api/backup").status_code == 401
    assert put(admin, out["token"], "a.bin").status_code == 201
    admin.post(f"/admin/api/backups/{out['id']}/revoke")
    assert put(admin, out["token"], "b.bin").status_code == 401
    assert admin.get("/api/backup", headers=auth(out["token"])).status_code == 401
    assert [e for e in admin.get("/admin/api/events?type=backup_auth_failed").json()["events"]]
    assert not drive.named("b.bin")


def test_an_admin_cookie_is_not_a_backup_token(admin, drive):
    make_share(admin, drive)
    assert admin.put("/api/backup/x.bin", content=b"x", headers=auth("not-a-token")).status_code == 401


def test_empty_body_and_filename_handling(admin, drive):
    out, _ = make_share(admin, drive)
    assert put(admin, out["token"], "empty.bin", b"").status_code == 400
    assert sql("SELECT COUNT(*) FROM backup_files")[0][0] == 0
    assert not drive.sessions  # the Drive upload was aborted, not left dangling
    r = put(admin, out["token"], "..%2F..%2Fetc%2Fpasswd")
    assert r.status_code == 201 and r.json()["filename"] == "passwd"


def test_drive_failure_leaves_nothing_behind(admin, drive):
    out, _ = make_share(admin, drive)
    drive.token_status = 400
    gdrive._tokens.clear()  # drop the access token cached while creating the share
    r = put(admin, out["token"], "a.bin")
    assert r.status_code == 502
    assert sql("SELECT COUNT(*) FROM backup_files")[0][0] == 0
    assert admin.get("/admin/api/events?type=backup_failed").json()["events"]


def test_prune_applies_retention_after_each_upload(admin, drive):
    out, _ = make_share(admin, drive, keep_daily=2, keep_weekly=0, keep_monthly=0, keep_yearly=0)
    t = out["token"]
    for name, ago in (("d4", 4), ("d3", 3), ("d2", 2), ("d1", 1)):
        put_aged(admin, t, name, ago)
    r = put(admin, t, "d0")
    assert r.json()["pruned"] == ["d2"]  # d4 and d3 went at earlier uploads
    left = [x["filename"] for x in admin.get("/api/backup", headers=auth(t)).json()["files"]]
    assert left == ["d0", "d1"]
    # pruned files went to the Drive trash, kept ones didn't
    assert all(drive.named(n)[0].get("trashed") for n in ("d2", "d3", "d4"))
    assert not any(drive.named(n)[0].get("trashed") for n in ("d0", "d1"))
    assert len(admin.get("/admin/api/events?type=backup_pruned").json()["events"]) == 3


def test_monthly_keepers_survive_daily_pruning(admin, drive):
    out, _ = make_share(admin, drive, keep_daily=1, keep_weekly=0, keep_monthly=2, keep_yearly=0)
    t = out["token"]
    for n, ago in (("m-oldest", 130), ("m-prev", 45), ("recent", 1)):
        put_aged(admin, t, n, ago)
    put(admin, t, "now")
    left = {x["filename"] for x in admin.get("/api/backup", headers=auth(t)).json()["files"]}
    assert "m-oldest" not in left and {"m-prev", "now"} <= left


def test_shortening_retention_prunes_immediately(admin, drive):
    out, _ = make_share(admin, drive, keep_daily=7, keep_weekly=0)
    t = out["token"]
    for n, ago in (("a", 3), ("b", 2), ("c", 1)):
        put_aged(admin, t, n, ago)
    r = admin.patch(f"/admin/api/backups/{out['id']}", json={"keep_daily": 1, "keep_weekly": 0, "keep_monthly": 0, "keep_yearly": 0})
    assert r.json()["pruned"] == 2
    assert [x["filename"] for x in admin.get("/api/backup", headers=auth(t)).json()["files"]] == ["c"]


def test_prune_failure_is_retried_on_the_next_upload(admin, drive, monkeypatch):
    from services import gdrive
    out, _ = make_share(admin, drive, keep_daily=1, keep_weekly=0, keep_monthly=0, keep_yearly=0)
    t = out["token"]
    put_aged(admin, t, "old", 2)
    real = gdrive.trash_file

    async def broken(*a):
        raise gdrive.DriveError("boom")
    monkeypatch.setattr(gdrive, "trash_file", broken)
    r = put(admin, t, "new")
    assert r.status_code == 201 and r.json()["pruned"] == []  # the upload still succeeds
    assert admin.get("/admin/api/events?type=backup_prune_failed").json()["events"]
    monkeypatch.setattr(gdrive, "trash_file", real)
    assert put(admin, t, "newer").json()["pruned"] == ["old", "new"]
    assert "old" not in [x["filename"] for x in admin.get("/api/backup", headers=auth(t)).json()["files"]]


def test_shares_are_independent(admin, drive):
    a, fa = make_share(admin, drive, keep_daily=1, keep_weekly=0, keep_monthly=0, keep_yearly=0)
    b, fb = make_share(admin, drive, keep_daily=1, keep_weekly=0, keep_monthly=0, keep_yearly=0)
    put_aged(admin, a["token"], "a1", 2)
    put(admin, b["token"], "b1")
    put(admin, a["token"], "a2")
    assert [x["filename"] for x in admin.get("/api/backup", headers=auth(a["token"])).json()["files"]] == ["a2"]
    assert [x["filename"] for x in admin.get("/api/backup", headers=auth(b["token"])).json()["files"]] == ["b1"]
    assert drive.named("b1")[0]["parents"] == [fb]
