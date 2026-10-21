# gdrive-dropbox

A self-hosted, write-only "dropbox" in front of Google Drive. See [docs/dropbox-spec.md](docs/dropbox-spec.md).

Stack: Traefik (external), FastAPI, SQLite, Uppy.js, Google Drive API v3.

## Layout

- `app/main.py`, `config.py`, `db.py`, `schema.sql` - app wiring and SQLite schema
- `app/services/sessions.py` - device onboarding window / max-device logic
- `app/services/gdrive.py` - Drive wrapper over httpx (resumable streaming upload, rename, trash)
- `app/services/multipart_stream.py` - incremental multipart parser, so uploads never touch disk
- `app/routes/public.py` - `/drop/{id}`, password unlock, and the uploader API
- `app/tests/` - pytest suite against a fake Drive
- `app/routes/admin.py` - Google-OAuth-gated admin
- `app/static/` - frontend

## Run

    cp .env.example .env   # fill in
    docker compose up -d --build

## Test

    docker compose --profile test run --rm --build test
