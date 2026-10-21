"""Device onboarding: decide whether a visitor may enter a dropbox."""
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class AccessDenied(Exception):
    pass


class PasswordRequired(AccessDenied):
    pass


class WrongPassword(AccessDenied):
    pass


def _is_live(box: sqlite3.Row) -> bool:
    return not box["is_revoked"] and utcnow() < _ts(box["expires_at"])


def session_context(db: sqlite3.Connection, cookie: str | None) -> sqlite3.Row | None:
    """The dropbox row (plus `session_id`) for a valid device cookie, or None."""
    if not cookie:
        return None
    row = db.execute(
        "SELECT d.*, s.id AS session_id FROM device_sessions s JOIN dropboxes d ON d.id = s.dropbox_id"
        " WHERE s.id = ? AND s.is_revoked = 0",
        (cookie,),
    ).fetchone()
    return row if row and _is_live(row) else None


def authorize(
    db: sqlite3.Connection, dropbox_id: str, cookie: str | None, password: str | None = None
) -> tuple[sqlite3.Row, str, bool]:
    """Return (dropbox, session_id, is_new_session) or raise AccessDenied.

    1. A valid, unrevoked device cookie for this dropbox grants access.
    2. Otherwise a new device is onboarded if the dropbox is live, still inside its
       onboarding window and under max_devices, and the password (if any) is right.
       A wrong password never consumes a device slot.
    """
    ctx = session_context(db, cookie)
    if ctx and ctx["id"] == dropbox_id:
        return ctx, ctx["session_id"], False

    box = db.execute("SELECT * FROM dropboxes WHERE id = ?", (dropbox_id,)).fetchone()
    if box is None or not _is_live(box):
        raise AccessDenied
    if utcnow() >= _ts(box["created_at"]) + timedelta(hours=box["onboard_window_hours"]):
        raise AccessDenied

    if box["password_hash"]:
        if password is None:
            raise PasswordRequired
        if not bcrypt.checkpw(password.encode(), box["password_hash"].encode()):
            raise WrongPassword

    # count and insert in one statement so concurrent visitors can't exceed the cap
    session_id = str(uuid.uuid4())
    cur = db.execute(
        "INSERT INTO device_sessions (id, dropbox_id, created_at) SELECT ?, ?, ?"
        " WHERE (SELECT COUNT(*) FROM device_sessions WHERE dropbox_id = ? AND is_revoked = 0) < ?",
        (session_id, dropbox_id, utcnow().isoformat(), dropbox_id, box["max_devices"]),
    )
    if cur.rowcount == 0:
        raise AccessDenied
    db.commit()
    return box, session_id, True
