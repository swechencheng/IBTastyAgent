"""Market-hours decision loop.

A lightweight async loop that invokes an async ``tick`` callable on an interval,
optionally gated to regular US equity market hours. The tick (wired in the app
layer) runs one full cycle: gather state -> orchestrate -> execute -> reconcile.

Holiday awareness is intentionally omitted here; TastyTrade's market-sessions
endpoint can refine ``is_market_open`` later. Kept free of broker/LLM imports so
the gating logic stays simple and the loop is easy to drive from the API or a CLI.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
import logging
import time as time_mod
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
DEFAULT_OPEN_DELAY_MINUTES = 15

logger = logging.getLogger("tastyagent.scheduler")


def is_market_open(now: datetime | None = None) -> bool:
    """True during regular US equity hours (weekday 9:30-16:00 ET). No holidays."""
    now = (now or datetime.now(ET)).astimezone(ET)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def decision_window_start(open_delay_minutes: int = DEFAULT_OPEN_DELAY_MINUTES) -> time:
    """Return the time of day when decision making begins (09:30 + open_delay_minutes ET)."""
    delay = max(0, int(open_delay_minutes))
    total_minutes = 9 * 60 + 30 + delay
    return time((total_minutes // 60) % 24, total_minutes % 60)


def is_decision_window_open(
    now: datetime | None = None,
    open_delay_minutes: int = DEFAULT_OPEN_DELAY_MINUTES,
) -> bool:
    """True during regular US equity hours after the opening IV stabilization delay.

    Weekday (09:30 + open_delay_minutes) to 16:00 ET. For example, with
    open_delay_minutes=15, the window is 09:45-16:00 ET.
    """
    now = (now or datetime.now(ET)).astimezone(ET)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    start_time = decision_window_start(open_delay_minutes)
    return start_time <= now.time() <= MARKET_CLOSE


def seconds_until_decision_window(
    now: datetime | None = None,
    open_delay_minutes: int = DEFAULT_OPEN_DELAY_MINUTES,
) -> float:
    """Seconds from `now` until the decision window opens.

    If currently inside the decision window (e.g. 09:45-16:00 ET on a weekday), returns 0.0.
    If today is a weekday and before the decision window (e.g. between 09:30 and 09:45, or earlier),
    returns the seconds until today's decision window open.
    If after market close today, or on a weekend, returns the seconds until the next trading day's
    decision window open.
    """
    now = (now or datetime.now(ET)).astimezone(ET)
    if is_decision_window_open(now, open_delay_minutes):
        return 0.0

    start_time = decision_window_start(open_delay_minutes)

    if now.weekday() < 5 and now.time() < start_time:
        target_dt = now.replace(
            hour=start_time.hour,
            minute=start_time.minute,
            second=start_time.second,
            microsecond=0,
        )
        return max(0.0, (target_dt - now).total_seconds())

    # Find the next weekday trading day
    days_ahead = 1
    next_day = now + timedelta(days=days_ahead)
    while next_day.weekday() >= 5:
        days_ahead += 1
        next_day = now + timedelta(days=days_ahead)

    target_dt = next_day.replace(
        hour=start_time.hour,
        minute=start_time.minute,
        second=start_time.second,
        microsecond=0,
    )
    return max(0.0, (target_dt - now).total_seconds())


async def _sleep_interruptible(
    timeout: float,
    stop: asyncio.Event,
    wake_event: asyncio.Event | None = None,
) -> None:
    """Sleep for ``timeout`` seconds, waking early if ``stop`` or ``wake_event`` fires."""
    if timeout <= 0 or stop.is_set():
        return
    if wake_event is not None:
        wake_event.clear()
        stop_task = asyncio.create_task(stop.wait())
        wake_task = asyncio.create_task(wake_event.wait())
        try:
            await asyncio.wait(
                [stop_task, wake_task],
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.TimeoutError:
            pass
        finally:
            for p in (stop_task, wake_task):
                if not p.done():
                    p.cancel()
    else:
        try:
            await asyncio.wait_for(stop.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass


async def run_loop(
    tick: Callable[[], Awaitable[None]],
    *,
    interval_seconds: float | Callable[[], float] = 300.0,
    market_hours_only: bool | Callable[[], bool] = True,
    open_delay_minutes: int | Callable[[], int] = DEFAULT_OPEN_DELAY_MINUTES,
    stop: asyncio.Event | None = None,
    wake_event: asyncio.Event | None = None,
    get_last_tick_time: Callable[[], float | None] | None = None,
) -> None:
    """Run ``tick`` every ``interval_seconds`` until ``stop`` is set.

    Supports dynamic interval and market_hours getters (callables), early sleep
    wake-up events (e.g. settings changed in dashboard), and avoids duplicate ticks
    on start/restart if a cycle completed recently.

    When ``market_hours_only`` is active, the first decision cycle of any trading
    day will wait until at least ``open_delay_minutes`` (default 15m) after market
    open (09:45 ET) so option implied volatility and bid-ask spreads have stabilized.
    """
    stop = stop or asyncio.Event()

    while not stop.is_set():
        current_iv = (
            interval_seconds() if callable(interval_seconds) else interval_seconds
        )
        current_iv = max(0.001, float(current_iv))

        # Check if we should wait because a cycle ran recently
        if get_last_tick_time is not None:
            last_t = get_last_tick_time()
            if last_t is not None:
                elapsed = time_mod.time() - last_t
                remaining = current_iv - elapsed
                if remaining > 0:
                    await _sleep_interruptible(remaining, stop, wake_event)
                    continue

        if stop.is_set():
            break

        mh_only = (
            market_hours_only() if callable(market_hours_only) else market_hours_only
        )
        delay_mins = (
            open_delay_minutes() if callable(open_delay_minutes) else open_delay_minutes
        )
        delay_mins = max(0, int(delay_mins))

        now_et = datetime.now(ET)
        can_run = not mh_only or is_decision_window_open(now_et, delay_mins)

        if can_run:
            try:
                await tick()
            except Exception as e:  # noqa: BLE001 - never let one cycle kill the loop
                logger.exception("cycle failed: %s", e)
        elif mh_only and is_market_open(now_et):
            # Market is open (e.g. 09:30-09:45 ET), but we must wait for IV stabilization buffer
            # before making the first decision of the day.
            wait_secs = seconds_until_decision_window(now_et, delay_mins)
            if wait_secs > 0:
                target_str = decision_window_start(delay_mins).strftime("%H:%M ET")
                logger.info(
                    "Market opened at 09:30 ET; waiting %.0fs until %s (+%dm IV stabilization buffer) before first decision...",
                    wait_secs,
                    target_str,
                    delay_mins,
                )
                await _sleep_interruptible(wait_secs, stop, wake_event)
                continue

        if stop.is_set():
            break

        # Re-fetch interval in case it changed during tick execution
        current_iv = (
            interval_seconds() if callable(interval_seconds) else interval_seconds
        )
        current_iv = max(0.001, float(current_iv))

        await _sleep_interruptible(current_iv, stop, wake_event)
