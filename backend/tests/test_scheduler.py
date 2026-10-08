import asyncio
from datetime import datetime, time, timedelta

from tastyagent.scheduler import (
    ET,
    decision_window_start,
    is_decision_window_open,
    is_market_open,
    run_loop,
    seconds_until_decision_window,
)


def test_midday_weekday_open_matches_rule():
    dt = datetime(2026, 6, 1, 12, 0, tzinfo=ET)
    # At noon the time window is satisfied, so openness == is-a-weekday.
    assert is_market_open(dt) == (dt.weekday() < 5)


def test_after_hours_closed():
    assert not is_market_open(datetime(2026, 6, 1, 17, 30, tzinfo=ET))


def test_weekend_closed():
    d = datetime(2026, 6, 1, 12, 0, tzinfo=ET)
    while d.weekday() != 5:  # advance to a Saturday
        d += timedelta(days=1)
    assert not is_market_open(d)


async def test_run_loop_ticks_then_stops():
    stop = asyncio.Event()
    calls = []

    async def tick():
        calls.append(1)
        if len(calls) >= 2:
            stop.set()

    await run_loop(tick, interval_seconds=0.01, market_hours_only=False, stop=stop)
    assert len(calls) >= 2


async def test_run_loop_survives_tick_errors():
    stop = asyncio.Event()
    calls = []

    async def tick():
        calls.append(1)
        if len(calls) >= 2:
            stop.set()
        raise RuntimeError("boom")

    await run_loop(tick, interval_seconds=0.01, market_hours_only=False, stop=stop)
    assert len(calls) >= 2  # a failing tick didn't kill the loop


async def test_run_loop_throttles_if_recent_tick():
    import time

    stop = asyncio.Event()
    wake = asyncio.Event()
    calls = []
    # Simulate a tick that completed just now
    last_tick_t = time.time()

    async def tick():
        calls.append(time.time())
        stop.set()

    # Interval is 0.08s, so it should wait ~0.08s before running tick
    task = asyncio.create_task(
        run_loop(
            tick,
            interval_seconds=0.08,
            market_hours_only=False,
            stop=stop,
            wake_event=wake,
            get_last_tick_time=lambda: last_tick_t,
        )
    )

    await asyncio.sleep(0.03)
    # Should not have run yet at 0.03s
    assert len(calls) == 0

    await asyncio.wait_for(task, timeout=0.2)
    assert len(calls) == 1
    # Check that it waited at least ~0.06s from last_tick_t
    assert calls[0] - last_tick_t >= 0.06


async def test_run_loop_dynamic_interval_wake():
    import time

    stop = asyncio.Event()
    wake = asyncio.Event()
    calls = []
    interval = 100.0  # start very long

    async def tick():
        calls.append(time.time())
        if len(calls) >= 2:
            stop.set()

    task = asyncio.create_task(
        run_loop(
            tick,
            interval_seconds=lambda: interval,
            market_hours_only=False,
            stop=stop,
            wake_event=wake,
        )
    )

    # First tick runs immediately
    await asyncio.sleep(0.02)
    assert len(calls) == 1

    # Now change interval to 0.02s and wake
    interval = 0.02
    wake.set()

    await asyncio.wait_for(task, timeout=0.2)
    assert len(calls) >= 2


def test_decision_window_start():
    assert decision_window_start(15) == time(9, 45)
    assert decision_window_start(0) == time(9, 30)
    assert decision_window_start(30) == time(10, 0)
    assert decision_window_start(-5) == time(9, 30)


def test_decision_window_open_weekday():
    # 2026-06-01 is a Monday
    assert not is_decision_window_open(datetime(2026, 6, 1, 9, 29, 59, tzinfo=ET))
    # At 9:30 market is open, but decision window is closed for IV stabilization buffer (+15m)
    assert not is_decision_window_open(datetime(2026, 6, 1, 9, 30, 0, tzinfo=ET))
    assert not is_decision_window_open(datetime(2026, 6, 1, 9, 44, 59, tzinfo=ET))
    # Exactly at 9:45, decision window opens
    assert is_decision_window_open(datetime(2026, 6, 1, 9, 45, 0, tzinfo=ET))
    assert is_decision_window_open(datetime(2026, 6, 1, 12, 0, 0, tzinfo=ET))
    assert is_decision_window_open(datetime(2026, 6, 1, 16, 0, 0, tzinfo=ET))
    # After 16:00, closed
    assert not is_decision_window_open(datetime(2026, 6, 1, 16, 0, 1, tzinfo=ET))


def test_decision_window_closed_on_weekend():
    # 2026-06-06 is a Saturday
    assert not is_decision_window_open(datetime(2026, 6, 6, 10, 0, 0, tzinfo=ET))
    # 2026-06-07 is a Sunday
    assert not is_decision_window_open(datetime(2026, 6, 7, 10, 0, 0, tzinfo=ET))


def test_seconds_until_decision_window():
    # Monday morning 09:30:00 -> 15 minutes (900 seconds) until 09:45:00
    secs = seconds_until_decision_window(
        datetime(2026, 6, 1, 9, 30, 0, tzinfo=ET), open_delay_minutes=15
    )
    assert secs == 900.0

    # Monday 09:40:00 -> 5 minutes (300 seconds)
    secs = seconds_until_decision_window(
        datetime(2026, 6, 1, 9, 40, 0, tzinfo=ET), open_delay_minutes=15
    )
    assert secs == 300.0

    # Already within window at 09:45:00 -> 0.0
    secs = seconds_until_decision_window(
        datetime(2026, 6, 1, 9, 45, 0, tzinfo=ET), open_delay_minutes=15
    )
    assert secs == 0.0


async def test_run_loop_waits_for_iv_stabilization_when_market_open_buffer(monkeypatch):
    """When market_hours_only is True and market is open but in first 15 mins, run_loop delays tick."""
    import time

    stop = asyncio.Event()
    calls = []

    # Mock datetime.now(ET) progression:
    # 1. 09:30:00 ET (market open, decision window closed -> wait 0.05s)
    # 2. 09:45:00 ET (decision window opens -> execute tick)
    times = [
        datetime(2026, 6, 1, 9, 30, 0, tzinfo=ET),
        datetime(2026, 6, 1, 9, 45, 0, tzinfo=ET),
        datetime(2026, 6, 1, 9, 45, 1, tzinfo=ET),
    ]

    class FakeDatetime:
        @classmethod
        def now(cls, tz=None):
            if times:
                return times.pop(0)
            return datetime(2026, 6, 1, 9, 45, 2, tzinfo=ET)

    monkeypatch.setattr("tastyagent.scheduler.datetime", FakeDatetime)
    # Mock seconds_until_decision_window to return a very small wait for unit testing
    monkeypatch.setattr(
        "tastyagent.scheduler.seconds_until_decision_window",
        lambda *args, **kwargs: 0.04,
    )

    async def tick():
        calls.append(time.time())
        stop.set()

    t0 = time.time()
    await run_loop(
        tick,
        interval_seconds=0.1,
        market_hours_only=True,
        open_delay_minutes=15,
        stop=stop,
    )
    elapsed = time.time() - t0
    # Must have waited through the IV stabilization buffer (~0.04s) before the first tick
    assert len(calls) == 1
    assert elapsed >= 0.03

