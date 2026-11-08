import sqlite3

import config
from tests.conftest import new_device
from tests.test_upload import joined, upload


def events(admin, **params):
    return admin.get("/admin/api/events", params=params).json()


def types(admin, **params):
    return [e["type"] for e in reversed(events(admin, **params)["events"])]


def test_events_require_admin(app):
    assert app.get("/admin/api/events").status_code == 401


def test_visitor_and_uploader_actions_are_recorded(admin, make_box):
    box, _ = make_box()
    assert new_device(admin).get("/drop/not-a-box").status_code == 403
    d = new_device(admin)
    d.get(f"/drop/{box}")
    r = upload(d, b"abc", "doc.txt", note="hi").json()
    d.patch(f"/api/files/{r['id']}", json={"filename": "renamed.txt"})
    d.delete(f"/api/files/{r['id']}")
    assert types(admin) == ["access_denied", "device_onboarded", "upload_completed", "file_renamed", "file_deleted"]

    up = next(e for e in events(admin)["events"] if e["type"] == "upload_completed")
    assert up["detail"] == {"file_id": r["id"], "filename": "doc.txt", "size": 3, "has_note": True}
    assert up["dropbox_name"] == "Test box" and up["actor"].startswith("device:") and up["ip"]
    assert len(up["device_ref"]) == 16


def test_log_never_exposes_the_device_cookie(admin, make_box):
    d, *_ = joined(admin, make_box)
    upload(d, b"x")
    cookie = d.cookies["device_session"]
    assert cookie not in admin.get("/admin/api/events").text


def test_admin_actions_are_recorded_with_who(admin, drive):
    folder = drive.add_folder("F")
    box = admin.post("/admin/api/dropboxes", json={"name": "N", "gdrive_folder_id": folder, "password": "pw"}).json()["id"]
    admin.patch(f"/admin/api/dropboxes/{box}", json={"max_devices": 5})
    admin.post(f"/admin/api/dropboxes/{box}/revoke")
    assert types(admin) == ["dropbox_created", "dropbox_adjusted", "dropbox_revoked"]
    created = events(admin)["events"][-1]
    assert created["actor"] == "admin:admin@example.com"
    assert created["detail"]["has_password"] is True and "pw" not in str(created)


def test_failed_password_and_revoked_device_are_recorded(admin, make_box):
    box, _ = make_box(password="secret")
    d = new_device(admin)
    assert d.post(f"/drop/{box}/unlock", json={"password": "nope"}).status_code == 401
    assert d.post(f"/drop/{box}/unlock", json={"password": "secret"}).status_code == 200
    ref = admin.get(f"/admin/api/dropboxes/{box}/devices").json()[0]["ref"]
    admin.post(f"/admin/api/dropboxes/{box}/devices/{ref}/revoke")
    assert types(admin) == ["unlock_failed", "device_onboarded", "device_revoked"]


def test_filtering_and_paging(admin, make_box):
    box, _ = make_box()
    for _ in range(3):
        new_device(admin).get(f"/drop/{box}")
    new_device(admin).get("/drop/nope")
    assert set(types(admin, type="device_onboarded")) == {"device_onboarded"}
    assert types(admin, dropbox_id=box) == ["device_onboarded"] * 3
    page1 = events(admin, limit=2)
    assert len(page1["events"]) == 2 and page1["next"]
    page2 = events(admin, limit=2, before=page1["next"])
    assert [e["id"] for e in page2["events"]] == [e["id"] for e in events(admin)["events"][2:4]]
    assert "access_denied" in events(admin)["types"]


def test_log_survives_deleting_what_it_describes(admin, make_box):
    box, _ = make_box()
    new_device(admin).get(f"/drop/{box}")
    c = sqlite3.connect(config.DB_PATH)
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("DELETE FROM device_sessions")
    c.execute("DELETE FROM dropboxes")
    c.commit()
    assert types(admin) == ["device_onboarded"]
