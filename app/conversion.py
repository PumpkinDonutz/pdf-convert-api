"""PDF validation, optional OCR, and PDF -> DOCX conversion.

Both OCRmyPDF and the pdf2docx worker run as subprocesses (argument lists, no
shell) in their own process group so a timeout or client cancellation kills
the whole tree. All intermediate files live in the per-request directory.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
import zipfile
from pathlib import Path

import pymupdf

from .config import Settings
from .schemas import (
    ApiError,
    conversion_failed,
    empty_output,
    encrypted_pdf,
    malformed_pdf,
    not_a_pdf,
    ocr_failed,
    ocr_timeout,
    page_limit_exceeded,
)

APP_ROOT = Path(__file__).resolve().parent.parent

_OCR_FLAGS = {"auto": "--skip-text", "always": "--force-ocr"}


def inspect_pdf(path: Path, max_pages: int) -> int:
    """Validate signature, readability, encryption and page count. Returns the page count."""
    with path.open("rb") as fh:
        if fh.read(5) != b"%PDF-":
            raise not_a_pdf()
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        raise malformed_pdf() from exc
    try:
        if doc.needs_pass:
            raise encrypted_pdf()
        try:
            pages = doc.page_count
        except Exception as exc:
            raise malformed_pdf() from exc
        if pages < 1:
            raise malformed_pdf()
        if pages > max_pages:
            raise page_limit_exceeded(max_pages)
        return pages
    finally:
        doc.close()


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def _run(argv: list[str], *, env: dict[str, str], cwd: Path, timeout: float) -> int | None:
    """Run a subprocess. Returns its exit code, or None if it timed out and was killed."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=max(timeout, 0.001))
    except asyncio.TimeoutError:
        _kill_group(proc)
        await proc.wait()
        return None
    except BaseException:
        # Cancellation (client disconnect) or anything unexpected: never leave children running.
        _kill_group(proc)
        raise
    return proc.returncode


def verify_docx(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise empty_output()
    try:
        with zipfile.ZipFile(path) as zf:
            if "word/document.xml" not in zf.namelist():
                raise empty_output()
    except zipfile.BadZipFile as exc:
        raise empty_output() from exc


async def convert(workdir: Path, settings: Settings, ocr_mode: str) -> Path:
    """Run OCR (per mode) then pdf2docx on workdir/input.pdf. Returns workdir/output.docx."""
    deadline = time.monotonic() + settings.conversion_timeout_seconds
    env = {**os.environ, "TMPDIR": str(workdir)}  # OCR intermediates stay in the request dir
    source = workdir / "input.pdf"

    if ocr_mode != "never":
        ocr_out = workdir / "ocr.pdf"
        argv = [
            "ocrmypdf",
            _OCR_FLAGS[ocr_mode],
            "--output-type", "pdf",
            "--optimize", "0",
            "--jobs", "1",
            "--language", settings.ocr_language,
            "--quiet",
            str(source),
            str(ocr_out),
        ]
        code = await _run(argv, env=env, cwd=workdir, timeout=deadline - time.monotonic())
        if code is None:
            raise ocr_timeout()
        if code != 0 or not ocr_out.is_file() or ocr_out.stat().st_size == 0:
            raise ocr_failed()
        source = ocr_out

    output = workdir / "output.docx"
    argv = [sys.executable, "-m", "app.worker", str(source), str(output)]
    code = await _run(argv, env=env, cwd=APP_ROOT, timeout=deadline - time.monotonic())
    if code is None:
        raise conversion_failed("The conversion timed out.")
    if code != 0:
        raise conversion_failed()
    verify_docx(output)
    return output
