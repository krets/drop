import os

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
DB_PATH = os.environ.get("DB_PATH", "./data/app.db")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-only-secret")

DEVICE_COOKIE = "device_session"
ADMIN_EMAILS = [e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()]


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


# --- email ---------------------------------------------------------------------------------
SMTP_SERVER = os.environ.get("SMTP_SERVER", "")
SMTP_PORT = _int("SMTP_PORT", 587)
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM") or SMTP_USER
# "starttls" (usually port 587), "ssl" (465) or "none"; defaults from the port
SMTP_SECURITY = os.environ.get("SMTP_SECURITY") or ("ssl" if SMTP_PORT == 465 else "starttls")
NOTIFY_EMAILS = [e.strip() for e in os.environ.get("NOTIFY_EMAILS", "").split(",") if e.strip()] or ADMIN_EMAILS

# --- background work -----------------------------------------------------------------------
BACKGROUND_TASKS = os.environ.get("BACKGROUND_TASKS", "1") != "0"
# an upload notice goes out once a device has been quiet this long, but never later than MAX_WAIT after its first file
NOTIFY_QUIET_SECONDS = _int("NOTIFY_QUIET_SECONDS", 120)
NOTIFY_MAX_WAIT_SECONDS = _int("NOTIFY_MAX_WAIT_SECONDS", 15 * 60)
HEARTBEAT_INTERVAL_SECONDS = _int("HEARTBEAT_INTERVAL_SECONDS", 15 * 60)
HEARTBEAT_FAILURES = _int("HEARTBEAT_FAILURES", 3)  # consecutive transient failures before "unreachable" alerts
ALERT_REMINDER_HOURS = _int("ALERT_REMINDER_HOURS", 24)
