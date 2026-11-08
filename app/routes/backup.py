"""Backup shares: a bearer token lets a script PUT files into one Drive folder; the server prunes old ones."""
import hashlib
import secrets
import sqlite3
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints, model_validator
from starlette.requests import ClientDisconnect

import config
from db import get_db
from routes.admin import require_admin
from routes.public import clean_filename
from services import events, gdrive, heartbeat
from services.retention import select_keep
from services.sessions import utcnow

router = APIRouter()
admin_router = APIRouter(prefix="/admin")

TOKEN_PREFIX = "bk_"
KEEP_FIELDS = ("keep_daily", "keep_weekly", "keep_monthly", "keep_yearly")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _actor(b) -> str:
    return f"backup:{b['name']}"[:80]


def retention_of(b) -> dict:
    return {k: b[k] for k in KEEP_FIELDS}


# --- the script-facing API ---------------------------------------------------------------------

def current_backup(request: Request, authorization: str | None = Header(None), db=Depends(get_db)):
    token = authorization[7:].strip() if authorization and authorization[:7].lower() == "bearer " else ""
    row = db.execute("SELECT * FROM backups WHERE token_hash = ?", (hash_token(token),)).fetchone() if token else None
    if row is None or row["is_revoked"]:
        events.log(db, "backup_auth_failed", actor="visitor", request=request,
                   reason="revoked" if row else "unknown token", backup=row["name"] if row else None,
                   dropbox_id=row["id"] if row else None)
        raise HTTPException(401, "Invalid backup token", headers={"WWW-Authenticate": "Bearer"})
    return row


def _admin_token(db, b) -> str:
    row = db.execute("SELECT refresh_token FROM admins WHERE email = ?", (b["created_by"],)).fetchone()
    if not row or not row["refresh_token"]:
        raise HTTPException(503, "Storage is not available")
    return row["refresh_token"]


async def prune(db: sqlite3.Connection, b: sqlite3.Row, token: str, request=None) -> list[str]:
    """Move files outside the retention policy to the Drive trash. Returns the pruned filenames."""
    rows = db.execute(
        "SELECT id, gdrive_file_id, filename, size, created_at FROM backup_files"
        " WHERE backup_id = ? AND status = 'completed'", (b["id"],)).fetchall()
    keep = select_keep([(r["id"], datetime.fromisoformat(r["created_at"])) for r in rows],
                       *(b[k] for k in KEEP_FIELDS))
    pruned = []
    for r in rows:
        if r["id"] in keep:
            continue
        try:
            await gdrive.trash_file(token, r["gdrive_file_id"])
        except gdrive.DriveError as e:
            # leave it 'completed': the next upload tries again
            events.log(db, "backup_prune_failed", actor=_actor(b), dropbox_id=b["id"], request=request,
                       filename=r["filename"], error=f"{type(e).__name__}: {e}"[:200])
            continue
        db.execute("UPDATE backup_files SET status = 'pruned', pruned_at = ? WHERE id = ?",
                   (utcnow().isoformat(), r["id"]))
        db.commit()
        events.log(db, "backup_pruned", actor="system", dropbox_id=b["id"], request=request,
                   filename=r["filename"], size=r["size"], uploaded_at=r["created_at"])
        pruned.append(r["filename"])
    return pruned


