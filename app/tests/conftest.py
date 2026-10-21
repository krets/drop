import base64
import json
import re
import sqlite3
import uuid
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

import config
import main
from services import gdrive
from services.sessions import utcnow
from services.throttle import unlock_throttle

ALIGN = 256 * 1024


class FakeDrive:
    """Just enough of Drive v3 to check our use of it, including resumable-upload rules."""

    def __init__(self):
        self.files: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.chunks: list[tuple[int, str]] = []  # (size, content-range) of every upload PUT
        self.fail_next_put = 0  # respond 503 to this many PUTs
        self.fail_sidecar = False
        self.refreshes = 0

    def add_folder(self, name="Folder"):
        fid = uuid.uuid4().hex
        self.files[fid] = {"id": fid, "name": name, "mimeType": gdrive.FOLDER_MIME, "parents": ["root"]}
        return fid

    def named(self, name):
        return [f for f in self.files.values() if f["name"] == name]

    def handle(self, req: httpx.Request) -> httpx.Response:
        url, path = str(req.url), req.url.path
        if url.startswith(gdrive.TOKEN_URI):
            self.refreshes += 1
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        if path.startswith("/upload-session/"):
            return self._session(req, path.rsplit("/", 1)[1])
        assert req.headers.get("authorization") == "Bearer at", f"unauthenticated call to {url}"
        if path == "/upload/drive/v3/files":
            return self._upload(req)
        if path.startswith("/upload/drive/v3/files/") and req.method == "PATCH":
            f = self.files.get(path.rsplit("/", 1)[-1])
            if not f:
                return httpx.Response(404)
            assert req.url.params["uploadType"] == "media"
            f["text"] = req.content.decode()
            return httpx.Response(200, json={"id": f["id"]})
        fid = path.rsplit("/", 1)[-1]
        if req.method == "GET" and path == "/drive/v3/files":
            return httpx.Response(200, json={"files": [
                {"id": f["id"], "name": f["name"]} for f in self.files.values()
                if f["mimeType"] == gdrive.FOLDER_MIME]})
        if req.method == "GET":
            f = self.files.get(fid)
            return httpx.Response(200, json={k: v for k, v in f.items() if k != "content"}) if f else httpx.Response(404)
        if req.method == "PATCH":
            f = self.files.get(fid)
            if not f:
                return httpx.Response(404)
            f.update(json.loads(req.content))
            return httpx.Response(200, json={"id": f["id"], "name": f["name"]})
        return httpx.Response(400)

    def _upload(self, req):
        kind = req.url.params["uploadType"]
        if kind == "resumable":
            meta = json.loads(req.content)
            sid = uuid.uuid4().hex
            self.sessions[sid] = {"meta": meta, "mime": req.headers.get("x-upload-content-type"), "data": bytearray()}
            return httpx.Response(200, headers={"Location": f"https://upload.test/upload-session/{sid}"})
        assert kind == "multipart"
        if self.fail_sidecar:
            return httpx.Response(500)
        raw = req.content.decode()
        meta = json.loads(re.search(r"\r\n\r\n(\{.*?\})\r\n--", raw, re.S).group(1))
        text = re.search(r"text/plain; charset=UTF-8\r\n\r\n(.*)\r\n--[^\r\n]*--$", raw, re.S).group(1)
        fid = uuid.uuid4().hex
        self.files[fid] = {"id": fid, "name": meta["name"], "parents": meta["parents"], "text": text,
                           "mimeType": "text/plain"}
        return httpx.Response(200, json={"id": fid})

    def _session(self, req, sid):
        s = self.sessions.get(sid)
        if s is None:
            return httpx.Response(404)
        if req.method == "DELETE":
            del self.sessions[sid]
            return httpx.Response(499)
        if self.fail_next_put:
            self.fail_next_put -= 1
            return httpx.Response(503)
        rng = req.headers["content-range"]
        body = req.content
        if body:
            m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", rng)
            start, end, total = int(m[1]), int(m[2]), m[3]
            assert start == len(s["data"]), f"non-contiguous chunk: {rng} after {len(s['data'])} bytes"
            assert end - start + 1 == len(body)
            if total == "*":
                assert len(body) % ALIGN == 0, f"unaligned non-final chunk: {rng}"
            else:
                assert int(total) == end + 1
            s["data"] += body
            self.chunks.append((len(body), rng))
            complete = total != "*"
        else:
            m = re.fullmatch(r"bytes \*/(\d+|\*)", rng)
            complete = m[1] != "*" and int(m[1]) == len(s["data"])
            if m[1] != "*" and not complete:
                complete = False
        if complete:
            fid = uuid.uuid4().hex
            self.files[fid] = {"id": fid, "name": s["meta"]["name"], "parents": s["meta"]["parents"],
                               "mimeType": s["mime"], "content": bytes(s["data"])}
            del self.sessions[sid]
            return httpx.Response(200, json={"id": fid})
        headers = {"Range": f"bytes=0-{len(s['data']) - 1}"} if s["data"] else {}
        return httpx.Response(308, headers=headers)


