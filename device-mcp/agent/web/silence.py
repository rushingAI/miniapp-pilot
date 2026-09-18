"""Shared, observation-only 240-second activity monitor."""

from __future__ import annotations

import asyncio
import time


class SilenceObserver:
    def __init__(self, emit, *, warn_after=240.0, waiting=None):
        self.emit = emit
        self.warn_after = float(warn_after)
        self.waiting = waiting or (lambda: False)
        self.last_activity = time.monotonic()
        self._level = 0
        self._closed = False

    def touch(self):
        self.last_activity = time.monotonic()
        self._level = 0

    async def watch(self):
        interval = max(0.005, min(1.0, self.warn_after / 4))
        while not self._closed:
            await asyncio.sleep(interval)
            if self.waiting():
                self.touch()
                continue
            elapsed = time.monotonic() - self.last_activity
            target = 1 if elapsed >= self.warn_after else 0
            if target > self._level:
                self._level = 1
                await self.emit({
                    "type": "silence_warning", "level": 1,
                    "elapsed_s": round(elapsed),
                    "msg": "模型已持续 240 秒无可见进展；任务仍在等待，不会自动重试或重复设备操作",
                })

    def close(self):
        self._closed = True
