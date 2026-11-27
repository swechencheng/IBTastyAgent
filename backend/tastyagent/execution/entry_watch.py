"""Resolve entry orders the placer stopped managing.

The placer gives up on an order when IB does not confirm a cancel in time, or
when ``placeOrder`` fails after the order was journaled. The intent then stays
non-terminal. Fifteen minutes after placement (at once for intents journaled
before this process started) this check asks IB directly: an order that is still
working is canceled, a finished order is settled from IB's own status, and an
order IB never saw is treated as unfilled only on strong evidence (same ET day,
a fresh execution history, none of its legs held). Anything else is recorded
for review.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from ..db.models import ExecutionOrder, Trade, TradeStatus
from ..market_calendar import ET
from .reconciliation import reconcile_broker_activity

ENTRY_CHECK_AFTER = timedelta(minutes=15)
IB_REQUEST_TIMEOUT_SECONDS = 10.0
ACTIVE_STATUSES = frozenset(
    {"ApiPending", "PendingSubmit", "PreSubmitted", "Submitted"}
)
UNSET_QUANTITY = 1e300  # ib_async marks unknown doubles with sys.float_info.max
# Intents journaled before this process started have no placer managing them.
PROCESS_STARTED_AT = datetime.now(UTC)

logger = logging.getLogger(__name__)
_noted: set[str] = set()


async def check_unresolved_entries(
    ledger: Any,
    ib: Any,
    account: str,
    fills: list[Any],
    positions: list[Any],
    *,
    executions_verified: bool = False,
    now: datetime | None = None,
    unmanaged_before: datetime | None = None,
) -> int:
    """Resolve entry intents the placer no longer manages; return how many settled.

    ``executions_verified`` means this cycle's execution history came from IB,
    not a cache. Without it no order is declared unfilled.
    """
    now = now or datetime.now(UTC)
    cutoff = max(now - ENTRY_CHECK_AFTER, unmanaged_before or PROCESS_STARTED_AT)
    stale = _stale_entries(ledger, account, cutoff)
    if not stale:
        return 0
    views = await _broker_views(ib)
    if views is None:
        return 0
    refs = {intent.order_ref for intent in stale}
    owned = [f for f in fills if getattr(f.execution, "orderRef", "") in refs]
    reconcile_broker_activity(
        ledger, [views[ref] for ref in refs if ref in views], owned, account=account
    )
    evidence = SimpleNamespace(
        views=views,
        executed={f.execution.orderRef for f in owned},
        positions=positions,
        verified=executions_verified,
        today=now.astimezone(ET).date(),
    )
    settled = 0
    for intent in stale:
        try:
            ledger.s.refresh(intent)
            settled += 1 if intent.terminal else _resolve(ledger, ib, intent, evidence)
        except Exception:
            ledger.s.rollback()
            logger.warning("Entry check failed for %s", intent.order_ref, exc_info=True)
    return settled


async def _broker_views(ib: Any) -> dict[str, Any] | None:
    """IB's completed and open orders by orderRef, or None when IB did not answer.

    ib_async keys these requests by name, so an overlapping request elsewhere can
    orphan ours; the timeout keeps the poller from waiting forever.
    """
    try:
        completed = await asyncio.wait_for(
            ib.reqCompletedOrdersAsync(True), IB_REQUEST_TIMEOUT_SECONDS
        )
        open_orders = await asyncio.wait_for(
            ib.reqAllOpenOrdersAsync(), IB_REQUEST_TIMEOUT_SECONDS
        )
    except TimeoutError:
        logger.warning("IB order lookup timed out; entry check skipped this cycle")
        return None
    views = {_ref(t): _finished_view(t) for t in completed}
    views.update({_ref(t): t for t in open_orders})
    return views


def _resolve(ledger: Any, ib: Any, intent: ExecutionOrder, evidence: Any) -> int:
    """Act on one unresolved intent; return 1 when it was settled."""
    trade = ledger.get(intent.trade_id)
    broker = evidence.views.get(intent.order_ref)
    status = broker.orderStatus.status if broker is not None else ""
    if status in ACTIVE_STATUSES:
        ib.cancelOrder(broker.order)
        _note(
            ledger,
            trade,
            intent,
            "cancel",
            f"Entry order {intent.order_ref} still {status} after placement "
            "stopped; cancel sent",
        )
        return 0
    if broker is None and _provably_unfilled(intent, trade, evidence):
        _mark_unfilled(ledger, intent, trade)
        return 1
    _note(
        ledger,
        trade,
        intent,
        "review",
        f"Entry order {intent.order_ref} needs review: IB status "
        f"{status or 'unknown'}, outcome unverified",
    )
    return 0


def _provably_unfilled(intent: ExecutionOrder, trade: Trade, evidence: Any) -> bool:
    """Absence at IB proves nothing unless today's executions were fetched fresh."""
    created = intent.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (
        evidence.verified
        and intent.order_ref not in evidence.executed
        and not intent.applied
        and trade.status is TradeStatus.WORKING
        and created.astimezone(ET).date() == evidence.today
        and not _holds_any_leg(trade, evidence.positions)
    )


