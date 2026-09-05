"""Small in-process scheduler for saved daily scans."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime
from typing import Any

from flightopt.jobs.daily import dispatch_due_profiles


class DailyScanScheduler:
    def __init__(self, runner, *, interval_seconds: int = 600,
                 now: Callable[[], datetime] | None = None) -> None:
        self.runner = runner
        self.interval_seconds = interval_seconds
        self.now = now or datetime.now
        self._task: asyncio.Task | None = None

    async def run_once(self) -> list[dict[str, Any]]:
        conn = self.runner._conn()
        try:
            return dispatch_due_profiles(conn, self.runner, now=self.now())
        finally:
            conn.close()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self.interval_seconds)
