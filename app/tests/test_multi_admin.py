"""Two admins on one install: each sees and touches only their own shares, events and notices."""
import asyncio
import sqlite3
from datetime import timedelta

import config
from services import notifier
from services.sessions import utcnow
from tests.conftest import new_device
from tests.test_upload import joined, upload


def sql(q, *a):
    db = sqlite3.connect(config.DB_PATH)
    try:
        return db.execute(q, a).fetchall()
    finally:
        db.close()


def make_backup(client, drive, name="nightly"):
    r = client.post("/admin/api/backups", json={"name": name, "gdrive_folder_id": drive.add_folder()})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_lists_only_show_own_shares(admin, other_admin, make_box, drive):
    mine, _ = make_box()
    theirs, _ = make_box(owner="other@example.com")
    mine_backup, theirs_backup = make_backup(admin, drive), make_backup(other_admin, drive)

    assert [d["id"] for d in admin.get("/admin/api/dropboxes").json()] == [mine]
    assert [d["id"] for d in other_admin.get("/admin/api/dropboxes").json()] == [theirs]
    assert [b["id"] for b in admin.get("/admin/api/backups").json()] == [mine_backup]
    assert [b["id"] for b in other_admin.get("/admin/api/backups").json()] == [theirs_backup]


def test_cannot_touch_another_admins_shares(admin, other_admin, make_box, drive):
    box, _ = make_box()
    backup = make_backup(admin, drive)
    dev = new_device(admin)
    assert dev.get(f"/drop/{box}").status_code == 200
    (ref,) = [d["ref"] for d in admin.get(f"/admin/api/dropboxes/{box}/devices").json()]

    for resp in (
        other_admin.get(f"/admin/api/dropboxes/{box}/devices"),
        other_admin.post(f"/admin/api/dropboxes/{box}/devices/{ref}/revoke"),
        other_admin.patch(f"/admin/api/dropboxes/{box}", json={"name": "mine now"}),
        other_admin.post(f"/admin/api/dropboxes/{box}/revoke"),
        other_admin.patch(f"/admin/api/backups/{backup}", json={"name": "mine now"}),
        other_admin.post(f"/admin/api/backups/{backup}/revoke"),
    ):
        assert resp.status_code == 404, resp.request.url
    assert sql("SELECT name, is_revoked FROM dropboxes WHERE id = ?", box) == [("Test box", 0)]
    assert sql("SELECT is_revoked FROM backups WHERE id = ?", backup) == [(0,)]
    assert sql("SELECT is_revoked FROM device_sessions") == [(0,)]


def test_events_are_split_by_owner(admin, other_admin, make_box, drive):
    mine, _ = make_box()
    theirs, _ = make_box(owner="other@example.com")
    new_device(admin).get(f"/drop/{mine}")      # a device joins my box
    new_device(admin).get("/drop/nope")          # a stranger: nobody's event
    make_backup(other_admin, drive)

    def seen(client):
        return {(e["type"], e["dropbox_id"]) for e in client.get("/admin/api/events").json()["events"]}

    a, b = seen(admin), seen(other_admin)
    assert ("device_onboarded", mine) in a and ("device_onboarded", mine) not in b
    assert not any(t == "backup_created" for t, _ in a) and any(t == "backup_created" for t, _ in b)
    # unowned events belong to the first admin only
    assert ("access_denied", "nope") in a and ("access_denied", "nope") not in b
    assert theirs not in {d for _, d in a}
    types_b = other_admin.get("/admin/api/events").json()["types"]
    assert "backup_created" in types_b and "device_onboarded" not in types_b
    # asking for someone else's dropbox by id yields nothing
    assert other_admin.get("/admin/api/events", params={"dropbox_id": mine}).json()["events"] == []


def test_upload_notice_goes_to_the_owner(app, other_admin, make_box, outbox):
    d, *_ = joined(app, make_box, owner="other@example.com")
    assert upload(d, b"hi").status_code == 200
    assert asyncio.run(notifier.flush_once(utcnow() + timedelta(seconds=config.NOTIFY_QUIET_SECONDS + 5))) == 1
    assert outbox.to == [["other@example.com"]]
    sent = [e for e in other_admin.get("/admin/api/events").json()["events"] if e["type"] == "notification_sent"]
    assert sent and sent[0]["detail"]["to"] == ["other@example.com"]


def test_status_and_test_email_target_the_admin(admin, other_admin, outbox):
    assert other_admin.get("/admin/api/status").json()["email"]["to"] == ["other@example.com"]
    assert other_admin.post("/admin/api/notifications/test").status_code == 200
    assert outbox.to == [["other@example.com"]]


def test_notify_override_applies_to_the_first_admin_only(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_EMAILS", ["admin@example.com", "other@example.com"])
    monkeypatch.setattr(config, "NOTIFY_EMAILS", ["ops@example.com"])
    assert config.notify_addresses("admin@example.com") == ["ops@example.com"]
    assert config.notify_addresses("other@example.com") == ["other@example.com"]


def test_existing_events_are_backfilled_with_an_owner(tmp_path, monkeypatch):
    import db as dbmod
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE dropboxes (id TEXT PRIMARY KEY, created_by TEXT);
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, type TEXT NOT NULL,
            actor TEXT NOT NULL, dropbox_id TEXT, device_ref TEXT, ip TEXT, detail TEXT);
        INSERT INTO dropboxes VALUES ('d1', 'a@x.com');
        INSERT INTO events (ts, type, actor, dropbox_id) VALUES ('t', 'upload_completed', 'device:1', 'd1');
        INSERT INTO events (ts, type, actor) VALUES ('t', 'admin_login', 'admin:b@x.com');
        INSERT INTO events (ts, type, actor) VALUES ('t', 'admin_login_denied', 'visitor');
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", str(path))
    dbmod.init_db()
    assert sql("SELECT owner FROM events ORDER BY id") == [("a@x.com",), ("b@x.com",), (None,)]
