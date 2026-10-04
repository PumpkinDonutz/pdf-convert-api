"""Abuse-resistance: admission, upload deadline, header cap, disconnects, page size, child limits."""
import asyncio
import os

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app import conversion
from app.config import ConfigError, Settings
from app.main import _unless_disconnected, create_app
from app.schemas import ApiError
from app.upload import receive_upload
from app.worker import page_has_invisible_text

from .conftest import TOKEN, URL, AUTH, leftovers, make_text_pdf, post_pdf


class FakeRequest:
    def __init__(self, chunks, boundary="b", delay=0.0):
        self.headers = {"content-type": f"multipart/form-data; boundary={boundary}"}
        self._chunks = chunks
        self._delay = delay

    async def stream(self):
        for chunk in self._chunks:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield chunk


def multipart(header_extra: str = "", payload: bytes = b"%PDF-1.4 x") -> bytes:
    return (
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.pdf"\r\n'
        + header_extra.encode()
        + b"\r\n"
        + payload
        + b"\r\n--b--\r\n"
    )


# --- multipart header cap -------------------------------------------------

async def test_oversized_part_header_is_rejected(tmp_path):
    body = multipart(header_extra="X-Junk: " + "a" * 20_000 + "\r\n")
    chunks = [body[i : i + 512] for i in range(0, len(body), 512)]
    with pytest.raises(ApiError) as exc:
        await receive_upload(FakeRequest(chunks), tmp_path / "in.pdf", 1_000_000)
    assert exc.value.status == 400


async def test_normal_part_headers_still_accepted(tmp_path):
    result = await receive_upload(FakeRequest([multipart()]), tmp_path / "in.pdf", 1_000_000)
    assert result.filename == "a.pdf" and result.size > 0


# --- upload deadline ------------------------------------------------------

def test_slow_upload_times_out_with_408(tmp_root):
    app = create_app(Settings(api_tokens=(TOKEN,), upload_timeout_seconds=1))

    async def slow(request, dest, max_bytes):
        await asyncio.sleep(30)

    import app.main as main_mod

    original = main_mod.receive_upload
    main_mod.receive_upload = slow
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            r = post_pdf(c, make_text_pdf())
    finally:
        main_mod.receive_upload = original
    assert r.status_code == 408 and r.json()["code"] == "UPLOAD_TIMEOUT"
    assert not leftovers(tmp_root)


def test_upload_timeout_must_be_positive():
    with pytest.raises(ConfigError):
        Settings(api_tokens=(TOKEN,), upload_timeout_seconds=0)


# --- admission before upload ---------------------------------------------

def test_admission_rejects_before_reading_upload(tmp_root):
    app = create_app(Settings(api_tokens=(TOKEN,), queue_size=1))
    gate = app.state.gate
    gate._inflight = gate._max_inflight  # everything already in flight
    with TestClient(app, raise_server_exceptions=False) as c:
        r = post_pdf(c, make_text_pdf(), params="?ocr=never")
    assert r.status_code == 503 and r.json()["code"] == "SERVER_BUSY"
    assert r.headers["retry-after"]
    assert not leftovers(tmp_root)


def test_admission_counter_is_released(tmp_root):
    app = create_app(Settings(api_tokens=(TOKEN,)))
    with TestClient(app, raise_server_exceptions=False) as c:
        assert post_pdf(c, make_text_pdf(), params="?ocr=never").status_code == 200
        assert post_pdf(c, b"not a pdf").status_code == 415
    assert app.state.gate._inflight == 0


# --- disconnect cancels work ---------------------------------------------

class DisconnectingRequest:
    def __init__(self, after: float):
        self._after = after

    async def receive(self):
        await asyncio.sleep(self._after)
        return {"type": "http.disconnect"}


async def test_disconnect_cancels_work():
    cancelled = asyncio.Event()

    async def work():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(ApiError) as exc:
        await _unless_disconnected(DisconnectingRequest(0.05), work())
    assert exc.value.code == "CLIENT_DISCONNECTED"
    assert cancelled.is_set()


async def test_finished_work_returns_result_and_stops_watcher():
    class NeverDisconnects:
        async def receive(self):
            await asyncio.sleep(60)

    async def work():
        return 42

    assert await _unless_disconnected(NeverDisconnects(), work()) == 42


# --- page size ------------------------------------------------------------

def test_huge_page_rejected_before_any_work(client, run_spy, tmp_root):
    doc = pymupdf.open()
    doc.new_page(width=8000, height=8000)  # tiny file, enormous page
    data = doc.tobytes()
    doc.close()
    r = post_pdf(client, data)
    assert r.status_code == 422 and r.json()["code"] == "PAGE_TOO_LARGE"
    assert run_spy == []
    assert not leftovers(tmp_root)


def test_a0_page_allowed(client, tmp_root):
    doc = pymupdf.open()
    doc.new_page(width=2384, height=3370)
    data = doc.tobytes()
    doc.close()
    assert post_pdf(client, data, params="?ocr=never").status_code == 200


# --- worker strictness ----------------------------------------------------

class BrokenPage:
    def get_texttrace(self):
        raise RuntimeError("boom")


def test_probe_failure_is_fatal_when_ocr_ran():
    with pytest.raises(RuntimeError):
        page_has_invisible_text(BrokenPage(), strict=True)


def test_probe_failure_tolerated_without_ocr():
    assert page_has_invisible_text(BrokenPage(), strict=False) is False


# --- child file-size limit -----------------------------------------------

@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX only")
async def test_child_file_size_is_limited(tmp_path, monkeypatch):
    monkeypatch.setattr(conversion, "MAX_CHILD_FILE_BYTES", 4096)
    code = await conversion._run(
        ["sh", "-c", "head -c 100000 /dev/zero > big.bin"], env=dict(os.environ), cwd=tmp_path, timeout=10
    )
    assert code not in (0, None)
    assert (tmp_path / "big.bin").stat().st_size <= 4096
