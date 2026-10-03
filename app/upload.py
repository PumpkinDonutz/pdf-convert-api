"""Streaming multipart upload to disk with a hard byte cap.

FastAPI's UploadFile parses the whole body before auth or size checks can run,
so the multipart body is parsed incrementally here: bytes are counted as they
are written and the request is aborted the moment the limit is exceeded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from fastapi import Request
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from .schemas import empty_file, file_too_large, missing_file_field

# Allowance for multipart framing and small non-file fields on top of the file cap.
_OVERHEAD_ALLOWANCE = 64 * 1024


@dataclass
class UploadResult:
    size: int
    filename: str | None


def safe_stem(filename: str | None) -> str:
    """Reduce a caller-supplied filename to a safe ASCII stem (never used as a path)."""
    if not filename:
        return "document"
    name = PurePosixPath(filename.replace("\\", "/")).name
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")[:100]
    return name or "document"


async def receive_upload(request: Request, dest: Path, max_bytes: int) -> UploadResult:
    content_type = request.headers.get("content-type", "")
    ctype, params = parse_options_header(content_type)
    boundary = params.get(b"boundary")
    if ctype != b"multipart/form-data" or not boundary:
        raise missing_file_field()

    state = {"header_field": b"", "header_value": b"", "headers": {}, "out": None, "done": False}
    result = UploadResult(size=0, filename=None)

    def on_part_begin() -> None:
        state["headers"] = {}
        state["header_field"] = b""
        state["header_value"] = b""
        state["out"] = None

    def on_header_field(data: bytes, start: int, end: int) -> None:
        state["header_field"] += data[start:end]

    def on_header_value(data: bytes, start: int, end: int) -> None:
        state["header_value"] += data[start:end]

    def on_header_end() -> None:
        state["headers"][state["header_field"].lower()] = state["header_value"]
        state["header_field"] = b""
        state["header_value"] = b""

    def on_headers_finished() -> None:
        disposition = state["headers"].get(b"content-disposition")
        if disposition is None or state["done"]:
            return
        _, disp_params = parse_options_header(disposition)
        if disp_params.get(b"name") == b"file":
            state["out"] = dest.open("wb")
            raw_name = disp_params.get(b"filename")
            result.filename = raw_name.decode("utf-8", errors="replace") if raw_name else None

    def on_part_data(data: bytes, start: int, end: int) -> None:
        out = state["out"]
        if out is None:
            return
        result.size += end - start
        if result.size > max_bytes:
            raise file_too_large(max_bytes)
        out.write(data[start:end])

    def on_part_end() -> None:
        out = state["out"]
        if out is not None:
            out.close()
            state["out"] = None
            state["done"] = True

    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_headers_finished": on_headers_finished,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
        },
    )

    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_bytes + _OVERHEAD_ALLOWANCE:
                raise file_too_large(max_bytes)
            parser.write(chunk)
        parser.finalize()
    except MultipartParseError as exc:
        raise missing_file_field() from exc
    finally:
        if state["out"] is not None:
            state["out"].close()

    if not state["done"]:
        raise missing_file_field()
    if result.size == 0:
        raise empty_file()
    return result
