from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

import config
from db import get_db

router = APIRouter()

oauth = OAuth()
oauth.register(
    name="google",
    client_id=config.GOOGLE_CLIENT_ID,
    client_secret=config.GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email https://www.googleapis.com/auth/drive"},
)


@router.get("/auth/google/login")
async def login(request: Request):
    # offline + consent so Google always returns a refresh token for Drive access
    return await oauth.google.authorize_redirect(
        request, f"{config.BASE_URL}/auth/google/callback",
        access_type="offline", prompt="consent",
    )


@router.get("/auth/google/callback")
async def callback(request: Request, db=Depends(get_db)):
    token = await oauth.google.authorize_access_token(request)
    info = token["userinfo"]
    email = info["email"].lower()
    admin = db.execute("SELECT email FROM admins WHERE email = ?", (email,)).fetchone()
    if not info.get("email_verified") or admin is None:
        raise HTTPException(403, "Not an authorized admin")
    db.execute(
        "UPDATE admins SET google_id = ?, refresh_token = COALESCE(?, refresh_token) WHERE email = ?",
        (info["sub"], token.get("refresh_token"), email),
    )
    db.commit()
    request.session.clear()
    request.session["admin_email"] = email
    return RedirectResponse("/admin/")


@router.get("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")
