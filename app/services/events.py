"""Append-only audit log. Every security-relevant action ends up here."""
import json
import logging
import sqlite3

from services.sessions import device_ref, utcnow

logger = logging.getLogger(__name__)

MAX_DETAIL = 2000


def ip_of(request) -> str | None:
    return request.client.host if request is not None and request.client else None


def log(db: sqlite3.Connection, type: str, *, actor: str = "system", dropbox_id: str | None = None,
        session_id: str | None = None, request=None, **detail) -> None:
    """Record an event and commit at once.

    Commits the caller's pending work too: handlers often raise right after logging (a denied
    visitor gets a 403), and the request's own commit never runs on that path. Never raises, so
    a failing log can't take a request down with it.
    """
    try:
        text = json.dumps({k: v for k, v in detail.items() if v is not None}, default=str)[:MAX_DETAIL]
        db.execute(
            "INSERT INTO events (ts, type, actor, dropbox_id, device_ref, ip, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (utcnow().isoformat(), type, actor, (dropbox_id or None) and dropbox_id[:64],
             device_ref(session_id) if session_id else None, ip_of(request), text),
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
