"""Device onboarding: decide whether a visitor may enter a dropbox."""
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class AccessDenied(Exception):
    pass


def authorize(db: sqlite3.Connection, dropbox_id: str, cookie: str | None) -> tuple[sqlite3.Row, str, bool]:
    """Return (dropbox, session_id, is_new_session) or raise AccessDenied.

    1. A valid, unrevoked device cookie for this dropbox grants access.
    2. Otherwise a new device is onboarded if the dropbox is live, still
       inside its onboarding window, and under max_devices.
    """
    box = db.execute("SELECT * FROM dropboxes WHERE id = ?", (dropbox_id,)).fetchone()
    if box is None or box["is_revoked"] or utcnow() >= _ts(box["expires_at"]):
        raise AccessDenied

    if cookie:
        row = db.execute(
            "SELECT id FROM device_sessions WHERE id = ? AND dropbox_id = ? AND is_revoked = 0",
            (cookie, dropbox_id),
        ).fetchone()
        if row:
            return box, row["id"], False

    window_end = _ts(box["created_at"]) + timedelta(hours=box["onboard_window_hours"])
    if utcnow() >= window_end:
        raise AccessDenied

    (count,) = db.execute(
        "SELECT COUNT(*) FROM device_sessions WHERE dropbox_id = ? AND is_revoked = 0",
        (dropbox_id,),
    ).fetchone()
    if count >= box["max_devices"]:
        raise AccessDenied

    session_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO device_sessions (id, dropbox_id, created_at) VALUES (?, ?, ?)",
        (session_id, dropbox_id, utcnow().isoformat()),
    )
    return box, session_id, True
