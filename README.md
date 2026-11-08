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
- `app/services/notifier.py` - batches finished uploads into one email per device
- `app/services/heartbeat.py` - checks the stored Google login and emails when it needs attention
- `app/services/events.py` - append-only audit log (`events` table, shown under Activity in the admin)
- `app/routes/backup.py` - backup shares: bearer-token `PUT /api/backup/{name}` for scripts, plus their admin API
- `app/services/retention.py` - which backups to keep (daily/weekly/monthly/yearly, by upload time)
- `app/services/mailer.py` - SMTP sender
- `app/static/` - frontend

## Run

    cp .env.example .env   # fill in
    docker compose up -d --build

## Multiple admins

`ADMIN_EMAILS` is a comma-separated list; each address signs in with its own Google account, and the Drive
login it stores is used only for that admin's shares. An admin sees and manages only the dropboxes and backup
shares they created, plus the audit events that concern them (`events.owner`). Events that belong to nobody
(e.g. a stranger's denied login) show up for the first admin in the list. Removing an address from the env
does not remove an existing admin; delete their row from `admins` to do that.

## Notifications and monitoring

Set `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` in `.env` (optional: `SMTP_FROM`, `SMTP_SECURITY`
= `starttls`/`ssl`/`none`, `NOTIFY_EMAILS`). Each admin is mailed at their own login address; `NOTIFY_EMAILS`, if set,
replaces that for the first admin only. Mail goes over SMTP rather
than the Gmail API so the "sign in to Google again" alert never depends on the login it reports on.

- **Upload notice**: one email per device, sent after `NOTIFY_QUIET_SECONDS` (120) without upload activity,
  and at the latest `NOTIFY_MAX_WAIT_SECONDS` (900) after its first file. Files deleted meanwhile are left out.
- **Heartbeat**: every `HEARTBEAT_INTERVAL_SECONDS` (900) the refresh token is exercised against Drive.
  A rejected login emails immediately; other failures email after `HEARTBEAT_FAILURES` (3) in a row. Alerts repeat
  every `ALERT_REMINDER_HOURS` (24) and a "restored" mail follows recovery. Signing in again re-checks at once.
- **Audit log**: logins, dropbox changes, device onboarding and revocation, failed passwords, every upload,
  rename and delete, Drive status changes and each email sent or failed. Rows are never edited or deleted by the app.

## Backup shares

For scripts rather than people. In the admin, create a backup share: pick a Drive folder and a retention
policy (default: last upload of each of 7 days, first of each of 4 weeks, 12 months and 10 years). You get a token once.

    curl -fsS -H "Authorization: Bearer $TOKEN" -T db.tar.gz "$BASE_URL/api/backup/myapp-$(date +%F).tar.gz"

- Streams straight to Drive using the stored Google login; the script holds no Google credentials or cookies.
- After each upload (and when the policy is edited) older files outside the policy move to the Drive trash.
  Pruning looks at upload time only, so file names are the script's business. The newest file is never pruned.
- `GET /api/backup` lists what is kept. The token can't delete; revoke it in the admin to cut it off.

## Local development

Compose merges a `docker-compose.override.yml` automatically. It is per-machine and gitignored:

    services:
      backend:
        networks: !reset []        # no external Traefik network locally
        labels: !reset []
        ports: ["8000:8000"]
        environment:
          - BASE_URL=http://localhost:8000
          - WATCHFILES_FORCE_POLLING=true
        volumes:
          - ./app:/srv             # edits show up without a rebuild
          - ./data:/data
        command: uvicorn main:app --host 0.0.0.0 --port 8000 --reload

    networks: !reset {}

Add `http://localhost:8000/auth/google/callback` to the OAuth client's redirect URIs, then
`docker compose up -d --build`.

## Test

    docker compose --profile test run --rm --build test
