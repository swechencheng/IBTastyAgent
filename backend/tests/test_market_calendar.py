"""Real session hours from IB gate the scheduler: holidays and early closes."""

from datetime import date, datetime, time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

from tastyagent import market_calendar
from tastyagent.scheduler import (
    ET,
    is_decision_window_open,
    is_market_open,
    seconds_until_decision_window,
)

# SPY liquidHours as IB published them on 2026-11-27, plus Thanksgiving.
THANKSGIVING_WEEK = (
    "20261126:CLOSED;20261127:0930-20261127:1300;20261128:CLOSED;"
    "20261129:CLOSED;20261130:0930-20261130:1600"
)


@pytest.fixture(autouse=True)
def loaded_calendar():
    market_calendar.clear()
    market_calendar.load(
        market_calendar.parse_liquid_hours(THANKSGIVING_WEEK, "US/Eastern")
    )
    yield
    market_calendar.clear()


def test_parses_sessions_holidays_and_early_closes():
    sessions = market_calendar.parse_liquid_hours(THANKSGIVING_WEEK, "US/Eastern")

    assert sessions[date(2026, 11, 26)] is None
    assert sessions[date(2026, 11, 27)] == (time(9, 30), time(13, 0))
    assert sessions[date(2026, 11, 30)] == (time(9, 30), time(16, 0))


def test_market_closes_at_the_early_close():
    assert is_market_open(datetime(2026, 11, 27, 12, 59, tzinfo=ET))
    assert not is_market_open(datetime(2026, 11, 27, 13, 1, tzinfo=ET))
    assert not is_decision_window_open(datetime(2026, 11, 27, 13, 30, tzinfo=ET))


def test_exchange_holiday_is_closed_all_day():
    assert not is_market_open(datetime(2026, 11, 26, 12, 0, tzinfo=ET))
    assert not is_decision_window_open(datetime(2026, 11, 26, 12, 0, tzinfo=ET))


def test_next_decision_window_skips_closed_days():
    now = datetime(2026, 11, 27, 14, 0, tzinfo=ET)

    wait = seconds_until_decision_window(now, open_delay_minutes=15)

    assert wait == (datetime(2026, 11, 30, 9, 45, tzinfo=ET) - now).total_seconds()


def test_days_the_calendar_does_not_cover_use_regular_hours():
    assert is_market_open(datetime(2026, 6, 1, 12, 0, tzinfo=ET))
    assert not is_market_open(datetime(2026, 6, 1, 16, 30, tzinfo=ET))


REGULAR = (time(9, 30), time(16, 0))
EARLY = (time(9, 30), time(13, 0))


@pytest.mark.parametrize(
    ("day", "hours"),
    [
        (date(2026, 11, 26), None),  # Thanksgiving
        (date(2026, 11, 27), EARLY),  # day after Thanksgiving
        (date(2026, 12, 24), EARLY),  # Christmas Eve on a Thursday
        (date(2026, 12, 25), None),  # Christmas
        (date(2026, 4, 3), None),  # Good Friday
        (date(2026, 1, 19), None),  # Martin Luther King Jr. Day
        (date(2026, 7, 3), None),  # Independence Day observed on Friday
        (date(2026, 7, 2), REGULAR),  # no early close before an observed Friday
        (date(2025, 7, 3), EARLY),  # early close before a Friday July 4
        (date(2027, 7, 5), None),  # July 4 on a Sunday, observed Monday
        (date(2027, 6, 18), None),  # Juneteenth on a Saturday, observed Friday
        (date(2021, 12, 31), REGULAR),  # New Year's Day on a Saturday is not observed
        (date(2026, 6, 1), REGULAR),  # an ordinary Monday
        (date(2026, 11, 28), None),  # Saturday
    ],
)
def test_exchange_calendar_fallback(day, hours):
    assert market_calendar.nyse_hours(day) == hours


async def test_ensure_loaded_reads_spy_hours_once_per_day():
    market_calendar.clear()
    ib = MagicMock()
    ib.reqContractDetailsAsync = AsyncMock(
        return_value=[NS(liquidHours=THANKSGIVING_WEEK, timeZoneId="US/Eastern")]
    )

    first = await market_calendar.ensure_loaded(ib, today=date(2026, 11, 27))
    second = await market_calendar.ensure_loaded(ib, today=date(2026, 11, 27))

    assert first and second
    ib.reqContractDetailsAsync.assert_awaited_once()
    assert not is_market_open(datetime(2026, 11, 27, 13, 1, tzinfo=ET))


async def test_falls_back_to_the_exchange_calendar_when_ib_fails():
    market_calendar.clear()
    ib = MagicMock()
    ib.reqContractDetailsAsync = AsyncMock(side_effect=ConnectionError("down"))

    loaded = await market_calendar.ensure_loaded(ib, today=date(2026, 11, 27))

    assert not loaded
    assert is_market_open(datetime(2026, 11, 27, 12, 59, tzinfo=ET))
    assert not is_market_open(datetime(2026, 11, 27, 13, 1, tzinfo=ET))
