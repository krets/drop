import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config

SCHEMA = Path(__file__).with_name("schema.sql")


def init_db() -> None:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.executescript(SCHEMA.read_text())
        # databases created before these columns existed
        have = {row[1] for row in conn.execute("PRAGMA table_info(uploaded_files)")}
        for col, decl in (("size", "INTEGER"), ("created_at", "TEXT"), ("note", "TEXT")):
            if col not in have:
                conn.execute(f"ALTER TABLE uploaded_files ADD COLUMN {col} {decl}")
        if "gdrive_folder_path" not in {row[1] for row in conn.execute("PRAGMA table_info(dropboxes)")}:
            conn.execute("ALTER TABLE dropboxes ADD COLUMN gdrive_folder_path TEXT")
        have = {row[1] for row in conn.execute("PRAGMA table_info(device_sessions)")}
        for col in ("user_agent", "last_seen_at"):
            if col not in have:
                conn.execute(f"ALTER TABLE device_sessions ADD COLUMN {col} TEXT")
        have = {row[1] for row in conn.execute("PRAGMA table_info(uploaded_files)")}
        if "notified_at" not in have:
            conn.execute("ALTER TABLE uploaded_files ADD COLUMN completed_at TEXT")
            conn.execute("ALTER TABLE uploaded_files ADD COLUMN notified_at TEXT")
            # uploads from before notifications existed must not trigger a flood of emails
            now = datetime.now(timezone.utc).isoformat()
            conn.execute("UPDATE uploaded_files SET completed_at = COALESCE(created_at, ?),"
                         " notified_at = COALESCE(created_at, ?) WHERE status = 'completed'", (now, now))
        have = {row[1] for row in conn.execute("PRAGMA table_info(admins)")}
        for col, decl in (("drive_status", "TEXT NOT NULL DEFAULT 'unknown'"), ("drive_checked_at", "TEXT"),
                          ("drive_ok_at", "TEXT"), ("drive_error", "TEXT"),
                          ("drive_failures", "INTEGER NOT NULL DEFAULT 0"), ("drive_alerted_at", "TEXT")):
            if col not in have:
                conn.execute(f"ALTER TABLE admins ADD COLUMN {col} {decl}")
        if "owner" not in {row[1] for row in conn.execute("PRAGMA table_info(events)")}:
            conn.execute("ALTER TABLE events ADD COLUMN owner TEXT")
            conn.execute(
                "UPDATE events SET owner = COALESCE("
                "(SELECT created_by FROM dropboxes d WHERE d.id = events.dropbox_id),"
                " (SELECT created_by FROM backups b WHERE b.id = events.dropbox_id),"
                " CASE WHEN actor LIKE 'admin:%' THEN substr(actor, 7) END)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_owner ON events(owner, id)")
        # an interrupted upload leaves a row that no Drive file backs
        conn.execute("DELETE FROM uploaded_files WHERE status = 'uploading'")
        conn.execute("DELETE FROM backup_files WHERE status = 'uploading'")
        conn.executemany(
            "INSERT OR IGNORE INTO admins (email) VALUES (?)", [(e,) for e in config.ADMIN_EMAILS]
        )


def get_db():
    """FastAPI dependency: one connection per request."""
    # FastAPI may open this in one worker thread and use it in another; a connection is never shared
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
