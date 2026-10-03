"""One active conversion per replica, with a small bounded waiting queue."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from .schemas import server_busy


class ConversionGate:
    def __init__(self, queue_size: int, retry_after_seconds: int):
        self._sem = asyncio.Semaphore(1)
        self._waiting = 0
        self._queue_size = queue_size
        self._retry_after = retry_after_seconds

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
