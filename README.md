# gdrive-dropbox

A self-hosted, write-only "dropbox" in front of Google Drive. See [docs/dropbox-spec.md](docs/dropbox-spec.md).

Stack: Caddy, FastAPI, SQLite, Uppy.js, Google Drive API v3.

## Layout

- `app/main.py`, `config.py`, `db.py`, `schema.sql` - app wiring and SQLite schema
- `app/services/sessions.py` - device onboarding window / max-device logic
- `app/services/gdrive.py` - Drive wrapper (streaming upload, rename, trash)
- `app/routes/public.py` - `/drop/{id}` and the uploader API
- `app/routes/admin.py` - Google-OAuth-gated admin
- `app/static/` - frontend

## Run

    cp .env.example .env   # fill in
    docker compose up -d --build
