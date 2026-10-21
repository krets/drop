from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

import config
from db import init_db
from routes import admin, auth, public


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    SessionMiddleware, secret_key=config.SESSION_SECRET, session_cookie="admin_session",
    https_only=config.BASE_URL.startswith("https"), same_site="lax", max_age=60 * 60 * 12,
)
app.include_router(auth.router)
app.include_router(public.router)
app.include_router(admin.router)
app.mount("/static", StaticFiles(directory="static"), name="static")
