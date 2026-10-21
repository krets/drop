from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

import config
from db import init_db
from services import gdrive
from routes import admin, auth, notes, public


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield
    await gdrive.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    SessionMiddleware, secret_key=config.SESSION_SECRET, session_cookie="admin_session",
    https_only=config.BASE_URL.startswith("https"), same_site="lax", max_age=60 * 60 * 12,
)
app.include_router(auth.router)
app.include_router(public.router)
app.include_router(notes.router)
app.include_router(admin.router)
app.mount("/static", StaticFiles(directory="static"), name="static")


# A bare page for browser navigations that fail, so nothing hints at what lives here.
BLANK_PAGE = (
    '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
    '<meta name="robots" content="noindex"><title></title>'
    '<style>:root{color-scheme:light dark}body{margin:0;min-height:100vh;display:grid;place-items:center;'
    'font:14px system-ui,sans-serif;color:#8b949e}</style><body>&middot;</body>'
)


@app.exception_handler(StarletteHTTPException)
async def blank_page_for_browsers(request, exc):
    if "text/html" in request.headers.get("accept", "") and not request.url.path.startswith("/api/"):
        return HTMLResponse(BLANK_PAGE, status_code=exc.status_code)
    return await http_exception_handler(request, exc)
