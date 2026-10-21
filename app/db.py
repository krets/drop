import sqlite3
from pathlib import Path

import config

SCHEMA = Path(__file__).with_name("schema.sql")


def init_db() -> None:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.executescript(SCHEMA.read_text())
        conn.executemany(
            "INSERT OR IGNORE INTO admins (email) VALUES (?)", [(e,) for e in config.ADMIN_EMAILS]
        )


def get_db():
    """FastAPI dependency: one connection per request."""
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
