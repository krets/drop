CREATE TABLE IF NOT EXISTS dropboxes (
    id                   TEXT PRIMARY KEY,
    gdrive_folder_id     TEXT NOT NULL,
    name                 TEXT NOT NULL,
    password_hash        TEXT,
    created_at           TEXT NOT NULL,
    expires_at           TEXT NOT NULL,
    onboard_window_hours INTEGER NOT NULL DEFAULT 48,
    max_devices          INTEGER NOT NULL DEFAULT 3,
    is_revoked           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS device_sessions (
    id          TEXT PRIMARY KEY,
    dropbox_id  TEXT NOT NULL REFERENCES dropboxes(id),
    created_at  TEXT NOT NULL,
    is_revoked  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS uploaded_files (
    id                TEXT PRIMARY KEY,
    dropbox_id        TEXT NOT NULL REFERENCES dropboxes(id),
    session_id        TEXT NOT NULL REFERENCES device_sessions(id),
    gdrive_file_id    TEXT,
    original_filename TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'uploading' CHECK (status IN ('uploading', 'completed'))
);
CREATE INDEX IF NOT EXISTS idx_files_session ON uploaded_files(session_id);

CREATE TABLE IF NOT EXISTS admins (
    email         TEXT PRIMARY KEY,
    google_id     TEXT,
    refresh_token TEXT
);
