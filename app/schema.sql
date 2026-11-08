CREATE TABLE IF NOT EXISTS dropboxes (
    id                   TEXT PRIMARY KEY,
    gdrive_folder_id     TEXT NOT NULL,
    name                 TEXT NOT NULL,
    password_hash        TEXT,
    created_by           TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    expires_at           TEXT NOT NULL,
    onboard_window_hours INTEGER NOT NULL DEFAULT 48,
    max_devices          INTEGER NOT NULL DEFAULT 3,
    is_revoked           INTEGER NOT NULL DEFAULT 0,
    gdrive_folder_path   TEXT
);

CREATE TABLE IF NOT EXISTS device_sessions (
    id          TEXT PRIMARY KEY,
    dropbox_id  TEXT NOT NULL REFERENCES dropboxes(id),
    created_at  TEXT NOT NULL,
    is_revoked  INTEGER NOT NULL DEFAULT 0,
    user_agent  TEXT,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS uploaded_files (
    id                TEXT PRIMARY KEY,
    dropbox_id        TEXT NOT NULL REFERENCES dropboxes(id),
    session_id        TEXT NOT NULL REFERENCES device_sessions(id),
    gdrive_file_id    TEXT,
    sidecar_gdrive_id TEXT,
    original_filename TEXT NOT NULL,
    size              INTEGER,
    note              TEXT,
    created_at        TEXT,
    status            TEXT NOT NULL DEFAULT 'uploading' CHECK (status IN ('uploading', 'completed')),
    completed_at      TEXT,
    notified_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_session ON uploaded_files(session_id);

CREATE TABLE IF NOT EXISTS admins (
    email         TEXT PRIMARY KEY,
    google_id     TEXT,
    refresh_token TEXT,
    drive_status     TEXT NOT NULL DEFAULT 'unknown',
    drive_checked_at TEXT,
    drive_ok_at      TEXT,
    drive_error      TEXT,
    drive_failures   INTEGER NOT NULL DEFAULT 0,
    drive_alerted_at TEXT
);

CREATE TABLE IF NOT EXISTS comments (
    id             TEXT PRIMARY KEY,
    dropbox_id     TEXT NOT NULL REFERENCES dropboxes(id),
    session_id     TEXT NOT NULL REFERENCES device_sessions(id),
    gdrive_file_id TEXT NOT NULL,
    text           TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_session ON comments(session_id);

-- Append-only audit trail. No foreign keys: it must outlive the rows it describes.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    type        TEXT NOT NULL,
    actor       TEXT NOT NULL,
    dropbox_id  TEXT,
    device_ref  TEXT,
    ip          TEXT,
    detail      TEXT,
    owner       TEXT  -- the admin whose dropbox/backup this concerns; NULL = nobody's
);
CREATE INDEX IF NOT EXISTS idx_events_dropbox ON events(dropbox_id, id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, id);

-- Token-authenticated backup shares for scripts. The server prunes old files by upload time.
CREATE TABLE IF NOT EXISTS backups (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    token_hash         TEXT NOT NULL UNIQUE,
    gdrive_folder_id   TEXT NOT NULL,
    gdrive_folder_path TEXT,
    created_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    is_revoked         INTEGER NOT NULL DEFAULT 0,
    last_upload_at     TEXT,
    keep_daily         INTEGER NOT NULL DEFAULT 7,
    keep_weekly        INTEGER NOT NULL DEFAULT 0,
    keep_monthly       INTEGER NOT NULL DEFAULT 12,
    keep_yearly        INTEGER NOT NULL DEFAULT 10
);

CREATE TABLE IF NOT EXISTS backup_files (
    id             TEXT PRIMARY KEY,
    backup_id      TEXT NOT NULL REFERENCES backups(id),
    gdrive_file_id TEXT,
    filename       TEXT NOT NULL,
    size           INTEGER,
    created_at     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'uploading' CHECK (status IN ('uploading', 'completed', 'pruned')),
    pruned_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_backup_files ON backup_files(backup_id, status, created_at);
