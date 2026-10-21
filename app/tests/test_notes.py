import re

import config
from tests.conftest import new_device
from tests.test_upload import joined, upload


def comments(d):
    return d.get("/api/my-comments").json()


def test_comment_is_its_own_file_in_the_folder(app, make_box, drive):
    d, _, folder = joined(app, make_box)
    r = d.post("/api/comments", json={"text": "  Sent the signed copies  "})
    assert r.status_code == 201, r.text
    (f,) = [f for f in drive.files.values() if f["name"].startswith("Comment ")]
    assert re.fullmatch(r"Comment \d{4}-\d\d-\d\d \d\d\.\d\d\.\d\d\.txt", f["name"])
    assert f["parents"] == [folder] and f["text"] == "Sent the signed copies\n"
    assert comments(d) == [{"id": r.json()["id"], "text": "Sent the signed copies", "created_at": r.json()["created_at"]}]
    assert d.get("/api/my-files").json() == []  # not mixed in with uploads


def test_blank_or_huge_comment_is_rejected(app, make_box, drive):
    d, *_ = joined(app, make_box)
    assert d.post("/api/comments", json={"text": "   "}).status_code == 422
    assert d.post("/api/comments", json={"text": "x" * 5001}).status_code == 422
    assert d.post("/api/comments", json={"text": "x" * 5000}).status_code == 201


def test_edit_and_delete_comment(app, make_box, drive):
    d, *_ = joined(app, make_box)
    cid = d.post("/api/comments", json={"text": "first"}).json()["id"]
    (f,) = [f for f in drive.files.values() if f["name"].startswith("Comment ")]

    assert d.patch(f"/api/comments/{cid}", json={"text": "second"}).status_code == 200
    assert f["text"] == "second\n" and comments(d)[0]["text"] == "second"
    assert d.patch(f"/api/comments/{cid}", json={"text": " "}).status_code == 422

    assert d.delete(f"/api/comments/{cid}").status_code == 204
    assert f["trashed"] and comments(d) == []


def test_comments_are_private_to_the_device(app, make_box, drive):
    d1, box, _ = joined(app, make_box)
    cid = d1.post("/api/comments", json={"text": "mine"}).json()["id"]
    d2 = new_device(app)
    d2.get(f"/drop/{box}")
    assert comments(d2) == []
    assert d2.patch(f"/api/comments/{cid}", json={"text": "x"}).status_code == 404
    assert d2.delete(f"/api/comments/{cid}").status_code == 404
    assert comments(d1)[0]["text"] == "mine"


def test_comments_need_a_session(app, make_box):
    d = new_device(app)
    assert d.post("/api/comments", json={"text": "hi"}).status_code == 403
    assert d.get("/api/my-comments").status_code == 403


def test_comment_drive_failure_leaves_nothing_behind(app, make_box, drive):
    d, *_ = joined(app, make_box)
    drive.fail_sidecar = True  # fails every small text upload
    assert d.post("/api/comments", json={"text": "hi"}).status_code == 502
    assert comments(d) == []


def test_add_edit_and_remove_a_files_note(app, make_box, drive):
    d, _, folder = joined(app, make_box)
    fid = upload(d, b"x", "scan.pdf").json()["id"]
    assert d.get("/api/my-files").json()[0]["has_note"] is False

    r = d.put(f"/api/files/{fid}/note", json={"text": "page 3 is upside down"})
    assert r.status_code == 200
    (side,) = drive.named("scan.pdf.meta.txt")
    assert side["text"] == "page 3 is upside down\n" and side["parents"] == [folder]
    listed = d.get("/api/my-files").json()[0]
    assert listed["has_note"] is True and listed["note"] == "page 3 is upside down"

    assert d.put(f"/api/files/{fid}/note", json={"text": "fixed"}).status_code == 200
    assert drive.named("scan.pdf.meta.txt") == [side] and side["text"] == "fixed\n"  # edited in place

    assert d.patch(f"/api/files/{fid}", json={"filename": "scan2.pdf"}).status_code == 200
    assert side["name"] == "scan2.pdf.meta.txt"

    assert d.delete(f"/api/files/{fid}/note").status_code == 204
    assert side["trashed"]
    listed = d.get("/api/my-files").json()[0]
    assert listed["has_note"] is False and listed["note"] == ""
    assert d.delete(f"/api/files/{fid}/note").status_code == 204  # already gone is fine
    assert d.put(f"/api/files/{fid}/note", json={"text": "again"}).status_code == 200
    assert len(drive.named("scan2.pdf.meta.txt")) == 2  # a fresh sidecar, the old one stays trashed


def test_notes_are_private_and_validated(app, make_box, drive):
    d1, box, _ = joined(app, make_box)
    fid = upload(d1, b"x", "a.txt").json()["id"]
    assert d1.put(f"/api/files/{fid}/note", json={"text": ""}).status_code == 422
    d2 = new_device(app)
    d2.get(f"/drop/{box}")
    assert d2.put(f"/api/files/{fid}/note", json={"text": "hi"}).status_code == 404
    assert d2.delete(f"/api/files/{fid}/note").status_code == 404
    assert not drive.named("a.txt.meta.txt")


def test_upload_makes_no_comment_files(app, make_box, drive):
    d, *_ = joined(app, make_box)
    assert upload(d, b"x", "a.txt", note="n").status_code == 200
    assert not [f for f in drive.files.values() if f["name"].startswith("Comment ")]
    assert comments(d) == []


def test_oversized_note_on_upload_is_rejected(app, make_box, drive):
    d, *_ = joined(app, make_box)
    assert upload(d, b"x", "a.txt", note="n" * 5001).status_code == 413
    assert d.get("/api/my-files").json() == [] and not drive.named("a.txt")
