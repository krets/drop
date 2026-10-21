import asyncio
import uuid
from datetime import timedelta

import bcrypt
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

import config
from db import get_db
from services import gdrive
from services.sessions import utcnow

router = APIRouter(prefix="/admin")


def require_admin(request: Request, db=Depends(get_db)):
    email = request.session.get("admin_email")
    admin = email and db.execute("SELECT * FROM admins WHERE email = ?", (email,)).fetchone()
    if not admin:
        raise HTTPException(401, "Admin login required")
    return admin


def _token(admin) -> str:
    if not admin["refresh_token"]:
        raise HTTPException(409, "No Drive token stored; log in again")
    return admin["refresh_token"]


@router.get("/")
def page(request: Request):
    if not request.session.get("admin_email"):
        return RedirectResponse("/auth/google/login")
    return FileResponse("static/admin.html")


@router.get("/api/me")
def me(admin=Depends(require_admin)):
    return {"email": admin["email"]}


@router.get("/api/folders")
async def folders(parent: str = "root", admin=Depends(require_admin)):
    try:
        return await gdrive.list_folders(_token(admin), parent)
    except ValueError:
        raise HTTPException(400, "Invalid folder id")
    except gdrive.DriveError:
        raise HTTPException(502, "Google Drive error")


class DropboxIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    gdrive_folder_id: str
    gdrive_folder_path: str | None = Field(None, max_length=1000)  # breadcrumb shown in the admin list
    password: str | None = None
    expires_in_days: int = Field(30, ge=1, le=365)
    onboard_window_hours: int = Field(48, ge=1, le=24 * 30)
    max_devices: int = Field(3, ge=1, le=50)


def helper_text(dropbox_id: str, password: str | None, days: int, hours: int, devices: int) -> str:
    lines = [f"Hey! Please upload your files securely here: {config.BASE_URL}/drop/{dropbox_id}"]
    if password:
        lines.append(f"* Password: {password}")
    lines.append(
        f"* Note: This link expires in {days} days, but you must open it on your devices "
        f"within {hours} hours. Max {devices} devices."
    )
    return "\n".join(lines)


@router.post("/api/dropboxes", status_code=201)
async def create_dropbox(body: DropboxIn, admin=Depends(require_admin), db=Depends(get_db)):
    try:
        folder = await gdrive.get_folder(_token(admin), body.gdrive_folder_id)  # must exist and be a folder
    except gdrive.DriveError:
        raise HTTPException(400, "Folder not found or not accessible")

    now = utcnow()
    dropbox_id = str(uuid.uuid4())
    pw_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode() if body.password else None
    db.execute(
        "INSERT INTO dropboxes (id, gdrive_folder_id, name, password_hash, created_by, created_at,"
        " expires_at, onboard_window_hours, max_devices, gdrive_folder_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (dropbox_id, body.gdrive_folder_id, body.name, pw_hash, admin["email"], now.isoformat(),
         (now + timedelta(days=body.expires_in_days)).isoformat(),
         body.onboard_window_hours, body.max_devices, body.gdrive_folder_path or folder["name"]),
    )
    db.commit()
    return {
        "id": dropbox_id,
        "url": f"{config.BASE_URL}/drop/{dropbox_id}",
        "helper_text": helper_text(dropbox_id, body.password, body.expires_in_days,
                                   body.onboard_window_hours, body.max_devices),
    }


async def _resolve_path(token: str | None, folder_id: str) -> str | None:
    """Best effort: a failed lookup leaves the row without a path so the UI can fall back."""
    if not token:
        return None
    try:
        return await gdrive.folder_path(token, folder_id)
    except (gdrive.DriveError, ValueError, httpx.HTTPError):
        return None


@router.get("/api/dropboxes")
async def list_dropboxes(admin=Depends(require_admin), db=Depends(get_db)):
    rows = db.execute(
        "SELECT d.id, d.name, d.gdrive_folder_id, d.created_at, d.expires_at, d.onboard_window_hours,"
        " d.max_devices, d.is_revoked, d.gdrive_folder_path, d.password_hash IS NOT NULL AS has_password,"
        " (SELECT COUNT(*) FROM device_sessions s WHERE s.dropbox_id = d.id AND s.is_revoked = 0) AS devices,"
        " (SELECT COUNT(*) FROM uploaded_files f WHERE f.dropbox_id = d.id AND f.status = 'completed') AS files"
        " FROM dropboxes d ORDER BY d.created_at DESC"
    ).fetchall()
    out = [{**dict(r), "url": f"{config.BASE_URL}/drop/{r['id']}"} for r in rows]
    # dropboxes created before the path was stored: look it up once and cache it
    legacy = [d for d in out if d["gdrive_folder_path"] is None]
    paths = await asyncio.gather(*(_resolve_path(admin["refresh_token"], d["gdrive_folder_id"]) for d in legacy))
    for d, path in zip(legacy, paths):
        if path:
            d["gdrive_folder_path"] = path
            db.execute("UPDATE dropboxes SET gdrive_folder_path = ? WHERE id = ?", (path, d["id"]))
    db.commit()
    return out


@router.post("/api/dropboxes/{dropbox_id}/revoke")
def revoke_dropbox(dropbox_id: str, _=Depends(require_admin), db=Depends(get_db)):
    cur = db.execute("UPDATE dropboxes SET is_revoked = 1 WHERE id = ?", (dropbox_id,))
    if cur.rowcount == 0:
        raise HTTPException(404)
    db.commit()
    return {"ok": True}


# TODO: revoke individual device sessions.
