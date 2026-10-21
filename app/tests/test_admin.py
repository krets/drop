import sqlite3

import config
from services import gdrive
from tests.conftest import new_device


def test_admin_requires_login(app):
    assert app.get("/admin/api/dropboxes").status_code == 401
    assert app.get("/admin/", follow_redirects=False).status_code in (302, 307)


def test_non_admin_cookie_is_rejected(app):
    from tests.conftest import signed_cookie
    app.cookies.set("admin_session", signed_cookie({"admin_email": "stranger@example.com"}))
    assert app.get("/admin/api/dropboxes").status_code == 401


def test_create_list_and_revoke(admin, drive):
    folder = drive.add_folder("Inbox")
    assert {"id": folder, "name": "Inbox"} in admin.get("/admin/api/folders").json()

    r = admin.post("/admin/api/dropboxes", json={"name": "Taxes", "gdrive_folder_id": folder, "password": "pw",
                                                 "expires_in_days": 10, "onboard_window_hours": 24, "max_devices": 2})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["helper_text"].splitlines() == [
        f"Hey! Please upload your files securely here: {config.BASE_URL}/drop/{body['id']}",
        "* Password: pw",
        "* Note: This link expires in 10 days, but you must open it on your devices within 24 hours. Max 2 devices.",
    ]
    (row,) = admin.get("/admin/api/dropboxes").json()
    assert row["has_password"] == 1 and row["max_devices"] == 2 and "password_hash" not in row

    assert admin.post(f"/admin/api/dropboxes/{body['id']}/revoke").json() == {"ok": True}
    assert new_device(admin).get(f"/drop/{body['id']}").status_code == 403


def test_create_rejects_missing_or_non_folder(admin, drive):
    assert admin.post("/admin/api/dropboxes", json={"name": "x", "gdrive_folder_id": "nope"}).status_code == 400
    f = drive.files[drive.add_folder()]
    f["mimeType"] = "text/plain"
    assert admin.post("/admin/api/dropboxes", json={"name": "x", "gdrive_folder_id": f["id"]}).status_code == 400
    assert admin.get("/admin/api/folders?parent=a'b").status_code == 400


def test_helper_text_without_password(admin, drive):
    folder = drive.add_folder()
    body = admin.post("/admin/api/dropboxes", json={"name": "x", "gdrive_folder_id": folder}).json()
    assert "Password" not in body["helper_text"]


def test_stale_uploading_rows_are_purged_on_startup(tmp_path, monkeypatch):
    import db
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "s.db"))
    db.init_db()
    c = sqlite3.connect(config.DB_PATH)
    c.execute("PRAGMA foreign_keys = OFF")
    c.execute("INSERT INTO uploaded_files (id, dropbox_id, session_id, original_filename, status)"
              " VALUES ('1', 'd', 's', 'f', 'uploading')")
    c.commit()
    db.init_db()
    assert c.execute("SELECT COUNT(*) FROM uploaded_files").fetchone()[0] == 0


def test_old_databases_gain_new_upload_columns(tmp_path, monkeypatch):
    import db
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "old.db"))
    c = sqlite3.connect(config.DB_PATH)
    c.execute("CREATE TABLE uploaded_files (id TEXT PRIMARY KEY, dropbox_id TEXT, session_id TEXT,"
              " gdrive_file_id TEXT, sidecar_gdrive_id TEXT, original_filename TEXT, status TEXT)")
    c.commit()
    db.init_db()
    cols = {r[1] for r in c.execute("PRAGMA table_info(uploaded_files)")}
    assert {"size", "created_at", "note"} <= cols
    db.init_db()  # idempotent


def test_me_and_list_expose_link_and_folder_path(admin, drive):
    assert admin.get("/admin/api/me").json() == {"email": "admin@example.com"}

    inbox = drive.add_folder("Inbox")
    other = drive.add_folder("Other")
    a = admin.post("/admin/api/dropboxes", json={"name": "A", "gdrive_folder_id": inbox,
                                                 "gdrive_folder_path": "My Drive / Work / Inbox"}).json()
    b = admin.post("/admin/api/dropboxes", json={"name": "B", "gdrive_folder_id": other}).json()
    rows = {r["id"]: r for r in admin.get("/admin/api/dropboxes").json()}
    assert rows[a["id"]]["gdrive_folder_path"] == "My Drive / Work / Inbox"
    assert rows[b["id"]]["gdrive_folder_path"] == "Other"  # falls back to the folder's own name
    assert rows[a["id"]]["url"] == f"{config.BASE_URL}/drop/{a['id']}"


def test_me_requires_login(app):
    assert app.get("/admin/api/me").status_code == 401


def test_list_resolves_and_caches_legacy_folder_paths(admin, drive, make_box):
    drive.files["root"] = {"id": "root", "name": "My Drive", "mimeType": gdrive.FOLDER_MIME}
    work = drive.add_folder("Work")
    box_id, inbox = make_box()
    drive.files[inbox].update(name="Inbox", parents=[work])
    gone_id, _ = make_box()
    db = sqlite3.connect(config.DB_PATH)
    db.execute("UPDATE dropboxes SET gdrive_folder_id = 'missing' WHERE id = ?", (gone_id,))
    db.commit()

    rows = {r["id"]: r for r in admin.get("/admin/api/dropboxes").json()}
    assert rows[box_id]["gdrive_folder_path"] == "My Drive / Work / Inbox"
    assert rows[gone_id]["gdrive_folder_path"] is None  # lookup failed: the UI falls back

    stored = dict(db.execute("SELECT id, gdrive_folder_path FROM dropboxes").fetchall())
    assert stored[box_id] == "My Drive / Work / Inbox" and stored[gone_id] is None
    drive.files.pop(inbox)  # served from the cache from now on
    assert {r["id"]: r for r in admin.get("/admin/api/dropboxes").json()}[box_id]["gdrive_folder_path"] == "My Drive / Work / Inbox"
