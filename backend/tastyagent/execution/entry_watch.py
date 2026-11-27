"""Resolve entry orders the placer stopped managing, 15 minutes after placement.

The placer gives up on an order when IB does not confirm a cancel in time, or
when ``placeOrder`` fails after the order was journaled. The intent then stays
non-terminal and its row WORKING. This check asks IB directly: an order that is
still working is canceled, a finished order is settled from IB's own status, and
an order IB never saw is treated as unfilled unless the account holds its legs.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from ..db.models import ExecutionOrder, Trade, TradeStatus
from .reconciliation import reconcile_broker_activity

ENTRY_CHECK_AFTER = timedelta(minutes=15)
ACTIVE_STATUSES = frozenset(
    {"ApiPending", "PendingSubmit", "PreSubmitted", "Submitted"}
)
UNSET_QUANTITY = 1e300  # ib_async marks unknown doubles with sys.float_info.max

logger = logging.getLogger(__name__)
_flagged: set[str] = set()


async def check_unresolved_entries(
    ledger: Any,
    ib: Any,
    account: str,
    fills: list[Any],
    positions: list[Any],
    *,
    now: datetime | None = None,
) -> int:
    """Resolve entry intents left non-terminal for ENTRY_CHECK_AFTER; return how many settled."""
    now = now or datetime.now(UTC)
    stale = _stale_entries(ledger, account, now - ENTRY_CHECK_AFTER)
    if not stale:
        return 0
    views = {_ref(t): _finished_view(t) for t in await ib.reqCompletedOrdersAsync(True)}
    views.update({_ref(t): t for t in await ib.reqAllOpenOrdersAsync()})
    refs = {intent.order_ref for intent in stale}
    owned_fills = [f for f in fills if getattr(f.execution, "orderRef", "") in refs]
    reconcile_broker_activity(
        ledger,
        [views[ref] for ref in refs if ref in views],
        owned_fills,
        account=account,
    )
    executed = {f.execution.orderRef for f in owned_fills}
    settled = 0
    for intent in stale:
        ledger.s.refresh(intent)
        if intent.terminal:
            settled += 1
            continue
        broker = views.get(intent.order_ref)
        settled += _resolve(ledger, ib, intent, broker, executed, positions)
    return settled


def _resolve(ledger, ib, intent, broker, executed, positions) -> int:
    """Act on one unresolved intent; return 1 when it was settled."""
    trade = ledger.get(intent.trade_id)
    status = broker.orderStatus.status if broker is not None else ""
    if status in ACTIVE_STATUSES:
        ib.cancelOrder(broker.order)
        ledger.record_event(
            trade,
            "order_check",
            f"Entry order {intent.order_ref} still {status} after placement "
            "stopped; cancel sent",
        )
        return 0
    unknown = broker is None and intent.order_ref not in executed
    if unknown and not _holds_any_leg(trade, positions):
        _mark_unfilled(ledger, intent, trade)
        return 1
    _flag_for_review(intent, status or "unknown at IB")
    return 0


def _stale_entries(ledger, account, cutoff: datetime) -> list[ExecutionOrder]:
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
                Trade.status == TradeStatus.WORKING,
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


def _mark_unfilled(ledger, intent: ExecutionOrder, trade: Trade) -> None:
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


def _flag_for_review(intent: ExecutionOrder, status: str) -> None:
    if intent.order_ref in _flagged:
        return
    _flagged.add(intent.order_ref)
    logger.warning(
        "Entry order %s (trade #%s) needs review: IB status %s, outcome unverified",
        intent.order_ref,
        intent.trade_id,
        status,
    )
