import asyncio
import sqlite3

import config
from services import gdrive
from services import heartbeat


def run():
    asyncio.run(heartbeat.check_all())


def admin_row():
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    return c.execute("SELECT * FROM admins").fetchone()


def test_healthy_drive_marks_ok_and_sends_nothing(app, drive, outbox):
    run()
    row = admin_row()
    assert row["drive_status"] == "ok" and row["drive_ok_at"] and outbox == []


def test_revoked_login_alerts_once_then_reminds_and_finally_reports_recovery(app, drive, outbox, monkeypatch):
    run()
    drive.token_status = 400
    run()
    assert admin_row()["drive_status"] == "auth_required"
    (subject, body), = outbox
    assert "sign in again" in subject and f"{config.BASE_URL}/admin/" in body and "invalid_grant" in body

    run()
    assert len(outbox) == 1  # no repeat within the reminder period

    c = sqlite3.connect(config.DB_PATH)
    c.execute("UPDATE admins SET drive_alerted_at = '2000-01-01T00:00:00+00:00'")
    c.commit()
    run()
    assert len(outbox) == 2  # reminder

    drive.token_status = 200
    run()
    assert admin_row()["drive_status"] == "ok"
    assert outbox[-1][0] == "[Drop] Google Drive connection restored"
    run()
    assert len(outbox) == 3


def test_network_trouble_only_alerts_after_repeated_failures(app, drive, outbox):
    run()
    drive.offline = True
    for _ in range(config.HEARTBEAT_FAILURES - 1):
        run()
    assert admin_row()["drive_status"] == "ok" and outbox == []
    run()
    assert admin_row()["drive_status"] == "unreachable"
    assert outbox[-1][0] == "[Drop] Google Drive is unreachable"
    drive.offline = False
    run()
    assert admin_row()["drive_status"] == "ok" and admin_row()["drive_failures"] == 0


def test_failed_alert_email_is_retried_next_check(app, drive, outbox):
    drive.token_status = 401
    outbox.fail = True
    run()
    assert outbox == [] and admin_row()["drive_alerted_at"] is None
    outbox.fail = False
    run()
    assert len(outbox) == 1 and admin_row()["drive_alerted_at"]


def test_never_signed_in_is_not_an_alert(app, drive, outbox):
    c = sqlite3.connect(config.DB_PATH)
    c.execute("UPDATE admins SET refresh_token = NULL")
    c.commit()
    run()
    assert admin_row()["drive_status"] == "not_connected" and outbox == []


def test_status_endpoint_and_upload_auth_failure_are_visible(admin, make_box, drive, outbox):
    from tests.test_upload import joined, upload
    run()
    s = admin.get("/admin/api/status").json()
    assert s["drive"]["status"] == "ok" and s["email"]["configured"]
    d, *_ = joined(admin, make_box)
    drive.token_status = 400
    gdrive._tokens.clear()  # the cached access token has expired
    assert upload(d, b"x", "a.txt").status_code == 502
    run()
    assert admin.get("/admin/api/status").json()["drive"]["status"] == "auth_required"
    assert any(e["type"] == "upload_failed" for e in admin.get("/admin/api/events").json()["events"])


def test_test_email_button(admin, outbox):
    assert admin.post("/admin/api/notifications/test").status_code == 200
    assert outbox[-1][0] == "[Drop] Test notification"
    outbox.fail = True
    assert admin.post("/admin/api/notifications/test").status_code == 502
