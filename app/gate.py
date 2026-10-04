"""One active conversion per replica, with a small bounded waiting queue."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager

from .schemas import server_busy

# Requests allowed to be uploading/inspecting beyond the running + queued conversions.
_UPLOAD_SLACK = 2


class ConversionGate:
    def __init__(self, queue_size: int, retry_after_seconds: int):
        self._sem = asyncio.Semaphore(1)
        self._waiting = 0
        self._queue_size = queue_size
        self._retry_after = retry_after_seconds
        self._inflight = 0
        self._max_inflight = 1 + queue_size + _UPLOAD_SLACK

    @contextmanager
    def admit(self):
        """Bound requests in flight (uploading, inspecting, queued or converting).

        Checked before any upload is read so excess clients cost one counter
        comparison, not a temp directory and a PDF parse. Never blocks.
        """
        if self._inflight >= self._max_inflight:
            raise server_busy(self._retry_after)
        self._inflight += 1
        try:
            yield
        finally:
            self._inflight -= 1

    @asynccontextmanager
    async def slot(self):
        """Wait for the single conversion slot, or raise SERVER_BUSY if the queue is full."""
        if self._sem.locked():
            if self._waiting >= self._queue_size:
                raise server_busy(self._retry_after)
            self._waiting += 1
            try:
                await self._sem.acquire()
            finally:
                self._waiting -= 1
        else:
            await self._sem.acquire()
        try:
            yield
        finally:
            self._sem.release()