def _stale_entries(ledger: Any, account: str, cutoff: datetime) -> list[ExecutionOrder]:
    return list(
        ledger.s.scalars(
            select(ExecutionOrder)
            .join(Trade, Trade.id == ExecutionOrder.trade_id)
            .where(
                ExecutionOrder.kind == "entry",
                ExecutionOrder.terminal.is_(False),
                ExecutionOrder.account == account,
                ExecutionOrder.created_at
                <= cutoff.astimezone(UTC).replace(tzinfo=None),
                Trade.status.in_((TradeStatus.WORKING, TradeStatus.OPEN)),
            )
        )
    )


def _ref(broker_trade: Any) -> str:
    return getattr(getattr(broker_trade, "order", None), "orderRef", "") or ""


def _finished_view(broker_trade: Any) -> SimpleNamespace:
    """IB's completed orders carry no fill count in their status; use the order's."""
    quantity = getattr(broker_trade.order, "filledQuantity", None)
    known = (
        isinstance(quantity, (int, float))
        and math.isfinite(quantity)
        and abs(quantity) < UNSET_QUANTITY
    )
    status = SimpleNamespace(
        status=broker_trade.orderStatus.status,
        filled=float(quantity) if known else None,
        permId=getattr(broker_trade.order, "permId", 0),
    )
    return SimpleNamespace(
        order=broker_trade.order,
        orderStatus=status,
        fills=[],
        log=list(getattr(broker_trade, "log", None) or []),
    )


def _contract_key(symbol: Any, right: Any, strike: Any, expiry: Any) -> tuple:
    return (
        str(symbol).upper(),
        str(right)[:1].upper(),
        round(float(strike), 4),
        str(expiry).replace("-", "")[:8],
    )


def _holds_any_leg(trade: Trade, positions: list[Any]) -> bool:
    held = {
        _contract_key(
            p.contract.symbol,
            p.contract.right,
            p.contract.strike,
            p.contract.lastTradeDateOrContractMonth,
        )
        for p in positions or []
        if getattr(p, "position", 0) and getattr(p.contract, "secType", "") == "OPT"
    }
    return any(
        _contract_key(
            trade.symbol,
            getattr(leg.option_type, "value", leg.option_type),
            leg.strike,
            leg.expiration,
        )
        in held
        for leg in trade.legs
    )


def _mark_unfilled(ledger: Any, intent: ExecutionOrder, trade: Trade) -> None:
    intent.status, intent.terminal, intent.management_slot = "NotFound", True, None
    if trade.broker_order_id and trade.broker_order_id == intent.order_id:
        trade.broker_order_id = None
    ledger.s.commit()
    ledger.record_event(
        trade,
        "order_check",
        f"Entry order {intent.order_ref} unknown at IB (no open or completed order, "
        "no executions); treated as unfilled",
    )


def _note(
    ledger: Any, trade: Trade, intent: ExecutionOrder, kind: str, detail: str
) -> None:
    """Record one order_check event per order and kind; reviews also warn."""
    key = f"{kind}:{intent.order_ref}"
    if key in _noted:
        return
    _noted.add(key)
    ledger.record_event(trade, "order_check", detail)
    if kind == "review":
        logger.warning("%s (trade #%s)", detail, intent.trade_id)
