from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse
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


# Failed browser navigations get a little arcade game rather than an error, so the site doesn't look like it hides anything.
GAME_PAGE = (Path(__file__).parent / "static" / "fall.html").read_text()


@app.exception_handler(StarletteHTTPException)
async def game_for_browsers(request, exc):
    if "text/html" in request.headers.get("accept", "") and not request.url.path.startswith("/api/"):
        return HTMLResponse(GAME_PAGE, status_code=exc.status_code)
    return await http_exception_handler(request, exc)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse("static/favicon.ico")
