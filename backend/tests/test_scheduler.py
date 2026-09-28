import asyncio
from datetime import datetime, timedelta

from tastyagent.scheduler import ET, is_market_open, run_loop


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
