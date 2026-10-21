import sqlite3
from datetime import timedelta

import config
from services.sessions import utcnow
from tests.conftest import new_device


def test_cookie_reuse_and_device_cap(app, make_box):
    box, _ = make_box(max_devices=2)
    a, b, c = new_device(app), new_device(app), new_device(app)
    assert a.get(f"/drop/{box}").status_code == 200
    assert a.get(f"/drop/{box}").status_code == 200  # same device, no new slot
    assert b.get(f"/drop/{box}").status_code == 200
    assert c.get(f"/drop/{box}").status_code == 403
    assert a.get(f"/drop/{box}").status_code == 200


def test_unknown_expired_and_revoked_are_forbidden(app, make_box):
    assert new_device(app).get("/drop/nope").status_code == 403
    box, _ = make_box(revoked=1)
    assert new_device(app).get(f"/drop/{box}").status_code == 403
    box, _ = make_box()
    d = new_device(app)
    assert d.get(f"/drop/{box}").status_code == 200
    db = sqlite3.connect(config.DB_PATH)
    db.execute("UPDATE dropboxes SET expires_at = ?", ((utcnow() - timedelta(seconds=1)).isoformat(),))
    db.commit()
    assert d.get(f"/drop/{box}").status_code == 403  # even with a cookie


def test_onboard_window_blocks_new_devices_but_not_existing_ones(app, make_box):
    box, _ = make_box(window_hours=48)
    old = new_device(app)
    assert old.get(f"/drop/{box}").status_code == 200
    db = sqlite3.connect(config.DB_PATH)
    db.execute("UPDATE dropboxes SET created_at = ?", ((utcnow() - timedelta(hours=49)).isoformat(),))
    db.commit()
    assert old.get(f"/drop/{box}").status_code == 200
    assert new_device(app).get(f"/drop/{box}").status_code == 403


def test_revoked_device_session_loses_access(app, make_box):
    box, _ = make_box()
    d = new_device(app)
    d.get(f"/drop/{box}")
    assert d.get("/api/my-files").status_code == 200
    db = sqlite3.connect(config.DB_PATH)
    db.execute("UPDATE device_sessions SET is_revoked = 1")
    db.commit()
    assert d.get("/api/my-files").status_code == 403


def test_password_flow(app, make_box):
    box, _ = make_box(password="hunter2", max_devices=1)
    d = new_device(app)
    page = d.get(f"/drop/{box}")
    assert page.status_code == 200 and "Password required" in page.text
    assert d.get("/api/my-files").status_code == 403  # no session yet
    assert d.post(f"/drop/{box}/unlock", json={"password": "wrong"}).status_code == 401
    # a wrong guess must not have used up the only device slot
    assert d.post(f"/drop/{box}/unlock", json={"password": "hunter2"}).status_code == 200
    assert d.get("/api/my-files").status_code == 200
    assert "Upload files" in d.get(f"/drop/{box}").text  # cookie now skips the prompt


def test_password_attempts_are_throttled(app, make_box):
    box, _ = make_box(password="hunter2")
    d = new_device(app)
    codes = [d.post(f"/drop/{box}/unlock", json={"password": "x"}).status_code for _ in range(7)]
    assert codes == [401] * 5 + [429] * 2
    assert d.post(f"/drop/{box}/unlock", json={"password": "hunter2"}).status_code == 429
