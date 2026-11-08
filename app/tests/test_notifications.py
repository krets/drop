import asyncio
import sqlite3
from datetime import timedelta

import config
from services import notifier
from services.sessions import utcnow
from tests.conftest import new_device
from tests.test_upload import joined, upload


def flush(at=None):
    return asyncio.run(notifier.flush_once(at))


def later(seconds):
    return utcnow() + timedelta(seconds=seconds)


def test_uploads_are_batched_into_one_email_after_a_quiet_period(app, make_box, outbox):
    d, *_ = joined(app, make_box)
    upload(d, b"1", "a.txt", note="for the taxes")
    upload(d, b"22", "b.txt")

    assert flush() == 0 and outbox == []  # still inside the quiet period
    assert flush(later(config.NOTIFY_QUIET_SECONDS - 10)) == 0
    assert flush(later(config.NOTIFY_QUIET_SECONDS + 5)) == 1

    (subject, body), = outbox
    assert subject == "[Drop] 2 files uploaded to Test box"
    assert "a.txt" in body and "b.txt" in body and "note: for the taxes" in body
    assert f"{config.BASE_URL}/admin/" in body
    assert flush(later(10_000)) == 0 and len(outbox) == 1  # reported once only


def test_each_device_gets_its_own_notice(app, make_box, outbox):
    d1, box, _ = joined(app, make_box)
    d2 = new_device(app)
    assert d2.get(f"/drop/{box}").status_code == 200
    upload(d1, b"1", "one.txt")
    upload(d2, b"2", "two.txt")
    assert flush(later(config.NOTIFY_QUIET_SECONDS + 5)) == 2
    assert sorted(s for s, _ in outbox) == ["[Drop] 1 file uploaded to Test box"] * 2


def test_files_deleted_during_the_wait_are_not_reported(app, make_box, outbox):
    d, *_ = joined(app, make_box)
    keep, drop = upload(d, b"1", "keep.txt").json(), upload(d, b"2", "oops.txt").json()
    assert d.delete(f"/api/files/{drop['id']}").status_code == 204
    flush(later(config.NOTIFY_QUIET_SECONDS + 5))
    (_, body), = outbox
    assert "keep.txt" in body and "oops.txt" not in body

    d2, *_ = joined(app, make_box)
    only = upload(d2, b"3", "gone.txt").json()
    d2.delete(f"/api/files/{only['id']}")
    outbox.clear()
    assert flush(later(config.NOTIFY_QUIET_SECONDS + 5)) == 0 and outbox == []


def test_a_busy_device_is_still_reported_after_the_max_wait(app, make_box, outbox):
    d, *_ = joined(app, make_box)
    upload(d, b"1", "a.txt")
    db = sqlite3.connect(config.DB_PATH)
    # a second upload that never finishes keeps the device "busy"
    db.execute("INSERT INTO uploaded_files (id, dropbox_id, session_id, original_filename, created_at)"
               " SELECT 'x', dropbox_id, session_id, 'big.iso', created_at FROM uploaded_files")
    db.commit()
    assert flush(later(config.NOTIFY_QUIET_SECONDS + 5)) == 0
    assert flush(later(config.NOTIFY_MAX_WAIT_SECONDS + 5)) == 1
    assert "a.txt" in outbox[0][1]


def test_a_failed_send_is_retried_and_logged(app, admin, make_box, outbox):
    d, *_ = joined(app, make_box)
    upload(d, b"1", "a.txt")
    due = later(config.NOTIFY_QUIET_SECONDS + 5)
    outbox.fail = True
    assert flush(due) == 0
    assert flush(due + timedelta(seconds=30)) == 0  # backing off
    outbox.fail = False
    assert flush(due + notifier.RETRY_AFTER + timedelta(seconds=1)) == 1
    types = [e["type"] for e in admin.get("/admin/api/events").json()["events"]]
    assert "notification_failed" in types and "notification_sent" in types


def test_uploads_from_before_the_feature_do_not_trigger_mail(tmp_path, monkeypatch, outbox):
    import db as dbmod
    path = tmp_path / "old.db"
    monkeypatch.setattr(config, "DB_PATH", str(path))
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE dropboxes (id TEXT PRIMARY KEY, gdrive_folder_id TEXT, name TEXT, password_hash TEXT,
            created_by TEXT, created_at TEXT, expires_at TEXT, onboard_window_hours INT, max_devices INT, is_revoked INT);
        CREATE TABLE device_sessions (id TEXT PRIMARY KEY, dropbox_id TEXT, created_at TEXT, is_revoked INT);
        CREATE TABLE uploaded_files (id TEXT PRIMARY KEY, dropbox_id TEXT, session_id TEXT, gdrive_file_id TEXT,
            sidecar_gdrive_id TEXT, original_filename TEXT, status TEXT NOT NULL DEFAULT 'uploading');
        CREATE TABLE admins (email TEXT PRIMARY KEY, google_id TEXT, refresh_token TEXT);
        INSERT INTO uploaded_files (id, dropbox_id, session_id, original_filename, status)
            VALUES ('f', 'b', 's', 'old.txt', 'completed');
    """)
    c.commit()
    c.close()
    dbmod.init_db()
    assert flush(later(10_000)) == 0 and outbox == []
