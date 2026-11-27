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
from datetime import date, datetime, time
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ET = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
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
            logger.warning("Ignoring unparseable IB session %r", part)
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
        hours = (MARKET_OPEN, MARKET_CLOSE) if day.weekday() < 5 else None
    if hours is None:
        return None
    opens, closes = hours
    return (
        datetime.combine(day, opens, tzinfo=ET),
        datetime.combine(day, closes, tzinfo=ET),
    )


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
