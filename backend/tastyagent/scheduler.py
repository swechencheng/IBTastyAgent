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
from datetime import datetime, time
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def is_market_open(now: datetime | None = None) -> bool:
    """True during regular US equity hours (weekday 9:30-16:00 ET). No holidays."""
    now = (now or datetime.now(ET)).astimezone(ET)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


import time as time_mod


async def run_loop(
    tick: Callable[[], Awaitable[None]],
    *,
    interval_seconds: float | Callable[[], float] = 300.0,
    market_hours_only: bool | Callable[[], bool] = True,
    stop: asyncio.Event | None = None,
    wake_event: asyncio.Event | None = None,
    get_last_tick_time: Callable[[], float | None] | None = None,
) -> None:
    """Run ``tick`` every ``interval_seconds`` until ``stop`` is set.

    Supports dynamic interval and market_hours getters (callables), early sleep
    wake-up events (e.g. settings changed in dashboard), and avoids duplicate ticks
    on start/restart if a cycle completed recently.
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
                    if wake_event is not None:
                        wake_event.clear()
                        stop_task = asyncio.create_task(stop.wait())
                        wake_task = asyncio.create_task(wake_event.wait())
                        try:
                            done, pending = await asyncio.wait(
                                [stop_task, wake_task],
                                timeout=remaining,
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
                            await asyncio.wait_for(stop.wait(), timeout=remaining)
                        except asyncio.TimeoutError:
                            pass
                    continue

        if stop.is_set():
            break

        mh_only = (
            market_hours_only() if callable(market_hours_only) else market_hours_only
        )
        if not mh_only or is_market_open():
            try:
                await tick()
            except Exception as e:  # noqa: BLE001 - never let one cycle kill the loop
                import logging

                logging.getLogger("tastyagent.scheduler").exception(
                    "cycle failed: %s", e
                )

        if stop.is_set():
            break

        # Re-fetch interval in case it changed during tick execution
        current_iv = (
            interval_seconds() if callable(interval_seconds) else interval_seconds
        )
        current_iv = max(0.001, float(current_iv))

        if wake_event is not None:
            wake_event.clear()
            stop_task = asyncio.create_task(stop.wait())
            wake_task = asyncio.create_task(wake_event.wait())
            try:
                done, pending = await asyncio.wait(
                    [stop_task, wake_task],
                    timeout=current_iv,
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
                await asyncio.wait_for(stop.wait(), timeout=current_iv)
            except asyncio.TimeoutError:
                pass
