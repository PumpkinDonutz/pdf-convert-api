"""Conversion paths, OCR mode selection, failure mapping, cleanup and logging."""
import asyncio
import io
import logging
import os
import time
import zipfile

import pytest
from docx import Document

from app import conversion
from app.config import Settings

from .conftest import (
    TOKEN,
    leftovers,
    make_mixed_pdf,
    make_scanned_pdf,
    make_text_pdf,
    post_pdf,
)

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def docx_text(content: bytes) -> str:
    doc = Document(io.BytesIO(content))
    return "\n".join(p.text for p in doc.paragraphs)


def assert_valid_docx(r):
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == DOCX_TYPE
    assert len(r.content) > 0
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert "word/document.xml" in zf.namelist()


def ocr_calls(calls):
    return [c for c in calls if c[0] == "ocrmypdf"]


def test_text_pdf_auto_converts_with_skip_text(client, run_spy, tmp_root):
    r = post_pdf(client, make_text_pdf(), filename="My Invoice (final).pdf")
    assert_valid_docx(r)
    assert "Quarterly invoice summary" in docx_text(r.content)
    assert r.headers["content-disposition"] == 'attachment; filename="My_Invoice_final.docx"'
    (call,) = ocr_calls(run_spy)
    assert "--skip-text" in call and "--force-ocr" not in call
    assert not leftovers(tmp_root)  # cleanup ran after the response streamed


def test_scanned_pdf_auto_runs_ocr_and_converts(client, run_spy, tmp_root):
    r = post_pdf(client, make_scanned_pdf())
    assert_valid_docx(r)
    assert len(ocr_calls(run_spy)) == 1
    assert not leftovers(tmp_root)


def test_mixed_pdf_auto_uses_skip_text(client, run_spy, tmp_root):
    r = post_pdf(client, make_mixed_pdf())
    assert_valid_docx(r)
    (call,) = ocr_calls(run_spy)
    assert "--skip-text" in call  # OCRmyPDF itself decides per page
    assert not leftovers(tmp_root)


def test_ocr_never_skips_ocrmypdf(client, run_spy, tmp_root):
    r = post_pdf(client, make_scanned_pdf(), params="?ocr=never")
    assert_valid_docx(r)
    assert ocr_calls(run_spy) == []
    assert not leftovers(tmp_root)


def test_ocr_always_forces_ocr(client, run_spy, tmp_root):
    r = post_pdf(client, make_text_pdf(), params="?ocr=always")
    assert_valid_docx(r)
    (call,) = ocr_calls(run_spy)
    assert "--force-ocr" in call and "--skip-text" not in call
    assert not leftovers(tmp_root)


def test_ocr_command_is_an_argument_list_with_configured_language(client, run_spy):
    post_pdf(client, make_text_pdf())
    (call,) = ocr_calls(run_spy)
    assert call[call.index("--language") + 1] == "eng"
    assert all(isinstance(a, str) for a in call)


@pytest.fixture
def fake_run(monkeypatch):
    """Replace subprocess execution; behaviour is chosen per tool name."""
    behaviour = {"ocrmypdf": 0, "worker": 0}

    async def fake(argv, **kwargs):
        name = "ocrmypdf" if argv[0] == "ocrmypdf" else "worker"
        result = behaviour[name]
        if result == 0:
            out = argv[-1]
            with open(out, "wb") as fh:
                fh.write(b"%PDF-fake" if name == "ocrmypdf" else b"")
        return result

    monkeypatch.setattr(conversion, "_run", fake)
    return behaviour


def test_ocr_failure_maps_to_ocr_failed(client, fake_run, tmp_root):
    fake_run["ocrmypdf"] = 1
    r = post_pdf(client, make_scanned_pdf())
    assert r.status_code == 422 and r.json()["code"] == "OCR_FAILED"
    assert not leftovers(tmp_root)


def test_ocr_timeout_maps_to_ocr_timeout(client, fake_run, tmp_root):
    fake_run["ocrmypdf"] = None
    r = post_pdf(client, make_scanned_pdf())
    assert r.status_code == 422 and r.json()["code"] == "OCR_TIMEOUT"
    assert not leftovers(tmp_root)