@router.put("/api/backup/{filename:path}", status_code=201)
async def put_backup(filename: str, request: Request, b=Depends(current_backup), db=Depends(get_db)):
    """Body = the raw file (`curl -T file`). Streams to Drive without touching disk."""
    token = _admin_token(db, b)
    name = clean_filename(filename)
    mime = request.headers.get("content-type", "").split(";")[0].strip()
    if not mime or mime.startswith(("multipart/", "application/x-www-form-urlencoded")):
        mime = "application/octet-stream"
    up: gdrive.ResumableUpload | None = None
    record_id, file_id, size = str(uuid.uuid4()), None, 0
    created_at = utcnow().isoformat()
    try:
        db.execute("INSERT INTO backup_files (id, backup_id, filename, created_at) VALUES (?, ?, ?, ?)",
                   (record_id, b["id"], name, created_at))
        db.commit()  # don't hold SQLite's write lock for the length of the upload
        up = await gdrive.ResumableUpload.start(token, name, mime, b["gdrive_folder_id"])
        async for chunk in request.stream():
            if chunk:
                size += len(chunk)
                await up.write(chunk)
        if size == 0:
            raise HTTPException(400, "Empty body")
        file_id, up = await up.finish(), None
        db.execute("UPDATE backup_files SET gdrive_file_id = ?, size = ?, status = 'completed' WHERE id = ?",
                   (file_id, size, record_id))
        db.execute("UPDATE backups SET last_upload_at = ? WHERE id = ?", (utcnow().isoformat(), b["id"]))
        db.commit()
        events.log(db, "backup_uploaded", actor=_actor(b), dropbox_id=b["id"], request=request,
                   file_id=record_id, filename=name, size=size)
    except BaseException as e:
        if up:
            await up.abort()
        db.execute("DELETE FROM backup_files WHERE id = ? AND status = 'uploading'", (record_id,))
        db.commit()
        if isinstance(e, ClientDisconnect):
            events.log(db, "backup_aborted", actor=_actor(b), dropbox_id=b["id"], request=request,
                       filename=name, size=size)
            return Response(status_code=499)
        if not isinstance(e, HTTPException):
            events.log(db, "backup_failed", actor=_actor(b), dropbox_id=b["id"], request=request,
                       filename=name, size=size, error=f"{type(e).__name__}: {e}"[:200])
        if isinstance(e, gdrive.AuthError):
            heartbeat.nudge()
        if isinstance(e, gdrive.DriveError):
            raise HTTPException(502, "Storage error") from e
        raise
    pruned = await prune(db, b, token, request)
    return {"id": record_id, "filename": name, "size": size, "created_at": created_at, "pruned": pruned}


@router.get("/api/backup")
def list_backups(b=Depends(current_backup), db=Depends(get_db)):
    rows = db.execute(
        "SELECT id, filename, size, created_at FROM backup_files WHERE backup_id = ? AND status = 'completed'"
        " ORDER BY created_at DESC", (b["id"],)).fetchall()
    return {"name": b["name"], "retention": retention_of(b), "files": [dict(r) for r in rows]}


# --- admin -------------------------------------------------------------------------------------

Keep = Field(ge=0, le=3650)


class Retention(BaseModel):
    keep_daily: int = Keep
    keep_weekly: int = Keep
    keep_monthly: int = Keep
    keep_yearly: int = Keep

    @model_validator(mode="after")
    def _some(self):
        if not any(getattr(self, k) for k in KEEP_FIELDS):
            raise ValueError("keep at least one period")
        return self


class BackupIn(Retention):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    gdrive_folder_id: str
    gdrive_folder_path: str | None = Field(None, max_length=1000)
    keep_daily: int = Field(7, ge=0, le=3650)
    keep_weekly: int = Field(4, ge=0, le=3650)
    keep_monthly: int = Field(12, ge=0, le=3650)
    keep_yearly: int = Field(10, ge=0, le=3650)


