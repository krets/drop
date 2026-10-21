import re
import uuid

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.requests import ClientDisconnect

import config
from db import get_db
from services import gdrive
from services.multipart_stream import boundary_of, stream_multipart
from services.sessions import AccessDenied, PasswordRequired, WrongPassword, authorize, session_context, utcnow
from services.throttle import unlock_throttle

router = APIRouter()

MAX_FIELD_BYTES = 16 * 1024
MAX_TEXT = 5000  # notes and comments, in characters
MIME_RE = re.compile(r"^[\w.+-]+/[\w.+-]+$")


def clean_filename(name: str) -> str:
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if c.isprintable()).strip()[:255]
    return name if name not in ("", ".", "..") else "unnamed"


def _set_device_cookie(resp: Response, session_id: str) -> None:
    resp.set_cookie(
        config.DEVICE_COOKIE, session_id,
        httponly=True, secure=config.BASE_URL.startswith("https"),
        samesite="lax", max_age=60 * 60 * 24 * 365,
    )


def current_session(device_session: str | None = Cookie(None), db=Depends(get_db)):
    ctx = session_context(db, device_session)
    if ctx is None:
        raise HTTPException(403, "Forbidden")
    return ctx


def refresh_token_for(db, ctx) -> str:
    row = db.execute("SELECT refresh_token FROM admins WHERE email = ?", (ctx["created_by"],)).fetchone()
    if not row or not row["refresh_token"]:
        raise HTTPException(503, "Storage is not available")
    return row["refresh_token"]


@router.get("/drop/{dropbox_id}")
def drop_page(dropbox_id: str, request: Request, device_session: str | None = Cookie(None), db=Depends(get_db)):
    try:
        _, session_id, is_new = authorize(db, dropbox_id, device_session, user_agent=request.headers.get("user-agent"))
    except PasswordRequired:
        return FileResponse("static/password.html")
    except AccessDenied:
        raise HTTPException(403, "Forbidden")
    resp = FileResponse("static/drop.html")
    if is_new:
        _set_device_cookie(resp, session_id)
    return resp


class Unlock(BaseModel):
    password: str


@router.post("/drop/{dropbox_id}/unlock")
def unlock(dropbox_id: str, body: Unlock, request: Request, device_session: str | None = Cookie(None),
           db=Depends(get_db)):
    key = f"{dropbox_id}:{request.client.host if request.client else ''}"
    if unlock_throttle.blocked(key):
        raise HTTPException(429, "Too many attempts; try again later")
    try:
        _, session_id, is_new = authorize(
            db, dropbox_id, device_session, body.password, user_agent=request.headers.get("user-agent"))
    except PasswordRequired:
        raise HTTPException(400)
    except WrongPassword:
        unlock_throttle.fail(key)
        raise HTTPException(401, "Wrong password")
    except AccessDenied:
        raise HTTPException(403, "Forbidden")
    resp = JSONResponse({"ok": True})
    if is_new:
        _set_device_cookie(resp, session_id)
    return resp


@router.get("/api/me")
def me(ctx=Depends(current_session)):
    return {"name": ctx["name"]}


@router.get("/api/my-files")
def my_files(ctx=Depends(current_session), db=Depends(get_db)):
    rows = db.execute(
        "SELECT id, original_filename AS filename, size, created_at,"
        " sidecar_gdrive_id IS NOT NULL AS has_note, COALESCE(note, '') AS note FROM uploaded_files"
        " WHERE session_id = ? AND status = 'completed' ORDER BY rowid DESC",
        (ctx["session_id"],),
    ).fetchall()
    return [{**r, "has_note": bool(r["has_note"])} for r in map(dict, rows)]


