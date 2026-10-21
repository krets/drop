"""Incremental multipart/form-data parsing: yields events as the request body arrives.

Starlette's form parser spools file parts to temporary files on disk; this never does.

Events:  ("begin", name, filename | None, content_type | None)
         ("data", bytes)
         ("end",)
"""
from collections.abc import AsyncIterator

from python_multipart.multipart import MultipartParser, parse_options_header


def boundary_of(content_type: str) -> bytes | None:
    media, params = parse_options_header(content_type)
    return params.get(b"boundary") if media == b"multipart/form-data" else None


async def stream_multipart(body: AsyncIterator[bytes], boundary: bytes):
    events: list[tuple] = []
    headers: dict[bytes, bytes] = {}
    field, value = bytearray(), bytearray()

    def on_header_field(data, start, end):
        field.extend(data[start:end])

    def on_header_value(data, start, end):
        value.extend(data[start:end])

    def on_header_end():
        headers[bytes(field).lower()] = bytes(value)
        field.clear()
        value.clear()

    def on_headers_finished():
        _, opts = parse_options_header(headers.get(b"content-disposition", b""))
        filename = opts.get(b"filename")
        events.append((
            "begin",
            opts.get(b"name", b"").decode("utf-8", "replace"),
            None if filename is None else filename.decode("utf-8", "replace"),
            headers.get(b"content-type", b"").decode("latin-1") or None,
        ))
        headers.clear()

    parser = MultipartParser(boundary, {
        "on_header_field": on_header_field,
        "on_header_value": on_header_value,
        "on_header_end": on_header_end,
        "on_headers_finished": on_headers_finished,
        "on_part_data": lambda data, start, end: events.append(("data", bytes(data[start:end]))),
        "on_part_end": lambda: events.append(("end",)),
    })

    async for chunk in body:
        parser.write(chunk)
        batch, events[:] = events[:], []
        for event in batch:
            yield event
    parser.finalize()
    for event in events:
        yield event
