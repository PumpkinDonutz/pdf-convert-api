"""Bounded-queue / busy-path tests. Deterministic: the gate is held directly."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.gate import ConversionGate
from app.main import create_app
from app.schemas import ApiError

from .conftest import TOKEN, leftovers, make_text_pdf, post_pdf


async def test_queue_fills_then_busy_then_recovers():
    gate = ConversionGate(queue_size=2, retry_after_seconds=7)
    release = asyncio.Event()
    active = 0
    peak = 0

    async def worker():
        nonlocal active, peak
        async with gate.slot():
            active += 1
            peak = max(peak, active)
            await release.wait()
            active -= 1

    tasks = [asyncio.create_task(worker()) for _ in range(3)]  # 1 running + 2 queued
    await asyncio.sleep(0.05)
    assert active == 1

    with pytest.raises(ApiError) as exc:
        async with gate.slot():
            pass
    assert exc.value.status == 503
    assert exc.value.code == "SERVER_BUSY"
    assert exc.value.headers["Retry-After"] == "7"

    release.set()
    await asyncio.gather(*tasks)
    assert peak == 1  # never more than one conversion at a time

    async with gate.slot():  # recovered
        pass


async def test_zero_length_queue_rejects_immediately_when_busy():
    gate = ConversionGate(queue_size=0, retry_after_seconds=1)
    async with gate.slot():
        with pytest.raises(ApiError) as exc:
            async with gate.slot():
                pass
        assert exc.value.code == "SERVER_BUSY"


async def test_cancelled_waiter_frees_its_queue_place():
    gate = ConversionGate(queue_size=1, retry_after_seconds=1)
    async with gate.slot():
        waiter = asyncio.create_task(_enter(gate))
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        replacement = asyncio.create_task(_enter(gate))  # would be BUSY if the place leaked
        await asyncio.sleep(0.05)
        assert not replacement.done()
        replacement.cancel()
        with pytest.raises(asyncio.CancelledError):
            await replacement


async def _enter(gate):
    async with gate.slot():
        pass


def test_http_returns_503_with_retry_after_when_queue_full(tmp_root):
    settings = Settings(api_tokens=(TOKEN,), queue_size=2, retry_after_seconds=11)
    app = create_app(settings)
    gate = app.state.gate
    asyncio.run(gate._sem.acquire())  # hold the single conversion slot directly
    gate._waiting = settings.queue_size  # and fill the waiting queue
    with TestClient(app, raise_server_exceptions=False) as c:
        r = post_pdf(c, make_text_pdf(), params="?ocr=never")
    assert r.status_code == 503
    assert r.json()["code"] == "SERVER_BUSY"
    assert r.headers["retry-after"] == "11"
    assert not leftovers(tmp_root)  # rejected request cleaned up its upload


def test_http_request_waits_in_queue_then_succeeds(tmp_root):
    """With the slot busy but the queue not full, a request waits rather than failing."""
    import threading
    import time

    settings = Settings(api_tokens=(TOKEN,), queue_size=2)
    app = create_app(settings)
    gate = app.state.gate
    asyncio.run(gate._sem.acquire())  # slot held
    result = {}

    with TestClient(app, raise_server_exceptions=False) as c:
        def call():
            result["r"] = post_pdf(c, make_text_pdf(), params="?ocr=never")

        t = threading.Thread(target=call)
        t.start()
        time.sleep(1.0)
        assert "r" not in result  # still waiting, not rejected

        async def release():
            gate._sem.release()

        c.portal.call(release)  # release from within the app's own event loop
        t.join(timeout=60)
    assert result["r"].status_code == 200