class BackupAdjust(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    keep_daily: int | None = Field(None, ge=0, le=3650)
    keep_weekly: int | None = Field(None, ge=0, le=3650)
    keep_monthly: int | None = Field(None, ge=0, le=3650)
    keep_yearly: int | None = Field(None, ge=0, le=3650)

    @model_validator(mode="after")
    def _something(self):
        if all(v is None for v in self.model_dump().values()):
            raise ValueError("nothing to change")
        return self


def curl_example(token: str) -> str:
    return (f'curl -fsS -H "Authorization: Bearer {token}" -T backup.tar.gz '
            f'"{config.BASE_URL}/api/backup/myapp-$(date +%F).tar.gz"')


@admin_router.post("/api/backups", status_code=201)
async def create_backup(body: BackupIn, request: Request, admin=Depends(require_admin), db=Depends(get_db)):
    if not admin["refresh_token"]:
        raise HTTPException(409, "No Drive token stored; log in again")
    try:
        folder = await gdrive.get_folder(admin["refresh_token"], body.gdrive_folder_id)
    except gdrive.DriveError:
        raise HTTPException(400, "Folder not found or not accessible")
    backup_id, token = str(uuid.uuid4()), TOKEN_PREFIX + secrets.token_urlsafe(32)
    path = body.gdrive_folder_path or folder["name"]
    db.execute(
        "INSERT INTO backups (id, name, token_hash, gdrive_folder_id, gdrive_folder_path, created_by, created_at,"
        " keep_daily, keep_weekly, keep_monthly, keep_yearly) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (backup_id, body.name, hash_token(token), body.gdrive_folder_id, path, admin["email"],
         utcnow().isoformat(), *(getattr(body, k) for k in KEEP_FIELDS)))
    db.commit()
    events.log(db, "backup_created", actor=f"admin:{admin['email']}", dropbox_id=backup_id, request=request,
               name=body.name, folder=path, **{k: getattr(body, k) for k in KEEP_FIELDS})
    return {"id": backup_id, "token": token, "url": f"{config.BASE_URL}/api/backup/", "curl": curl_example(token)}


@admin_router.get("/api/backups")
def list_admin_backups(_=Depends(require_admin), db=Depends(get_db)):
    rows = db.execute(
        "SELECT b.id, b.name, b.gdrive_folder_id, b.gdrive_folder_path, b.created_at, b.is_revoked,"
        " b.last_upload_at, b.keep_daily, b.keep_weekly, b.keep_monthly, b.keep_yearly,"
        " (SELECT COUNT(*) FROM backup_files f WHERE f.backup_id = b.id AND f.status = 'completed') AS files,"
        " (SELECT COALESCE(SUM(size), 0) FROM backup_files f WHERE f.backup_id = b.id AND f.status = 'completed')"
        " AS bytes FROM backups b ORDER BY b.created_at DESC").fetchall()
    return [{**dict(r), "url": f"{config.BASE_URL}/api/backup/"} for r in rows]


@admin_router.patch("/api/backups/{backup_id}")
async def adjust_backup(backup_id: str, body: BackupAdjust, request: Request, admin=Depends(require_admin),
                        db=Depends(get_db)):
    b = db.execute("SELECT * FROM backups WHERE id = ?", (backup_id,)).fetchone()
    if b is None:
        raise HTTPException(404)
    if b["is_revoked"]:
        raise HTTPException(409, "This backup share is revoked")
    changes = body.model_dump(exclude_none=True)
    merged = {**{k: b[k] for k in KEEP_FIELDS}, **changes}
    if not any(merged[k] for k in KEEP_FIELDS):
        raise HTTPException(422, "Keep at least one period")
    name = changes.get("name", b["name"])
    db.execute("UPDATE backups SET name = ?, keep_daily = ?, keep_weekly = ?, keep_monthly = ?, keep_yearly = ?"
               " WHERE id = ?", (name, *(merged[k] for k in KEEP_FIELDS), backup_id))
    db.commit()
    events.log(db, "backup_adjusted", actor=f"admin:{admin['email']}", dropbox_id=backup_id, request=request, **changes)
    # a shorter policy applies at once, not at the next upload
    pruned = await prune(db, db.execute("SELECT * FROM backups WHERE id = ?", (backup_id,)).fetchone(),
                         admin["refresh_token"], request) if admin["refresh_token"] and set(changes) & set(KEEP_FIELDS) else []
    return {"id": backup_id, "name": name, **{k: merged[k] for k in KEEP_FIELDS}, "pruned": len(pruned)}


@admin_router.post("/api/backups/{backup_id}/revoke")
def revoke_backup(backup_id: str, request: Request, admin=Depends(require_admin), db=Depends(get_db)):
    cur = db.execute("UPDATE backups SET is_revoked = 1 WHERE id = ?", (backup_id,))
    if cur.rowcount == 0:
        raise HTTPException(404)
    db.commit()
    events.log(db, "backup_revoked", actor=f"admin:{admin['email']}", dropbox_id=backup_id, request=request)
    return {"ok": True}
