import sqlite3
from datetime import datetime, timedelta

import config
from services.sessions import utcnow
from tests.conftest import new_device
from tests.test_upload import upload


def db():
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def box_row(box):
    return db().execute("SELECT * FROM dropboxes WHERE id = ?", (box,)).fetchone()


def iso(s):
    return datetime.fromisoformat(s)


def devices(admin, box):
    return admin.get(f"/admin/api/dropboxes/{box}/devices").json()


# --- extending -------------------------------------------------------------------------------

def test_extending_the_onboarding_window_lets_new_devices_in(admin, make_box):
    box, _ = make_box(window_hours=48)
    c = db()
    c.execute("UPDATE dropboxes SET created_at = ?", ((utcnow() - timedelta(hours=60)).isoformat(),))
    c.commit()
    assert new_device(admin).get(f"/drop/{box}").status_code == 403  # window closed 12h ago

    r = admin.patch(f"/admin/api/dropboxes/{box}", json={"extend_onboarding_hours": 24})
    assert r.status_code == 200, r.text
    # an already-closed window reopens for 24h from now, not from when it closed
    end = iso(box_row(box)["created_at"]) + timedelta(hours=box_row(box)["onboard_window_hours"])
    assert timedelta(hours=23) < end - utcnow() <= timedelta(hours=25)
    assert new_device(admin).get(f"/drop/{box}").status_code == 200


def test_extending_an_open_window_adds_to_its_end(admin, make_box):
    box, _ = make_box(window_hours=48)
    admin.patch(f"/admin/api/dropboxes/{box}", json={"extend_onboarding_hours": 10})
    assert 58 <= box_row(box)["onboard_window_hours"] <= 59  # 48 + 10, rounded up to whole hours


def test_extending_expiry_adds_days_and_revives_an_expired_dropbox(admin, make_box):
    box, _ = make_box(days=30)
    before = iso(box_row(box)["expires_at"])
    admin.patch(f"/admin/api/dropboxes/{box}", json={"extend_expiry_days": 7})
    assert iso(box_row(box)["expires_at"]) - before == timedelta(days=7)

    d = new_device(admin)
    assert d.get(f"/drop/{box}").status_code == 200
    c = db()
    c.execute("UPDATE dropboxes SET expires_at = ?", ((utcnow() - timedelta(days=2)).isoformat(),))
    c.commit()
    assert d.get("/api/my-files").status_code == 403  # expired, even with a cookie

    admin.patch(f"/admin/api/dropboxes/{box}", json={"extend_expiry_days": 5})
    assert timedelta(days=4) < iso(box_row(box)["expires_at"]) - utcnow() <= timedelta(days=5)  # from now
    assert d.get("/api/my-files").status_code == 200


def test_raising_max_devices_admits_another_device(admin, make_box):
    box, _ = make_box(max_devices=1)
    assert new_device(admin).get(f"/drop/{box}").status_code == 200
    assert new_device(admin).get(f"/drop/{box}").status_code == 403
    assert admin.patch(f"/admin/api/dropboxes/{box}", json={"max_devices": 2}).json()["max_devices"] == 2
    assert new_device(admin).get(f"/drop/{box}").status_code == 200


def test_renaming_a_dropbox(admin, make_box):
    box, _ = make_box()
    r = admin.patch(f"/admin/api/dropboxes/{box}", json={"name": "  Q3 receipts  "})
    assert r.status_code == 200 and r.json()["name"] == "Q3 receipts"
    assert admin.get("/admin/api/dropboxes").json()[0]["name"] == "Q3 receipts"
    d = new_device(admin)
    d.get(f"/drop/{box}")
    assert d.get("/api/me").json() == {"name": "Q3 receipts"}  # uploaders see the new name
    # renaming alone leaves the limits untouched
    row = box_row(box)
    assert (row["max_devices"], row["onboard_window_hours"]) == (3, 48)
    assert admin.patch(f"/admin/api/dropboxes/{box}", json={"name": "   "}).status_code == 422
    assert admin.patch(f"/admin/api/dropboxes/{box}", json={"name": "x" * 201}).status_code == 422


def test_adjust_validation_and_errors(admin, make_box):
    box, _ = make_box()
    assert admin.patch(f"/admin/api/dropboxes/{box}", json={}).status_code == 422
    assert admin.patch(f"/admin/api/dropboxes/{box}", json={"extend_expiry_days": 0}).status_code == 422
    assert admin.patch(f"/admin/api/dropboxes/{box}", json={"max_devices": 51}).status_code == 422
    assert admin.patch("/admin/api/dropboxes/nope", json={"max_devices": 2}).status_code == 404
    admin.post(f"/admin/api/dropboxes/{box}/revoke")
    assert admin.patch(f"/admin/api/dropboxes/{box}", json={"max_devices": 2}).status_code == 409


