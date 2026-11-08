"""Append-only audit log. Every security-relevant action ends up here."""
import json
import logging
import sqlite3

from services.sessions import device_ref, utcnow

logger = logging.getLogger(__name__)

MAX_DETAIL = 2000


def ip_of(request) -> str | None:
    return request.client.host if request is not None and request.client else None


def owner_of(db: sqlite3.Connection, dropbox_id: str | None, actor: str) -> str | None:
    """The admin an event belongs to: the creator of the dropbox/backup it concerns, else the acting admin."""
    if dropbox_id:
        for table in ("dropboxes", "backups"):
            row = db.execute(f"SELECT created_by FROM {table} WHERE id = ?", (dropbox_id,)).fetchone()
            if row:
                return row[0]
    return actor[len("admin:"):] if actor.startswith("admin:") else None


def log(db: sqlite3.Connection, type: str, *, actor: str = "system", dropbox_id: str | None = None,
        session_id: str | None = None, request=None, owner: str | None = None, **detail) -> None:
    """Record an event and commit at once.

    Commits the caller's pending work too: handlers often raise right after logging (a denied
    visitor gets a 403), and the request's own commit never runs on that path. Never raises, so
    a failing log can't take a request down with it.
    """
    try:
        text = json.dumps({k: v for k, v in detail.items() if v is not None}, default=str)[:MAX_DETAIL]
        db.execute(
            "INSERT INTO events (ts, type, actor, dropbox_id, device_ref, ip, detail, owner)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (utcnow().isoformat(), type, actor, (dropbox_id or None) and dropbox_id[:64],
             device_ref(session_id) if session_id else None, ip_of(request), text,
             owner or owner_of(db, dropbox_id, actor)),
        )
        db.commit()
    except sqlite3.Error:
        logger.exception("could not write audit event %s", type)


def log_standalone(type: str, **kw) -> None:
    """For background tasks, which have no request connection."""
    import config
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    try:
        log(conn, type, **kw)
    finally:
        conn.close()
