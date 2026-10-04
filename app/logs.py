"""Structured, safe, one-line-per-request logging."""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("pdf_convert")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def log_request(*, request_id: str, status: int, code: str, duration_ms: int, info: dict) -> None:
    """Emit one JSON line. Only safe fields: never filenames, tokens, paths, or content."""
    record = {
        "request_id": request_id,
        "status": status,
        "code": code,
        "bytes": info.get("bytes"),
        "ocr_mode": info.get("ocr_mode"),
        "pages": info.get("pages"),
        "duration_ms": duration_ms,
        "error_class": info.get("error_class"),
    }
    logger.info(json.dumps(record))