def test_adjusting_requires_admin(app, make_box):
    box, _ = make_box()
    assert app.patch(f"/admin/api/dropboxes/{box}", json={"max_devices": 9}).status_code == 401
    assert app.get(f"/admin/api/dropboxes/{box}/devices").status_code == 401
    assert app.post(f"/admin/api/dropboxes/{box}/devices/abc/revoke").status_code == 401


def test_listing_reports_when_onboarding_ends(admin, make_box):
    box, _ = make_box(window_hours=48)
    (row,) = admin.get("/admin/api/dropboxes").json()
    assert 47 <= (iso(row["onboard_ends_at"]) - utcnow()).total_seconds() / 3600 <= 48


# --- devices ---------------------------------------------------------------------------------

def test_devices_list_shows_browser_activity_and_uploads(admin, make_box):
    box, _ = make_box()
    d = new_device(admin)
    d.headers["user-agent"] = "Mozilla/5.0 (X11; Linux x86_64) Firefox/130.0"
    d.get(f"/drop/{box}")
    upload(d, b"x", "a.txt")
    d.post("/api/comments", json={"text": "hi"})
    new_device(admin).get(f"/drop/{box}")

    first, second = devices(admin, box)
    assert first["user_agent"].endswith("Firefox/130.0") and first["files"] == 1 and first["comments"] == 1
    assert first["last_seen_at"] and first["is_revoked"] == 0
    assert second["files"] == 0 and second["ref"] != first["ref"]


def test_device_ref_does_not_leak_the_session_cookie(admin, make_box):
    box, _ = make_box()
    d = new_device(admin)
    d.get(f"/drop/{box}")
    cookie = d.cookies.get(config.DEVICE_COOKIE)
    assert cookie and cookie not in admin.get(f"/admin/api/dropboxes/{box}/devices").text


def test_revoking_one_device_frees_a_slot_and_locks_it_out(admin, make_box):
    box, _ = make_box(max_devices=2)
    a, b = new_device(admin), new_device(admin)
    a.get(f"/drop/{box}")
    b.get(f"/drop/{box}")
    assert new_device(admin).get(f"/drop/{box}").status_code == 403  # full

    ref = devices(admin, box)[0]["ref"]
    assert admin.post(f"/admin/api/dropboxes/{box}/devices/{ref}/revoke").status_code == 200
    assert a.get("/api/my-files").status_code == 403  # the revoked device is out
    assert b.get("/api/my-files").status_code == 200  # the other is untouched
    assert devices(admin, box)[0]["is_revoked"] == 1
    assert new_device(admin).get(f"/drop/{box}").status_code == 200  # slot is free again


def test_revoke_device_scoped_to_its_dropbox(admin, make_box):
    box1, _ = make_box()
    box2, _ = make_box()
    new_device(admin).get(f"/drop/{box1}")
    ref = devices(admin, box1)[0]["ref"]
    assert admin.post(f"/admin/api/dropboxes/{box2}/devices/{ref}/revoke").status_code == 404
    assert admin.post(f"/admin/api/dropboxes/{box1}/devices/nope/revoke").status_code == 404
    assert admin.get("/admin/api/dropboxes/nope/devices").status_code == 404
    assert devices(admin, box1)[0]["is_revoked"] == 0


def test_last_seen_updates_only_after_the_interval(admin, make_box):
    box, _ = make_box()
    d = new_device(admin)
    d.get(f"/drop/{box}")
    c = db()
    old = (utcnow() - timedelta(hours=3)).isoformat()
    c.execute("UPDATE device_sessions SET last_seen_at = ?", (old,))
    c.commit()
    d.get("/api/my-files")
    seen = iso(devices(admin, box)[0]["last_seen_at"])
    assert utcnow() - seen < timedelta(minutes=1)  # was 3h stale, now refreshed

    d.get("/api/my-files")
    assert iso(devices(admin, box)[0]["last_seen_at"]) == seen  # no write inside the interval


def test_old_databases_gain_device_columns(tmp_path, monkeypatch):
    import db as dbmod
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "old.db"))
    c = sqlite3.connect(config.DB_PATH)
    c.execute("CREATE TABLE device_sessions (id TEXT PRIMARY KEY, dropbox_id TEXT, created_at TEXT NOT NULL,"
              " is_revoked INTEGER NOT NULL DEFAULT 0)")
    c.commit()
    dbmod.init_db()
    assert {"user_agent", "last_seen_at"} <= {r[1] for r in c.execute("PRAGMA table_info(device_sessions)")}
