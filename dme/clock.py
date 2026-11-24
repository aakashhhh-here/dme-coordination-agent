"""Simulated clock, timers, and business-hours arithmetic."""

from __future__ import annotations

import heapq
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from pydantic import BaseModel

from dme import config


class Task(BaseModel):
    kind: str
    supplier_id: str | None = None
    callback: bool = False
    tries: int = 0  # re-runs after an LLM/API failure


class Timer(BaseModel):
    timer_id: int
    at: datetime
    task: Task


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in config.HOLIDAYS


def business_time(t: datetime) -> datetime:
    """t itself if inside calling hours, otherwise the next opening time."""
    if is_business_day(t.date()):
        if t.hour < config.OPEN_HOUR:
            return datetime.combine(t.date(), time(config.OPEN_HOUR))
        if t.hour < config.CLOSE_HOUR:
            return t
    d = t.date() + timedelta(days=1)
    while not is_business_day(d):
        d += timedelta(days=1)
    return datetime.combine(d, time(config.OPEN_HOUR))


def add_business_days(d: date, n: int) -> date:
    step = timedelta(days=1 if n >= 0 else -1)
    for _ in range(abs(n)):
        d += step
        while not is_business_day(d):
            d += step
    return d


class SimClock:
    def __init__(self, now: datetime = config.SIM_START):
        self.now = now
        self._heap: list[tuple[datetime, int, Timer]] = []

    def schedule(self, timer: Timer) -> None:
        heapq.heappush(self._heap, (timer.at, timer.timer_id, timer))

    def cancel(self, timer_id: int) -> None:
        self._heap = [h for h in self._heap if h[1] != timer_id]
        heapq.heapify(self._heap)

    def advance_to(self, t: datetime, handler: Callable[[Timer], None]) -> None:
        """Fire everything due up to and including t, then set now = t."""
        while self._heap and self._heap[0][0] <= t:
            self._fire(handler)
        self.now = max(self.now, t)

    def advance_until_idle(self, handler: Callable[[Timer], None], max_events: int = 1000) -> None:
        for _ in range(max_events):
            if not self._heap:
                return
            self._fire(handler)
        raise RuntimeError(f"Still not idle after {max_events} timers: runaway loop?")

    def _fire(self, handler: Callable[[Timer], None]) -> None:
        at, _, timer = heapq.heappop(self._heap)
        self.now = max(self.now, at)  # an overdue timer fires now, time never goes back
        handler(timer)
