"""Clock abstraction.

The same strategy and broker code runs in backtests and in production. The only
difference is where ``now`` comes from. Nothing in the engine may call
``datetime.now`` directly; tests inject a SimulatedClock and live code uses
RealClock.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...

    async def sleep_until(self, when: datetime) -> None: ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def sleep_until(self, when: datetime) -> None:
        delay = (when - self.now()).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)


class SimulatedClock:
    """Mutable clock advanced by the backtest engine, one timestamp at a time."""

    def __init__(self, start: datetime) -> None:
        self._now = start
        if self._now.tzinfo is None:
            self._now = self._now.replace(tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when < self._now:
            raise ValueError(f"clock cannot move backwards: {when} < {self._now}")
        self._now = when

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - never awaited
        self.advance(seconds)

    async def sleep_until(self, when: datetime) -> None:  # pragma: no cover
        self.set(when)
