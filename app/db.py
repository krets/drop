import sqlite3
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
        # an interrupted upload leaves a row that no Drive file backs
        conn.execute("DELETE FROM uploaded_files WHERE status = 'uploading'")
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
