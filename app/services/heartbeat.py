"""Periodically proves the stored Google login still works, and emails when it doesn't.

Status per admin: unknown -> ok | auth_required | unreachable | not_connected.
  auth_required  Google refused our refresh token: the admin must sign in again. Alerts at once.
  unreachable    HEARTBEAT_FAILURES checks in a row failed for another reason (network, Google 5xx).
An alert repeats every ALERT_REMINDER_HOURS while the problem lasts, and a "restored" mail follows recovery.
"""
import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timedelta

import httpx

import config
from services import events, gdrive, mailer
from services.sessions import utcnow

logger = logging.getLogger(__name__)

PROBLEMS = ("auth_required", "unreachable")
MIN_GAP = 60  # seconds between checks, however often something nudges us
_nudge: asyncio.Event | None = None


def nudge() -> None:
    """Ask for a check soon (e.g. an upload just hit an auth error)."""
    if _nudge is not None:
        _nudge.set()


async def _alert(conn, admin, status: str, error: str | None) -> bool:
    login = f"{config.BASE_URL}/admin/"
    to = config.notify_addresses(admin["email"])
    if status == "auth_required":
        subject = "[Drop] Google Drive needs you to sign in again"
        body = (f"The dropbox service can no longer reach Google Drive as {admin['email']}.\n"
                f"Reason: {error}\n\nUploads fail until you sign in again: {login}\n")
    else:
        subject = "[Drop] Google Drive is unreachable"
        body = (f"The last {config.HEARTBEAT_FAILURES} Drive connectivity checks for {admin['email']} failed.\n"
                f"Last error: {error}\n\nThis is usually temporary. If it persists, check {login}\n")
    try:
        await mailer.send(subject, body, to)
    except Exception as e:
        logger.warning("could not send drive alert: %s", e)
        events.log(conn, "notification_failed", owner=admin["email"], kind="drive_alert",
                   error=f"{type(e).__name__}: {e}"[:300])
        return False
    events.log(conn, "notification_sent", owner=admin["email"], kind="drive_alert", status=status, to=to)
    return True


async def check_admin(conn: sqlite3.Connection, admin: sqlite3.Row, now: datetime | None = None) -> str:
    """Run one check for one admin row, record the outcome, send any alert. Returns the new status."""
    now = now or utcnow()
    prev, failures, alerted = admin["drive_status"], admin["drive_failures"], admin["drive_alerted_at"]
    error, status = None, prev

    if not admin["refresh_token"]:
        status, error = "not_connected", "Never signed in with Google"
    else:
        try:
            await gdrive.check(admin["refresh_token"])
            status, failures = "ok", 0
        except gdrive.AuthError as e:
            status, error, failures = "auth_required", str(e), 0
        except (gdrive.DriveError, httpx.HTTPError) as e:
            failures += 1
            error = str(e) or type(e).__name__
            if failures >= config.HEARTBEAT_FAILURES and prev != "auth_required":
                status = "unreachable"

    if status != prev:
        events.log(conn, "drive_status_changed", actor="system", owner=admin["email"], old=prev, new=status, error=error)
        if prev in PROBLEMS and status not in PROBLEMS and alerted and mailer.configured():
            to = config.notify_addresses(admin["email"])
            try:
                await mailer.send("[Drop] Google Drive connection restored",
                                  f"The dropbox service can reach Google Drive as {admin['email']} again.\n",
                                  to)
                events.log(conn, "notification_sent", owner=admin["email"], kind="drive_restored", to=to)
            except Exception as e:
                events.log(conn, "notification_failed", owner=admin["email"], kind="drive_restored", error=f"{type(e).__name__}: {e}"[:300])
        alerted = None  # a new state is a new alert (and recovery clears it)

    if status in PROBLEMS:
        remind = timedelta(hours=config.ALERT_REMINDER_HOURS)
        if (alerted is None or now - datetime.fromisoformat(alerted) >= remind) and mailer.configured():
            if await _alert(conn, admin, status, error):
                alerted = now.isoformat()

    conn.execute(
        "UPDATE admins SET drive_status = ?, drive_checked_at = ?, drive_ok_at = CASE WHEN ? = 'ok' THEN ? ELSE drive_ok_at END,"
        " drive_error = ?, drive_failures = ?, drive_alerted_at = ? WHERE email = ?",
        (status, now.isoformat(), status, now.isoformat(), error, failures, alerted, admin["email"]),
    )
    conn.commit()
    return status


async def check_all() -> None:
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        for admin in conn.execute("SELECT * FROM admins").fetchall():
            await check_admin(conn, admin)
    finally:
        conn.close()


async def run_forever() -> None:
    global _nudge
    _nudge = asyncio.Event()
    await asyncio.sleep(5)  # let the app finish starting
    last = 0.0
    while True:
        wait = max(0.0, MIN_GAP - (time.monotonic() - last))
        await asyncio.sleep(wait)
        try:
            await check_all()
        except Exception:
            logger.exception("heartbeat failed")
        last = time.monotonic()
        _nudge.clear()
        try:
            await asyncio.wait_for(_nudge.wait(), config.HEARTBEAT_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
