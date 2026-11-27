"""Regular US equity session hours per day, as published by IB.

IB's contract details carry ``liquidHours``: the regular session for each of the
next several days, with exchange holidays marked ``CLOSED`` and early closes
shown as shortened ranges. The scheduler and the P&L poller gate on these
sessions. Any day IB has not described falls back to weekday 09:30-16:00 ET.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ET = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
REFERENCE_SYMBOL = "SPY"
FETCH_TIMEOUT_SECONDS = 10.0

DayHours = tuple[time, time] | None

logger = logging.getLogger(__name__)
_published: Mapping[date, DayHours] = MappingProxyType({})


def parse_liquid_hours(text: str, tz_id: str) -> dict[date, DayHours]:
    """Parse IB ``liquidHours`` into ET (open, close) per day; None marks a closed day.

    Format: ``20261127:0930-20261127:1300;20261128:CLOSED``.
    """
    zone = _zone(tz_id)
    days: dict[date, DayHours] = {}
    for part in (p.strip() for p in (text or "").split(";")):
        if not part:
            continue
        try:
            if part.endswith(":CLOSED"):
                days.setdefault(date.fromisoformat(part[:8]), None)
                continue
            start_text, _, end_text = part.partition("-")
            start, end = _to_et(start_text, zone), _to_et(end_text, zone)
        except ValueError:
            logger.error(
                "Ignoring unparseable IB session %r; using the exchange calendar", part
            )
            continue
        known = days.get(start.date())
        opens = min(known[0], start.time()) if known else start.time()
        closes = max(known[1], end.time()) if known else end.time()
        days[start.date()] = (opens, closes)
    return days


def _zone(tz_id: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_id) if tz_id else ET
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown IB time zone %r; assuming New York time", tz_id)
        return ET


def _to_et(stamp: str, zone: ZoneInfo) -> datetime:
    return datetime.strptime(stamp, "%Y%m%d:%H%M").replace(tzinfo=zone).astimezone(ET)


def load(days: Mapping[date, DayHours]) -> None:
    """Publish session hours; newer data replaces older data for the same day."""
    global _published
    _published = MappingProxyType({**_published, **days})


def clear() -> None:
    """Forget all published sessions (every day falls back to regular hours)."""
    global _published
    _published = MappingProxyType({})


def knows(day: date) -> bool:
    return day in _published


def session_on(day: date) -> tuple[datetime, datetime] | None:
    """The regular session of an ET date as aware datetimes, or None when closed."""
    if day in _published:
        hours = _published[day]
    else:
        hours = nyse_hours(day)
    if hours is None:
        return None
    opens, closes = hours
    return (
        datetime.combine(day, opens, tzinfo=ET),
        datetime.combine(day, closes, tzinfo=ET),
    )


def nyse_hours(day: date) -> DayHours:
    """NYSE regular hours from the exchange's published holiday rules.

    Used for days IB has not described. Ad-hoc closures (e.g. national days of
    mourning) cannot be predicted and only come from IB.
    """
    if day.weekday() >= 5 or day in _nyse_holidays(day.year):
        return None
    if day in _nyse_early_closes(day.year):
        return (MARKET_OPEN, EARLY_CLOSE)
    return (MARKET_OPEN, MARKET_CLOSE)


@lru_cache(maxsize=16)
def _nyse_holidays(year: int) -> frozenset[date]:
    fixed = [date(year, 1, 1), date(year, 7, 4), date(year, 12, 25)]
    if year >= 2022:
        fixed.append(date(year, 6, 19))  # Juneteenth
    # New Year's Day on a Saturday is not observed on the prior Friday.
    observed = {_observed(d) for d in fixed if not (d.month == 1 and d.weekday() == 5)}
    floating = {
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
    }
    return frozenset(observed | floating)


@lru_cache(maxsize=16)
def _nyse_early_closes(year: int) -> frozenset[date]:
    """13:00 ET closes: the day after Thanksgiving, and July 3 / Dec 24 on Mon-Thu."""
    after_thanksgiving = _nth_weekday(year, 11, 3, 4) + timedelta(days=1)
    eves = (date(year, 7, 3), date(year, 12, 24))
    return frozenset({after_thanksgiving, *(eve for eve in eves if eve.weekday() < 4)})


def _observed(holiday: date) -> date:
    return holiday + timedelta(days={5: -1, 6: 1}.get(holiday.weekday(), 0))


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (Meeus/Jones/Butcher)."""
    a, (b, c) = year % 19, divmod(year, 100)
    d, e = divmod(b, 4)
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    weekday_shift = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * weekday_shift) // 451
    month, day = divmod(h + weekday_shift - 7 * m + 114, 31)
    return date(year, month, day + 1)


async def ensure_loaded(ib: Any, *, today: date | None = None) -> bool:
    """Fetch IB's published sessions unless today's are known. Never raises."""
    day = today or datetime.now(ET).date()
    if knows(day):
        return True
    try:
        from ib_async import Stock

        details = await asyncio.wait_for(
            ib.reqContractDetailsAsync(Stock(REFERENCE_SYMBOL, "SMART", "USD")),
            FETCH_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - keep trading on regular hours
        logger.warning(
            "Session hours unavailable from IB (%s); using regular hours", exc
        )
        return False
    days = (
        parse_liquid_hours(details[0].liquidHours, details[0].timeZoneId)
        if details
        else {}
    )
    load(days)
    if day not in days:
        logger.warning("IB published no session for %s; using regular hours", day)
        return False
    return True
