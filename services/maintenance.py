"""Time-based housekeeping, independent of message traffic."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

log = logging.getLogger("astrbot_plugin_regular")


class Janitor:
    """Runs `job` now and then every `interval_s`, until stopped."""

    def __init__(self, job: Callable[[], None], interval_s: float = 3600):
        self.job = job
        self.interval_s = interval_s
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        """Start on the running loop. Returns False if there is no running loop yet."""
        if self.running:
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._task = loop.create_task(self._run())
        return True

    async def _run(self) -> None:
        while True:
            try:
                self.job()
            except Exception:  # keep going; a failed cleanup must not stop future ones
                log.exception("[regular] cleanup failed")
            await asyncio.sleep(self.interval_s)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
