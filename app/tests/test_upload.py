import os
import sqlite3

import config
from services import gdrive
from tests.conftest import ALIGN, multipart, new_device


def joined(app, make_box, **kw):
    box, folder = make_box(**kw)
    d = new_device(app)
    assert d.get(f"/drop/{box}").status_code == 200
    return d, box, folder


def upload(d, data: bytes, filename="a.bin", note=None, mime="application/octet-stream"):
    parts = []
    if note is not None:
        parts.append(({"name": "note"}, note.encode()))
    parts.append(({"name": "file", "filename": filename, "type": mime}, data))
    headers, body = multipart(parts)
    return d.post("/api/upload", content=body, headers=headers)


def content_of(drive, file_id):
    return drive.files[file_id]["content"]


def test_small_upload_lands_in_folder_and_is_listed(app, make_box, drive):
    d, _, folder = joined(app, make_box)
    r = upload(d, b"hello", "hi.txt", mime="text/plain")
    assert r.status_code == 200, r.text
    (f,) = drive.named("hi.txt")
    assert f["parents"] == [folder] and f["content"] == b"hello" and f["mimeType"] == "text/plain"
    (listed,) = d.get("/api/my-files").json()
    assert r.json() == listed  # the response is a full list item, so the page needn't re-fetch
    assert listed | {"created_at": None} == {
        "id": r.json()["id"], "filename": "hi.txt", "size": 5, "created_at": None, "has_note": False, "note": ""}
    assert listed["created_at"]


def test_large_upload_is_chunked_and_intact(app, make_box, drive):
    d, *_ = joined(app, make_box)
    data = os.urandom(ALIGN * 4 + 123)
    assert upload(d, data, "big.bin").status_code == 200
    (f,) = drive.named("big.bin")
    assert f["content"] == data
    assert [n for n, _ in drive.chunks] == [ALIGN] * 4 + [123]  # nothing buffered beyond a chunk


def test_exact_chunk_multiple_and_empty_file(app, make_box, drive):
    d, *_ = joined(app, make_box)
    data = os.urandom(ALIGN * 2)
    assert upload(d, data, "exact.bin").status_code == 200
    assert drive.named("exact.bin")[0]["content"] == data
    assert upload(d, b"", "empty.txt").status_code == 200
    assert drive.named("empty.txt")[0]["content"] == b""


def test_body_arriving_in_tiny_pieces(app, make_box, drive):
    d, *_ = joined(app, make_box)
    data = os.urandom(ALIGN + 5000)
    headers, body = multipart([({"name": "file", "filename": "p.bin"}, data)])
    pieces = (body[i:i + 1000] for i in range(0, len(body), 1000))
    assert d.post("/api/upload", content=pieces, headers=headers).status_code == 200
    assert drive.named("p.bin")[0]["content"] == data


def test_note_becomes_sidecar_and_follows_rename_and_delete(app, make_box, drive):
    d, _, folder = joined(app, make_box)
    r = upload(d, b"x", "doc.pdf", note="please review")
    fid = r.json()["id"]
    assert d.get("/api/my-files").json()[0] | {"id": 0, "created_at": 0} == {
        "id": 0, "filename": "doc.pdf", "size": 1, "created_at": 0, "has_note": True, "note": "please review"}
    (side,) = drive.named("doc.pdf.meta.txt")
    assert side["text"].strip() == "please review" and side["parents"] == [folder]

    assert d.patch(f"/api/files/{fid}", json={"filename": "final.pdf"}).status_code == 200
    assert drive.named("final.pdf") and drive.named("final.pdf.meta.txt") and not drive.named("doc.pdf")

    assert d.delete(f"/api/files/{fid}").status_code == 204
    assert drive.named("final.pdf")[0]["trashed"] and drive.named("final.pdf.meta.txt")[0]["trashed"]
    assert d.get("/api/my-files").json() == []


