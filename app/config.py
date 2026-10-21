import os

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
DB_PATH = os.environ.get("DB_PATH", "./data/app.db")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-only-secret")

DEVICE_COOKIE = "device_session"
ADMIN_EMAILS = [e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()]
