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


async def test_ensure_loaded_keeps_regular_hours_when_ib_fails():
    market_calendar.clear()
    ib = MagicMock()
    ib.reqContractDetailsAsync = AsyncMock(side_effect=ConnectionError("down"))

    loaded = await market_calendar.ensure_loaded(ib, today=date(2026, 11, 27))

    assert not loaded
    assert is_market_open(datetime(2026, 11, 27, 13, 1, tzinfo=ET))
