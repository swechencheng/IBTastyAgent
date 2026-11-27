"""Market-hours decision loop.

A lightweight async loop that invokes an async ``tick`` callable on an interval,
optionally gated to regular US equity market hours. The tick (wired in the app
layer) runs one full cycle: gather state -> orchestrate -> execute -> reconcile.

Session hours come from ``market_calendar``: IB's published hours, including
holidays and early closes, with weekday 09:30-16:00 ET for days IB has not
described. Kept free of broker/LLM imports so the gating logic stays simple and
the loop is easy to drive from the API or a CLI.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta
import logging
import time as time_mod
from typing import Awaitable, Callable

from .market_calendar import ET, session_on

DEFAULT_OPEN_DELAY_MINUTES = 15
SESSION_SEARCH_DAYS = 14

logger = logging.getLogger("tastyagent.scheduler")


def is_market_open(now: datetime | None = None) -> bool:
    """True during the day's regular session (IB hours, else weekday 9:30-16:00 ET)."""
    now = (now or datetime.now(ET)).astimezone(ET)
    session = session_on(now.date())
    return session is not None and session[0] <= now <= session[1]


def decision_window_start(open_delay_minutes: int = DEFAULT_OPEN_DELAY_MINUTES) -> time:
    """Return the time of day when decision making begins (09:30 + open_delay_minutes ET)."""
    delay = max(0, int(open_delay_minutes))
    total_minutes = 9 * 60 + 30 + delay
    return time((total_minutes // 60) % 24, total_minutes % 60)


def _open_delay(open_delay_minutes: int) -> timedelta:
    return timedelta(minutes=max(0, int(open_delay_minutes)))


def is_decision_window_open(
    now: datetime | None = None,
    open_delay_minutes: int = DEFAULT_OPEN_DELAY_MINUTES,
) -> bool:
    """True inside the day's session once the opening IV stabilization delay has passed.

    With open_delay_minutes=15 a regular day's window is 09:45-16:00 ET; an
    early-close day's window ends at the early close, and holidays have none.
    """
    now = (now or datetime.now(ET)).astimezone(ET)
    session = session_on(now.date())
    if session is None:
        return False
    return session[0] + _open_delay(open_delay_minutes) <= now <= session[1]


def seconds_until_decision_window(
    now: datetime | None = None,
    open_delay_minutes: int = DEFAULT_OPEN_DELAY_MINUTES,
) -> float:
    """Seconds from ``now`` until the next decision window opens; 0.0 inside one.

    Skips weekends, exchange holidays and days whose window has already passed.
    """
    now = (now or datetime.now(ET)).astimezone(ET)
    if is_decision_window_open(now, open_delay_minutes):
        return 0.0
    delay = _open_delay(open_delay_minutes)
    for days_ahead in range(SESSION_SEARCH_DAYS):
        session = session_on(now.date() + timedelta(days=days_ahead))
        if session is not None and session[0] + delay > now:
            return (session[0] + delay - now.astimezone(UTC)).total_seconds()
    return 0.0


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
