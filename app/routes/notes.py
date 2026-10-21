"""Text the uploader writes: a note per file (`<name>.meta.txt`) and standalone comment files.

The text is kept in our database as well as written to Drive, so editing it later never needs to
read anything back out of Drive.
"""
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, StringConstraints

from db import get_db
from routes.public import MAX_TEXT, current_session, owned_file, refresh_token_for
from services import gdrive
from services.sessions import utcnow

router = APIRouter()


class Text(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TEXT)]


def _owned_comment(db, ctx, comment_id: str):
    row = db.execute(
        "SELECT * FROM comments WHERE id = ? AND session_id = ?", (comment_id, ctx["session_id"])
    ).fetchone()
    if row is None:
        raise HTTPException(404)
    return row


# --- standalone comments ---------------------------------------------------------------------

@router.get("/api/my-comments")
def my_comments(ctx=Depends(current_session), db=Depends(get_db)):
    rows = db.execute(
        "SELECT id, text, created_at FROM comments WHERE session_id = ? ORDER BY rowid DESC",
        (ctx["session_id"],),
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/comments", status_code=201)
async def create_comment(body: Text, ctx=Depends(current_session), db=Depends(get_db)):
    token = refresh_token_for(db, ctx)
    now = utcnow()
    try:
        file_id = await gdrive.upload_text(
            token, ctx["gdrive_folder_id"], f"Comment {now:%Y-%m-%d %H.%M.%S}.txt", body.text + "\n")
    except gdrive.DriveError:
        raise HTTPException(502, "Storage error")
    comment_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO comments (id, dropbox_id, session_id, gdrive_file_id, text, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (comment_id, ctx["id"], ctx["session_id"], file_id, body.text, now.isoformat()),
    )
    db.commit()
    return {"id": comment_id, "text": body.text, "created_at": now.isoformat()}


@router.patch("/api/comments/{comment_id}")
async def edit_comment(comment_id: str, body: Text, ctx=Depends(current_session), db=Depends(get_db)):
    row = _owned_comment(db, ctx, comment_id)
    try:
        await gdrive.update_text(refresh_token_for(db, ctx), row["gdrive_file_id"], body.text + "\n")
    except gdrive.DriveError:
        raise HTTPException(502, "Storage error")
    db.execute("UPDATE comments SET text = ? WHERE id = ?", (body.text, comment_id))
    db.commit()
    return {"id": comment_id, "text": body.text}


@router.delete("/api/comments/{comment_id}", status_code=204)
async def delete_comment(comment_id: str, ctx=Depends(current_session), db=Depends(get_db)):
    row = _owned_comment(db, ctx, comment_id)
    try:
        await gdrive.trash_file(refresh_token_for(db, ctx), row["gdrive_file_id"])
    except gdrive.DriveError:
        raise HTTPException(502, "Storage error")
    db.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    db.commit()


# --- per-file notes --------------------------------------------------------------------------

@router.put("/api/files/{file_id}/note")
async def set_note(file_id: str, body: Text, ctx=Depends(current_session), db=Depends(get_db)):
    row = owned_file(db, ctx, file_id)
    token = refresh_token_for(db, ctx)
    sidecar_id = row["sidecar_gdrive_id"]
    try:
        if sidecar_id:
            await gdrive.update_text(token, sidecar_id, body.text + "\n")
        else:
            sidecar_id = await gdrive.upload_text(
                token, ctx["gdrive_folder_id"], f"{row['original_filename']}.meta.txt", body.text + "\n")
    except gdrive.DriveError:
        raise HTTPException(502, "Storage error")
    db.execute("UPDATE uploaded_files SET sidecar_gdrive_id = ?, note = ? WHERE id = ?",
               (sidecar_id, body.text, file_id))
    db.commit()
    return {"id": file_id, "has_note": True, "note": body.text}


@router.delete("/api/files/{file_id}/note", status_code=204)
async def remove_note(file_id: str, ctx=Depends(current_session), db=Depends(get_db)):
    row = owned_file(db, ctx, file_id)
    if row["sidecar_gdrive_id"]:
        try:
            await gdrive.trash_file(refresh_token_for(db, ctx), row["sidecar_gdrive_id"])
        except gdrive.DriveError:
            raise HTTPException(502, "Storage error")
    db.execute("UPDATE uploaded_files SET sidecar_gdrive_id = NULL, note = NULL WHERE id = ?", (file_id,))
    db.commit()
