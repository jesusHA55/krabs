"""US equities calendar (NYSE/Nasdaq regular sessions).

Deliberately dependency-free: a small holiday table covers 2024-2026. Extend
``HOLIDAYS`` before running past 2026 or replace this module with
``exchange_calendars``; the rest of the platform only uses ``is_open``,
``session_bounds`` and ``next_open``.

All returned datetimes are timezone-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
UTC = timezone.utc

OPEN_TIME = time(9, 30)
CLOSE_TIME = time(16, 0)
EARLY_CLOSE_TIME = time(13, 0)

# Full-day market holidays.
HOLIDAYS: set[date] = {
    # 2024
    date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19), date(2024, 3, 29),
    date(2024, 5, 27), date(2024, 6, 19), date(2024, 7, 4), date(2024, 9, 2),
    date(2024, 11, 28), date(2024, 12, 25),
    # 2025
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25),
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
}

# Sessions that close at 13:00 ET.
EARLY_CLOSES: set[date] = {
    date(2024, 7, 3), date(2024, 11, 29), date(2024, 12, 24),
    date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24),
    date(2026, 11, 27), date(2026, 12, 24),
}

TIMEFRAME_MINUTES = {
    "1Min": 1,
    "5Min": 5,
    "15Min": 15,
    "30Min": 30,
    "1Hour": 60,
    "1Day": 390,
}


def timeframe_delta(timeframe: str) -> timedelta:
    try:
        return timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
    except KeyError as exc:
        raise ValueError(f"unsupported timeframe: {timeframe!r}") from exc


@dataclass(frozen=True, slots=True)
class Session:
    day: date
    opens: datetime
    closes: datetime

    @property
    def is_early_close(self) -> bool:
        return self.closes.astimezone(EASTERN).time() == EARLY_CLOSE_TIME

    def contains(self, ts: datetime) -> bool:
        moment = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
        return self.opens <= moment.astimezone(UTC) < self.closes


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in HOLIDAYS


def session_for(day: date) -> Session | None:
    if not is_trading_day(day):
        return None
    close_time = EARLY_CLOSE_TIME if day in EARLY_CLOSES else CLOSE_TIME
    opens = datetime.combine(day, OPEN_TIME, tzinfo=EASTERN).astimezone(UTC)
    closes = datetime.combine(day, close_time, tzinfo=EASTERN).astimezone(UTC)
    return Session(day=day, opens=opens, closes=closes)


def _to_eastern(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(EASTERN)


def is_open(ts: datetime) -> bool:
    """True if ``ts`` falls inside a regular trading session."""
    eastern = _to_eastern(ts)
    session = session_for(eastern.date())
    return bool(session and session.contains(ts))


def next_open(ts: datetime) -> datetime:
    """First session open strictly after ``ts`` (UTC)."""
    moment = ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)
    day = _to_eastern(moment).date()
    for _ in range(10):
        session = session_for(day)
        if session and session.opens > moment:
            return session.opens
        day += timedelta(days=1)
    raise RuntimeError("no market session found within 10 days")


def previous_close(ts: datetime) -> datetime:
    """Most recent session close at or before ``ts`` (UTC)."""
    eastern = _to_eastern(ts)
    day = eastern.date()
    for _ in range(10):
        session = session_for(day)
        if session and session.closes <= ts.astimezone(UTC):
            return session.closes
        day -= timedelta(days=1)
    raise RuntimeError("no market session found within 10 days")


def session_days(start: date, end: date) -> list[date]:
    """Trading days in ``[start, end]`` inclusive."""
    days: list[date] = []
    day = start
    while day <= end:
        if is_trading_day(day):
            days.append(day)
        day += timedelta(days=1)
    return days


def bar_is_closed(bar_ts: datetime, timeframe: str, now: datetime) -> bool:
    """A Krabs bar timestamp is its *close* time, so it is usable at or after it."""
    del timeframe
    return now >= bar_ts