@router.post("/api/upload")
async def upload(request: Request, ctx=Depends(current_session), db=Depends(get_db)):
    boundary = boundary_of(request.headers.get("content-type", ""))
    if not boundary:
        raise HTTPException(400, "Expected multipart/form-data")
    token = refresh_token_for(db, ctx)

    fields: dict[str, str] = {}
    field_name, field_buf = None, bytearray()
    up: gdrive.ResumableUpload | None = None
    record_id = filename = created_at = None
    file_id = sidecar_id = None
    size = 0

    try:
        async for event in stream_multipart(request.stream(), boundary):
            kind = event[0]
            if kind == "begin":
                _, name, part_filename, mime = event
                if part_filename is None:
                    field_name, field_buf = name, bytearray()
                    continue
                if record_id:
                    raise HTTPException(400, "One file per request")
                filename = clean_filename(part_filename)
                mime = mime if mime and MIME_RE.match(mime) else "application/octet-stream"
                record_id = str(uuid.uuid4())
                created_at = utcnow().isoformat()
                db.execute(
                    "INSERT INTO uploaded_files (id, dropbox_id, session_id, original_filename, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (record_id, ctx["id"], ctx["session_id"], filename, created_at),
                )
                db.commit()  # don't hold SQLite's write lock for the length of the upload
                up = await gdrive.ResumableUpload.start(token, filename, mime, ctx["gdrive_folder_id"])
            elif kind == "data":
                if field_name is not None:
                    field_buf += event[1]
                    if len(field_buf) > MAX_FIELD_BYTES:
                        raise HTTPException(413, "Field too large")
                elif up:
                    size += len(event[1])
                    await up.write(event[1])
            else:  # end
                if field_name is not None:
                    fields[field_name] = field_buf.decode("utf-8", "replace")
                    if len(fields[field_name].strip()) > MAX_TEXT:  # fail before any file bytes go to Drive
                        raise HTTPException(413, "Note too long")
                    field_name = None
                elif up:
                    file_id = await up.finish()
                    up = None

        if not file_id:
            raise HTTPException(400, "No file")
        note = fields.get("note", "").strip()
        if note:
            sidecar_id = await gdrive.upload_text(
                token, ctx["gdrive_folder_id"], f"{filename}.meta.txt", note + "\n")
        db.execute(
            "UPDATE uploaded_files SET gdrive_file_id = ?, sidecar_gdrive_id = ?, note = ?, size = ?,"
            " status = 'completed' WHERE id = ?",
            (file_id, sidecar_id, note or None, size, record_id),
        )
        db.commit()
        return {"id": record_id, "filename": filename, "size": size, "created_at": created_at,
                "has_note": bool(note), "note": note}

    except BaseException as e:
        if up:
            await up.abort()
        if file_id:  # finished, but the sidecar failed: don't leave a half-complete upload behind
            try:
                await gdrive.trash_file(token, file_id)
            except gdrive.DriveError:
                pass
        if record_id:
            db.execute("DELETE FROM uploaded_files WHERE id = ?", (record_id,))
            db.commit()
        if isinstance(e, ClientDisconnect):
            return Response(status_code=499)
        if isinstance(e, gdrive.DriveError):
            raise HTTPException(502, "Storage error") from e
        if isinstance(e, ValueError):  # malformed multipart
            raise HTTPException(400, "Malformed upload") from e
        raise


def owned_file(db, ctx, file_id: str):
    row = db.execute(
        "SELECT * FROM uploaded_files WHERE id = ? AND session_id = ? AND status = 'completed'",
        (file_id, ctx["session_id"]),
    ).fetchone()
    if row is None:
        raise HTTPException(404)
    return row


class Rename(BaseModel):
    filename: str = Field(min_length=1, max_length=255)


@router.patch("/api/files/{file_id}")
async def rename_file(file_id: str, body: Rename, ctx=Depends(current_session), db=Depends(get_db)):
    row = owned_file(db, ctx, file_id)
    name = clean_filename(body.filename)
    token = refresh_token_for(db, ctx)
    try:
        await gdrive.rename_file(token, row["gdrive_file_id"], name)
        if row["sidecar_gdrive_id"]:
            await gdrive.rename_file(token, row["sidecar_gdrive_id"], f"{name}.meta.txt")
    except gdrive.DriveError:
        raise HTTPException(502, "Storage error")
    db.execute("UPDATE uploaded_files SET original_filename = ? WHERE id = ?", (name, file_id))
    db.commit()
    return {"id": file_id, "filename": name}


@router.delete("/api/files/{file_id}", status_code=204)
async def delete_file(file_id: str, ctx=Depends(current_session), db=Depends(get_db)):
    row = owned_file(db, ctx, file_id)
    token = refresh_token_for(db, ctx)
    try:
        await gdrive.trash_file(token, row["gdrive_file_id"])
        if row["sidecar_gdrive_id"]:
            await gdrive.trash_file(token, row["sidecar_gdrive_id"])
    except gdrive.DriveError:
        raise HTTPException(502, "Storage error")
    db.execute("DELETE FROM uploaded_files WHERE id = ?", (file_id,))
    db.commit()
