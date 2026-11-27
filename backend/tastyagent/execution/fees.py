"""Commission capture diagnostics and the once-a-day after-close catch-up.

IB delivers an execution's commission report once per connection. A report the
live session misses stays missing until the trading session reconnects, when
IB re-sends today's executions together with their commissions.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select

from .. import market_calendar
from ..db.models import BrokerExecution, ExecutionOrder

FEE_GRACE = timedelta(minutes=10)
IN_FLIGHT_WINDOW = timedelta(minutes=15)
REPORT_SETTLE_SECONDS = 5.0
RECONNECT_RETRY_WAIT_SECONDS = 15.0
MAX_LISTED = 10

logger = logging.getLogger(__name__)
_warned: set[str] = set()


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def note_rejected_commission(exec_id: str, report: Any) -> None:
    """Warn once when IB's commission report for an owned execution cannot be booked."""
    key = f"rejected:{exec_id}"
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(
        "Ignoring commission report for execution %s: commission=%r currency=%r",
        exec_id,
        getattr(report, "commission", None),
        getattr(report, "currency", None),
    )


def missing_fee_executions(
    session: Any, *, since: datetime, before: datetime
) -> list[BrokerExecution]:
    """Owned option executions without a commission (combo BAG rows never carry one)."""
    return list(
        session.scalars(
            select(BrokerExecution).where(
                BrokerExecution.leg != "BAG",
                BrokerExecution.fee.is_(None),
                BrokerExecution.ts >= _naive_utc(since),
                BrokerExecution.ts <= _naive_utc(before),
            )
        )
    )


def warn_missing_fees(session: Any, now: datetime) -> int:
    """Warn once per execution still missing its commission FEE_GRACE after the fill."""
    rows = missing_fee_executions(
        session, since=now - timedelta(days=1), before=now - FEE_GRACE
    )
    overdue = [row.exec_id for row in rows if f"missing:{row.exec_id}" not in _warned]
    if overdue:
        _warned.update(f"missing:{exec_id}" for exec_id in overdue)
        logger.warning(
            "Commission missing for %d broker execution(s) %d+ min after the fill: %s",
            len(overdue),
            int(FEE_GRACE.total_seconds() // 60),
            ", ".join(overdue[:MAX_LISTED]),
        )
    return len(rows)


def orders_in_flight(session: Any, now: datetime) -> bool:
    """True while a recent entry, close or roll order may still be managed live."""
    recent = session.scalar(
        select(ExecutionOrder.id)
        .where(
            ExecutionOrder.terminal.is_(False),
            ExecutionOrder.kind.in_(("entry", "close", "roll")),
            ExecutionOrder.created_at >= _naive_utc(now - IN_FLIGHT_WINDOW),
        )
        .limit(1)
    )
    return recent is not None


def _scan(
    open_session: Callable[[], Any], day: date, now: datetime
) -> tuple[int, bool]:
    """Count today's executions missing commissions and check for orders in flight."""
    session = open_session()
    try:
        since = datetime.combine(day, time(0), tzinfo=market_calendar.ET)
        missing = missing_fee_executions(session, since=since, before=now - FEE_GRACE)
        return len(missing), orders_in_flight(session, now)
    finally:
        session.close()


def _needs_recovery(open_session: Callable[[], Any], day: date, now: datetime) -> int:
    """Commissions missing past the grace period, or 0 while an order is in flight."""
    missing, busy = _scan(open_session, day, now)
    return 0 if busy else missing


@dataclass
class FeeRecovery:
    """Reconnect once per trading day after the close when commissions are missing."""

    settle_seconds: float = REPORT_SETTLE_SECONDS
    retry_wait_seconds: float = RECONNECT_RETRY_WAIT_SECONDS
    last_day: date | None = None

    async def run(
        self,
        client: Any,
        open_session: Callable[[], Any],
        resync: Callable[[], Awaitable[Any]],
        *,
        now: datetime | None = None,
        live: bool = False,
    ) -> bool:
        """Return True when a reconnect was attempted.

        A live session is never reconnected: the live gateway also carries the
        market data and the trading connection.
        """
        now = now or datetime.now(UTC)
        day = now.astimezone(market_calendar.ET).date()
        hours = market_calendar.session_on(day)
        if live or hours is None or now < hours[1] or self.last_day == day:
            return False
        if not _needs_recovery(open_session, day, now):
            return False
        await resync()  # the live session may already hold the reports
        missing = _needs_recovery(open_session, day, now)
        if not missing:
            return False
        self.last_day = day
        logger.warning(
            "Reconnecting the trading session after the close to recover "
            "%d missing commission(s)",
            missing,
        )
        reconnected = await client.reconnect_trading()
        if not reconnected:
            logger.warning(
                "Trading session reconnect failed; resyncing after the keep-alive "
                "monitor reconnects"
            )
        await asyncio.sleep(
            self.settle_seconds if reconnected else self.retry_wait_seconds
        )
        await resync()
        still, _ = _scan(open_session, day, now)
        report = logger.warning if still else logger.info
        report("Commission catch-up finished: %d of %d still missing", still, missing)
        return True
