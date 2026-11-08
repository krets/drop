"""Batches finished uploads into one email per device.

A notice goes out once a device has had no upload activity for NOTIFY_QUIET_SECONDS (so someone
still adding, renaming or deleting files isn't reported half-done), but no later than
NOTIFY_MAX_WAIT_SECONDS after the first unreported file. Files the uploader deletes during the
wait simply drop out of the notice.
"""
import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timedelta

import config
from services import events, mailer
from services.sessions import device_ref, utcnow

logger = logging.getLogger(__name__)

TICK_SECONDS = 20
RETRY_AFTER = timedelta(minutes=5)
_retry_at: dict[str, datetime] = {}  # session id -> don't retry before


def human_size(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _due(first: datetime, last: datetime, uploading: bool, now: datetime) -> bool:
    if now - first >= timedelta(seconds=config.NOTIFY_MAX_WAIT_SECONDS):
        return True
    return not uploading and now - last >= timedelta(seconds=config.NOTIFY_QUIET_SECONDS)


def compose(box, session, files) -> tuple[str, str]:
    total = sum(f["size"] or 0 for f in files)
    n = len(files)
    subject = f"[Drop] {n} file{'s' if n != 1 else ''} uploaded to {mailer.oneline(box['name'], 80)}"
    lines = [
        f"{n} file{'s were' if n != 1 else ' was'} uploaded to \"{mailer.oneline(box['name'], 200)}\" "
        f"({human_size(total)} total).",
        "",
        f"Drive folder: {box['gdrive_folder_path'] or box['gdrive_folder_id']}",
        f"Device:       {mailer.oneline(session['user_agent'] or 'unknown', 120)} (ref {device_ref(session['id'])})",
        f"Between:      {min(f['completed_at'] for f in files)} and {max(f['completed_at'] for f in files)} (UTC)",
        "",
        "Files:",
    ]
    for f in files[:50]:
        lines.append(f"  - {mailer.oneline(f['original_filename'], 255)} ({human_size(f['size'])})")
        if f["note"]:
            lines.append(f"      note: {mailer.oneline(f['note'], 300)}")
    if n > 50:
        lines.append(f"  ... and {n - 50} more")
    lines += ["", f"Admin: {config.BASE_URL}/admin/"]
    return subject, "\n".join(lines)


async def flush_once(now: datetime | None = None) -> int:
    """Send every notice that is due. Returns how many went out."""
    now = now or utcnow()
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    sent = 0
    try:
        groups = conn.execute(
            "SELECT session_id, MIN(completed_at) AS first, MAX(completed_at) AS last,"
            " EXISTS(SELECT 1 FROM uploaded_files u WHERE u.session_id = f.session_id AND u.status = 'uploading') AS uploading"
            " FROM uploaded_files f WHERE status = 'completed' AND notified_at IS NULL GROUP BY session_id"
        ).fetchall()
        for g in groups:
            sid = g["session_id"]
            if not _due(datetime.fromisoformat(g["first"]), datetime.fromisoformat(g["last"]), bool(g["uploading"]), now):
                continue
            if _retry_at.get(sid, now) > now:
                continue
            files = conn.execute(
                "SELECT * FROM uploaded_files WHERE session_id = ? AND status = 'completed' AND notified_at IS NULL"
                " ORDER BY completed_at, rowid", (sid,)).fetchall()
            session = conn.execute("SELECT * FROM device_sessions WHERE id = ?", (sid,)).fetchone()
            box = conn.execute("SELECT * FROM dropboxes WHERE id = ?", (files[0]["dropbox_id"],)).fetchone()
            subject, body = compose(box, session, files)
            to = config.notify_addresses(box["created_by"])
            try:
                await mailer.send(subject, body, to)
            except Exception as e:  # SMTP down, bad credentials, ...: try again later, keep the files pending
                _retry_at[sid] = now + RETRY_AFTER
                logger.warning("upload notification failed: %s", e)
                events.log(conn, "notification_failed", dropbox_id=box["id"], session_id=sid,
                           kind="uploads", files=len(files), error=f"{type(e).__name__}: {e}"[:300])
                continue
            _retry_at.pop(sid, None)
            ids = [f["id"] for f in files]
            conn.execute(f"UPDATE uploaded_files SET notified_at = ? WHERE id IN ({','.join('?' * len(ids))})",
                         (now.isoformat(), *ids))
            conn.commit()
            events.log(conn, "notification_sent", dropbox_id=box["id"], session_id=sid, kind="uploads",
                       files=len(files), to=to)
            sent += 1
    finally:
        conn.close()
    return sent


async def run_forever() -> None:
    if not mailer.configured():
        logger.warning("SMTP is not configured (SMTP_SERVER/SMTP_USER/SMTP_PASS): upload notices are disabled")
        return
    while True:
        try:
            await flush_once()
        except Exception:
            logger.exception("notifier tick failed")
        await asyncio.sleep(TICK_SECONDS)