def test_converter_failure_maps_to_conversion_failed(client, fake_run, tmp_root):
    fake_run["worker"] = 1
    r = post_pdf(client, make_text_pdf())
    assert r.status_code == 422 and r.json()["code"] == "CONVERSION_FAILED"
    assert not leftovers(tmp_root)


def test_converter_timeout_maps_to_conversion_failed(client, fake_run, tmp_root):
    fake_run["worker"] = None
    r = post_pdf(client, make_text_pdf())
    assert r.status_code == 422 and r.json()["code"] == "CONVERSION_FAILED"
    assert not leftovers(tmp_root)


def test_zero_byte_output_maps_to_empty_output(client, fake_run, tmp_root):
    r = post_pdf(client, make_text_pdf(), params="?ocr=never")  # fake worker writes 0 bytes
    assert r.status_code == 422 and r.json()["code"] == "EMPTY_OUTPUT"
    assert not leftovers(tmp_root)


def test_invalid_docx_output_maps_to_empty_output(tmp_path):
    bad = tmp_path / "output.docx"
    bad.write_bytes(b"this is not a zip")
    with pytest.raises(Exception) as e:
        conversion.verify_docx(bad)
    assert e.value.code == "EMPTY_OUTPUT"
    with pytest.raises(Exception) as e:
        conversion.verify_docx(tmp_path / "missing.docx")
    assert e.value.code == "EMPTY_OUTPUT"


def pid_alive(pid: int) -> bool:
    """True if the process exists and is not a zombie (a killed orphan may linger as one)."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


async def test_run_kills_process_group_on_timeout(tmp_path):
    marker = tmp_path / "child.pid"
    script = f"sleep 60 & echo $! > {marker}; wait"
    started = time.monotonic()
    code = await conversion._run(["sh", "-c", script], env=dict(os.environ), cwd=tmp_path, timeout=1)
    assert code is None
    assert time.monotonic() - started < 10
    child = int(marker.read_text())
    await asyncio.sleep(0.3)
    assert not pid_alive(child)  # grandchild was killed with the group


async def test_run_kills_process_group_on_cancellation(tmp_path):
    marker = tmp_path / "child.pid"
    script = f"sleep 60 & echo $! > {marker}; wait"
    task = asyncio.create_task(conversion._run(["sh", "-c", script], env=dict(os.environ), cwd=tmp_path, timeout=60))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    child = int(marker.read_text())
    await asyncio.sleep(0.3)
    assert not pid_alive(child)


def test_request_log_line_is_structured_and_safe(client, caplog, tmp_root):
    with caplog.at_level(logging.INFO, logger="pdf_convert"):
        r = post_pdf(client, make_text_pdf(), params="?ocr=never", filename="secret-client-name.pdf")
        bad = post_pdf(client, b"nope", filename="other-secret.pdf")
    assert r.status_code == 200 and bad.status_code == 415
    lines = [__import__("json").loads(rec.getMessage()) for rec in caplog.records if rec.name == "pdf_convert"]
    assert len(lines) == 2
    ok, err = lines
    assert ok["status"] == 200 and ok["code"] == "OK" and ok["ocr_mode"] == "never" and ok["pages"] == 1
    assert ok["bytes"] > 0 and ok["duration_ms"] >= 0 and ok["request_id"]
    assert err["status"] == 415 and err["code"] == "NOT_A_PDF"
    assert r.headers["x-request-id"] == ok["request_id"]
    text = caplog.text
    assert TOKEN not in text and "secret-client-name" not in text and "other-secret" not in text
    assert "/tmp" not in text and "pdfconv-" not in text


def test_internal_error_logs_class_only(client, monkeypatch, caplog):
    monkeypatch.setattr("app.main.inspect_pdf", lambda *a, **k: (_ for _ in ()).throw(ValueError("private detail")))
    with caplog.at_level(logging.INFO, logger="pdf_convert"):
        r = post_pdf(client, make_text_pdf())
    assert r.status_code == 500
    assert "ValueError" in caplog.text
    assert "private detail" not in caplog.text


def test_failure_after_upload_leaves_no_files(client, monkeypatch, tmp_root):
    async def boom(*a, **k):
        raise RuntimeError("explode")

    monkeypatch.setattr("app.main.convert", boom)
    r = post_pdf(client, make_text_pdf())
    assert r.status_code == 500 and r.json()["code"] == "INTERNAL_ERROR"
    assert not leftovers(tmp_root)