def test_note_without_file_or_blank_note(app, make_box, drive):
    d, *_ = joined(app, make_box)
    headers, body = multipart([({"name": "note"}, b"hi")])
    assert d.post("/api/upload", content=body, headers=headers).status_code == 400
    assert upload(d, b"x", "n.txt", note="   ").status_code == 200
    assert not drive.named("n.txt.meta.txt")


def test_filename_is_sanitized(app, make_box, drive):
    d, *_ = joined(app, make_box)
    assert upload(d, b"x", "../../etc/passwd").status_code == 200
    assert drive.named("passwd")
    assert upload(d, b"x", "..").status_code == 200
    assert drive.named("unnamed")


def test_files_are_private_to_the_uploading_device(app, make_box, drive):
    d1, box, _ = joined(app, make_box)
    fid = upload(d1, b"secret", "mine.txt").json()["id"]
    d2 = new_device(app)
    d2.get(f"/drop/{box}")
    assert d2.get("/api/my-files").json() == []
    assert d2.patch(f"/api/files/{fid}", json={"filename": "x"}).status_code == 404
    assert d2.delete(f"/api/files/{fid}").status_code == 404
    assert drive.named("mine.txt") and not drive.named("x")


def test_no_download_route_exists(app, make_box):
    d, *_ = joined(app, make_box)
    fid = upload(d, b"x", "a.txt").json()["id"]
    for path in (f"/api/files/{fid}", f"/api/files/{fid}/download", f"/api/download/{fid}"):
        assert d.get(path).status_code in (404, 405)


def test_upload_requires_a_session_and_multipart(app, make_box):
    assert new_device(app).post("/api/upload", content=b"x").status_code == 403
    d, *_ = joined(app, make_box)
    assert d.post("/api/upload", json={"a": 1}).status_code == 400


def test_transient_drive_errors_are_retried(app, make_box, drive):
    d, *_ = joined(app, make_box)
    data = os.urandom(ALIGN * 2 + 10)
    drive.fail_next_put = 2
    assert upload(d, data, "flaky.bin").status_code == 200
    assert drive.named("flaky.bin")[0]["content"] == data


def test_persistent_drive_failure_cleans_up(app, make_box, drive):
    d, *_ = joined(app, make_box)
    drive.fail_next_put = 1000
    assert upload(d, b"x" * 10, "bad.bin").status_code == 502
    assert d.get("/api/my-files").json() == []
    db = sqlite3.connect(config.DB_PATH)
    assert db.execute("SELECT COUNT(*) FROM uploaded_files").fetchone()[0] == 0


def test_sidecar_failure_rolls_back_the_upload(app, make_box, drive):
    d, *_ = joined(app, make_box)
    drive.fail_sidecar = True
    assert upload(d, b"x", "c.txt", note="note").status_code == 502
    assert drive.named("c.txt")[0]["trashed"]
    assert d.get("/api/my-files").json() == []


def test_resumable_upload_resumes_after_partial_persist():
    import asyncio
    import httpx

    stored = bytearray()
    first = {"done": False}

    def handler(req):
        rng = req.headers["content-range"]
        if req.content:
            start = int(rng.split()[1].split("-")[0])
            assert start == len(stored)
            total = rng.rsplit("/", 1)[1]
            if not first["done"] and total == "*":
                first["done"] = True
                stored.extend(req.content[:ALIGN])  # Drive keeps only part of the chunk
                return httpx.Response(308, headers={"Range": f"bytes=0-{ALIGN - 1}"})
            stored.extend(req.content)
            if total != "*":
                return httpx.Response(200, json={"id": "F"})
            return httpx.Response(308, headers={"Range": f"bytes=0-{len(stored) - 1}"})
        return httpx.Response(200, json={"id": "F"})

    async def run():
        gdrive.set_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        up = gdrive.ResumableUpload("https://upload.test/s")
        up.CHUNK = ALIGN * 2
        data = os.urandom(ALIGN * 2 + 50)
        await up.write(data)
        assert await up.finish() == "F"
        assert bytes(stored) == data

    asyncio.run(run())
    gdrive.set_client(None)
