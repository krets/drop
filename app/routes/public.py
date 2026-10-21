from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

import config
from db import get_db
from services.sessions import AccessDenied, authorize

router = APIRouter()


@router.get("/drop/{dropbox_id}")
def drop_page(dropbox_id: str, device_session: str | None = Cookie(None), db=Depends(get_db)):
    try:
        _, session_id, is_new = authorize(db, dropbox_id, device_session)
    except AccessDenied:
        raise HTTPException(403, "Forbidden")
    resp = FileResponse("static/drop.html")
    if is_new:
        resp.set_cookie(
            config.DEVICE_COOKIE, session_id,
            httponly=True, secure=config.BASE_URL.startswith("https"),
            samesite="lax", max_age=60 * 60 * 24 * 365,
        )
    return resp


# TODO: password check (bcrypt) before authorize() when password_hash is set.
# TODO: POST /api/upload   - stream multipart -> gdrive.stream_upload, record uploaded_files row,
#                            write sidecar comment if a comment field is present.
# TODO: GET  /api/my-files - uploaded_files WHERE session_id = cookie (metadata only).
# TODO: PATCH /api/files/{id}, DELETE /api/files/{id} - verify session ownership, then Drive rename/trash.
