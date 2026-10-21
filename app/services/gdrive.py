"""Google Drive API v3 over httpx. Never reads file content back (no downloads)."""
import asyncio
import json
import time

import httpx

import config

TOKEN_URI = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveError(Exception):
    pass


_client: httpx.AsyncClient | None = None
_tokens: dict[str, tuple[str, float]] = {}


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0, write=120.0))
    return _client


def set_client(c: httpx.AsyncClient | None) -> None:
    global _client
    _client = c
    _tokens.clear()


async def close() -> None:
    if _client is not None:
        await _client.aclose()


async def access_token(refresh_token: str) -> str:
    cached = _tokens.get(refresh_token)
    if cached and cached[1] > time.monotonic() + 60:
        return cached[0]
    r = await client().post(TOKEN_URI, data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
    })
    if r.status_code != 200:
        raise DriveError("Could not refresh the Google access token")
    body = r.json()
    _tokens[refresh_token] = (body["access_token"], time.monotonic() + body.get("expires_in", 3600))
    return body["access_token"]


async def _api(refresh_token: str, method: str, url: str, **kw) -> httpx.Response:
    headers = {"Authorization": f"Bearer {await access_token(refresh_token)}", **kw.pop("headers", {})}
    r = await client().request(method, url, headers=headers, **kw)
    if r.status_code >= 400:
        raise DriveError(f"Drive API {r.status_code}")
    return r


async def list_folders(refresh_token: str, parent_id: str = "root") -> list[dict]:
    if not parent_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("invalid folder id")
    r = await _api(refresh_token, "GET", f"{API}/files", params={
        "q": f"mimeType='{FOLDER_MIME}' and '{parent_id}' in parents and trashed=false",
        "fields": "files(id, name)",
        "orderBy": "name",
        "pageSize": 200,
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    })
    return r.json()["files"]


async def get_folder(refresh_token: str, folder_id: str) -> dict:
    r = await _api(refresh_token, "GET", f"{API}/files/{folder_id}", params={
        "fields": "id, name, mimeType", "supportsAllDrives": "true"})
    info = r.json()
    if info["mimeType"] != FOLDER_MIME:
        raise DriveError("Not a folder")
    return info


async def folder_path(refresh_token: str, folder_id: str, max_depth: int = 12) -> str:
    """'My Drive / a / b' for a folder, walking up its parents. Stops early where Drive denies access."""
    names, current = [], folder_id
    for _ in range(max_depth):
        if not current.replace("-", "").replace("_", "").isalnum():
            raise ValueError("invalid folder id")
        try:
            r = await _api(refresh_token, "GET", f"{API}/files/{current}", params={
                "fields": "name, parents", "supportsAllDrives": "true"})
        except DriveError:
            if not names:
                raise
            break
        info = r.json()
        names.append(info["name"])
        if not info.get("parents"):
            break
        current = info["parents"][0]
    return " / ".join(reversed(names))


async def rename_file(refresh_token: str, file_id: str, name: str) -> None:
    await _api(refresh_token, "PATCH", f"{API}/files/{file_id}",
               params={"supportsAllDrives": "true"}, json={"name": name})


async def trash_file(refresh_token: str, file_id: str) -> None:
    await _api(refresh_token, "PATCH", f"{API}/files/{file_id}",
               params={"supportsAllDrives": "true"}, json={"trashed": True})


async def upload_text(refresh_token: str, folder_id: str, name: str, text: str) -> str:
    """Small single-request upload for notes (`<filename>.meta.txt`) and comment files."""
    boundary = "sidecar-boundary-7f3a"
    meta = json.dumps({"name": name, "parents": [folder_id]})
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
        f"--{boundary}\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n{text}\r\n--{boundary}--"
    ).encode()
    r = await _api(
        refresh_token, "POST", UPLOAD,
        params={"uploadType": "multipart", "supportsAllDrives": "true"},
        headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        content=body,
    )
    return r.json()["id"]


async def update_text(refresh_token: str, file_id: str, text: str) -> None:
    await _api(
        refresh_token, "PATCH", f"{UPLOAD}/{file_id}",
        params={"uploadType": "media", "supportsAllDrives": "true"},
        headers={"Content-Type": "text/plain; charset=UTF-8"},
        content=text.encode(),
    )


def _persisted(r: httpx.Response) -> int:
    """Bytes Drive has stored, from a 308's `Range: bytes=0-N` header (absent = none)."""
    rng = r.headers.get("Range")
    return int(rng.rsplit("-", 1)[1]) + 1 if rng else 0


class ResumableUpload:
    """Feeds a byte stream of unknown length into Drive's resumable upload protocol.

    Holds at most one chunk (plus whatever the last write added) in memory; nothing touches disk.
    """

    CHUNK = 8 * 1024 * 1024  # non-final chunks must be a multiple of 256 KiB
    ATTEMPTS = 5

    def __init__(self, session_url: str):
        self._url = session_url
        self._buf = bytearray()
        self._offset = 0  # bytes Drive has confirmed

    @classmethod
    async def start(cls, refresh_token: str, name: str, mime_type: str, parent_id: str) -> "ResumableUpload":
        r = await _api(
            refresh_token, "POST", UPLOAD,
            params={"uploadType": "resumable", "supportsAllDrives": "true"},
            headers={"X-Upload-Content-Type": mime_type},
            json={"name": name, "parents": [parent_id]},
        )
        return cls(r.headers["Location"])

    async def write(self, data: bytes) -> None:
        self._buf += data
        while len(self._buf) >= self.CHUNK:
            await self._advance(final=False)

    async def finish(self) -> str:
        """Send the remainder and return the new file's Drive id."""
        return (await self._advance(final=True))["id"]

    async def abort(self) -> None:
        try:
            await client().delete(self._url)
        except httpx.HTTPError:
            pass

    async def _send(self, content: bytes, content_range: str) -> httpx.Response | None:
        try:
            return await client().put(self._url, content=content, headers={"Content-Range": content_range})
        except httpx.TransportError:
            return None

    async def _advance(self, final: bool) -> dict | None:
        for attempt in range(self.ATTEMPTS):
            data = bytes(self._buf) if final else bytes(self._buf[: self.CHUNK])
            total = str(self._offset + len(self._buf)) if final else "*"
            end = self._offset + len(data) - 1
            r = await self._send(data, f"bytes {self._offset}-{end}/{total}" if data else f"bytes */{total}")

            if r is None or r.status_code >= 500:
                await asyncio.sleep(0.5 * 2**attempt)
                r = await self._send(b"", f"bytes */{total}")  # ask Drive where it is
                if r is None or r.status_code >= 500:
                    continue
            if r.status_code in (200, 201):
                self._buf.clear()
                return r.json()
            if r.status_code != 308:
                raise DriveError(f"Drive upload failed ({r.status_code})")

            persisted = min(max(_persisted(r), self._offset), self._offset + len(data))
            del self._buf[: persisted - self._offset]
            self._offset = persisted
            if not final:
                return None
        raise DriveError("Drive upload failed after retries")