@pytest.fixture
def drive(monkeypatch):
    fake = FakeDrive()
    gdrive.set_client(httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)))
    monkeypatch.setattr(gdrive.ResumableUpload, "CHUNK", ALIGN)  # small chunks so tests span several
    monkeypatch.setattr(gdrive.asyncio, "sleep", lambda _: _noop())
    yield fake
    gdrive.set_client(None)


async def _noop():
    pass


@pytest.fixture
def app(tmp_path, monkeypatch, drive):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(config, "ADMIN_EMAILS", ["admin@example.com"])
    unlock_throttle.clear()
    with TestClient(main.app) as c:
        db = sqlite3.connect(config.DB_PATH)
        db.execute("UPDATE admins SET google_id = 'g', refresh_token = 'rt'")
        db.commit()
        db.close()
        yield c


def signed_cookie(data: dict) -> str:
    return TimestampSigner(config.SESSION_SECRET).sign(base64.b64encode(json.dumps(data).encode())).decode()


@pytest.fixture
def admin(app):
    app.cookies.set("admin_session", signed_cookie({"admin_email": "admin@example.com"}))
    return app


@pytest.fixture
def make_box(app, drive):
    """Insert a dropbox straight into SQLite; returns its id."""
    def make(password=None, max_devices=3, window_hours=48, days=30, revoked=0):
        import bcrypt
        folder = drive.add_folder()
        box_id = str(uuid.uuid4())
        now = utcnow()
        pw = bcrypt.hashpw(password.encode(), bcrypt.gensalt(4)).decode() if password else None
        db = sqlite3.connect(config.DB_PATH)
        db.execute(
            "INSERT INTO dropboxes (id, gdrive_folder_id, name, password_hash, created_by, created_at,"
            " expires_at, onboard_window_hours, max_devices, is_revoked)"
            " VALUES (?, ?, 'Test box', ?, 'admin@example.com', ?, ?, ?, ?, ?)",
            (box_id, folder, pw, (now - timedelta(hours=0)).isoformat(),
             (now + timedelta(days=days)).isoformat(), window_hours, max_devices, revoked))
        db.commit()
        db.close()
        return box_id, folder
    return make


def new_device(app):
    """A fresh browser: its own cookie jar."""
    return TestClient(main.app, cookies={})


def multipart(parts: list[tuple[dict, bytes]], boundary="XBOUNDARYX") -> tuple[dict, bytes]:
    out = b""
    for disp, data in parts:
        head = f'Content-Disposition: form-data; name="{disp["name"]}"'
        if "filename" in disp:
            head += f'; filename="{disp["filename"]}"'
        if "type" in disp:
            head += f'\r\nContent-Type: {disp["type"]}'
        out += f"--{boundary}\r\n{head}\r\n\r\n".encode() + data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return {"Content-Type": f"multipart/form-data; boundary={boundary}"}, out
