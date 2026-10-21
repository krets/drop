import asyncio
import hashlib
import math
import uuid
from datetime import datetime, timedelta
from typing import Annotated

import bcrypt
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field, StringConstraints, model_validator

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
    out = [{**dict(r), "url": f"{config.BASE_URL}/drop/{r['id']}",
            "onboard_ends_at": (datetime.fromisoformat(r["created_at"])
                                + timedelta(hours=r["onboard_window_hours"])).isoformat()} for r in rows]
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



class Adjust(BaseModel):
    """Extensions add to the current end (or to now, if it has already passed); max_devices is absolute."""
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    extend_expiry_days: int | None = Field(None, ge=1, le=365)
    extend_onboarding_hours: int | None = Field(None, ge=1, le=24 * 30)
    max_devices: int | None = Field(None, ge=1, le=50)

    @model_validator(mode="after")
    def _something(self):
        if all(v is None for v in (self.name, self.extend_expiry_days, self.extend_onboarding_hours, self.max_devices)):
            raise ValueError("nothing to change")
        return self


@router.patch("/api/dropboxes/{dropbox_id}")
def adjust_dropbox(dropbox_id: str, body: Adjust, _=Depends(require_admin), db=Depends(get_db)):
    box = db.execute("SELECT * FROM dropboxes WHERE id = ?", (dropbox_id,)).fetchone()
    if box is None:
        raise HTTPException(404)
    if box["is_revoked"]:
        raise HTTPException(409, "This dropbox is revoked")
    now = utcnow()
    name, expires_at = body.name or box["name"], box["expires_at"]
    hours, devices = box["onboard_window_hours"], box["max_devices"]
    if body.extend_expiry_days:
        expires_at = (max(datetime.fromisoformat(expires_at), now) + timedelta(days=body.extend_expiry_days)).isoformat()
    if body.extend_onboarding_hours:
        created = datetime.fromisoformat(box["created_at"])
        new_end = max(created + timedelta(hours=hours), now) + timedelta(hours=body.extend_onboarding_hours)
        hours = math.ceil((new_end - created).total_seconds() / 3600)  # stored as hours since creation
    if body.max_devices:
        devices = body.max_devices
    db.execute("UPDATE dropboxes SET name = ?, expires_at = ?, onboard_window_hours = ?, max_devices = ? WHERE id = ?",
               (name, expires_at, hours, devices, dropbox_id))
    db.commit()
    return {"id": dropbox_id, "name": name, "expires_at": expires_at, "onboard_window_hours": hours,
            "max_devices": devices}


def _device_ref(session_id: str) -> str:
    """Public handle for a device. The session id itself is the device's login cookie, so never expose it."""
    return hashlib.sha256(session_id.encode()).hexdigest()[:16]


@router.get("/api/dropboxes/{dropbox_id}/devices")
def list_devices(dropbox_id: str, _=Depends(require_admin), db=Depends(get_db)):
    if db.execute("SELECT 1 FROM dropboxes WHERE id = ?", (dropbox_id,)).fetchone() is None:
        raise HTTPException(404)
    rows = db.execute(
        "SELECT s.id, s.created_at, s.last_seen_at, s.user_agent, s.is_revoked,"
        " (SELECT COUNT(*) FROM uploaded_files f WHERE f.session_id = s.id AND f.status = 'completed') AS files,"
        " (SELECT COUNT(*) FROM comments c WHERE c.session_id = s.id) AS comments"
        " FROM device_sessions s WHERE s.dropbox_id = ? ORDER BY s.created_at",
        (dropbox_id,),
    ).fetchall()
    return [{"ref": _device_ref(r["id"]), **{k: r[k] for k in r.keys() if k != "id"}} for r in rows]


@router.post("/api/dropboxes/{dropbox_id}/devices/{ref}/revoke")
def revoke_device(dropbox_id: str, ref: str, _=Depends(require_admin), db=Depends(get_db)):
    ids = [r["id"] for r in db.execute("SELECT id FROM device_sessions WHERE dropbox_id = ?", (dropbox_id,))]
    match = next((i for i in ids if _device_ref(i) == ref), None)
    if match is None:
        raise HTTPException(404)
    db.execute("UPDATE device_sessions SET is_revoked = 1 WHERE id = ?", (match,))
    db.commit()
    return {"ok": True}
