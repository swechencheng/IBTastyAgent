"""Fill tracking and exit application — reconcile our ledger with broker state.

Kept broker-agnostic: callers pass plain dicts (order-id -> status, trade-id -> mark)
gathered from the broker, so this is deterministic and unit-testable. The exit
decisions themselves come from ``strategy/exits.py``; this module just applies them
to the ledger.
"""

from __future__ import annotations

from datetime import UTC, datetime, timezone
import logging
from typing import Any, Callable, Optional

from sqlalchemy import select

from ..config import TradingMode
from ..risk.session import account_modes
from ..db.models import EquitySnapshot, ExecutionOrder, Trade, TradeStatus
from .entry_watch import check_unresolved_entries
from .fees import warn_missing_fees
from .reconciliation import broker_account, reconcile_broker_activity
from ..portfolio.ledger import Ledger
from ..portfolio.pnl import summarize


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _has_matching_broker_order(
    trade: Trade, ib_trades: Optional[list[Any]]
) -> bool:
    """Return True if broker reports a non-canceled order matching this trade's orderRef."""
    if not ib_trades:
        return False
    expected_ref = trade.order_ref or (
        f"TastyAgent_{trade.id}" if trade.id is not None else ""
    )
    if not expected_ref:
        return False
    for bt in ib_trades:
        order = getattr(bt, "order", None)
        ref = getattr(order, "orderRef", "") or ""
        if ref == expected_ref:
            status = getattr(getattr(bt, "orderStatus", None), "status", "")
            if status not in ("Cancelled", "ApiCancelled", "Inactive"):
                return True
    return False


def _has_matching_broker_position(
    trade: Trade, positions: Optional[list[Any]]
) -> bool:
    """Return True if any non-zero broker option position exists for this trade's symbol."""
    if not positions:
        return False
    for pos in positions:
        contract = getattr(pos, "contract", None)
        qty = getattr(pos, "position", 0) or 0
        if (
            qty != 0
            and getattr(contract, "symbol", None) == trade.symbol
            and getattr(contract, "secType", None) == "OPT"
        ):
            return True
    return False


def reconcile_stale_working_trades(
    ledger: Ledger,
    ib_trades: Optional[list[Any]] = None,
    positions: Optional[list[Any]] = None,
    *,
    stale_after_seconds: float = 600.0,
    now: Optional[datetime] = None,
    mode: TradingMode | None = None,
) -> int:
    """Cancel stale WORKING entries that have no live or filled broker order.

    A journaled entry is settled by its own orders: once the broker has ended every
    attempt with nothing applied, other lots on the same underlying prove nothing.
    Only unjournaled legacy rows fall back to the symbol-level position check.
    """
    current_time = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    canceled = 0
    modes = account_modes(mode) if isinstance(mode, TradingMode) else None
    for trade in ledger.open_trades():
        if modes is not None and trade.mode not in modes:
            continue
        if trade.status != TradeStatus.WORKING or trade.broker_order_id:
            continue
        orders = list(
            ledger.s.scalars(
                select(ExecutionOrder).where(ExecutionOrder.trade_id == trade.id)
            )
        )
        if any(not order.terminal or order.applied for order in orders):
            continue
        if _has_matching_broker_order(trade, ib_trades):
            continue
        if not orders and _has_matching_broker_position(trade, positions):
            continue
        last_activity = max(
            _as_utc(ts) for ts in (trade.created_at, *(o.created_at for o in orders))
        )
        age_seconds = (current_time - last_activity).total_seconds()
        if age_seconds < stale_after_seconds:
            continue
        reason = (
            f"entry never filled: {len(orders)} broker order(s) ended unfilled"
            if orders
            else f"stale WORKING trade with no broker_order_id (age={int(age_seconds)}s)"
        )
        ledger.cancel_trade(trade, reason=reason)
        canceled += 1
    return canceled

logger = logging.getLogger(__name__)

# Broker order states we treat as filled vs dead.
FILLED = {"Filled", "filled"}
DEAD = {
    "Cancelled",
    "Canceled",
    "Rejected",
    "Expired",
    "Removed",
    "rejected",
    "expired",
}


def reconcile_fills(ledger: Ledger, order_status: dict[str, str]) -> None:
    """Compatibility entry point: unscoped order IDs/statuses cannot settle an owned lot."""
    return None

def reconcile_closing_orders(
    ledger: Ledger,
    ib_trades: list[Any],
    active_ib: Any = None,
) -> int:
    """Legacy sync adapter. Only owned execution reports may close a ledger lot."""
    accounts = active_ib.managedAccounts() if active_ib and hasattr(active_ib, "managedAccounts") else []
    if len(accounts) != 1:
        return 0
    fills = active_ib.fills() if hasattr(active_ib, "fills") else []
    return reconcile_broker_activity(ledger, ib_trades, fills, account=accounts[0])


def reconcile_positions_from_broker(ledger: Ledger, positions: list[Any]) -> int:
    """Account-level positions do not establish ownership of a strategy execution."""
    return 0


def calculate_marks_from_portfolio(
    trades: list[Trade], portfolio: list[Any]
) -> dict[int, float]:
    """Calculate net cost-to-close for trades from broker portfolio items.

    For credit positions:
    - short leg (sell_to_open): cost to close increases by marketPrice
    - long leg (buy_to_open): cost to close decreases by marketPrice
    """
    if not portfolio:
        return {}

    port_map = {}
    for p in portfolio:
        c = getattr(p, "contract", None)
        if c is not None:
            sym = getattr(c, "symbol", "").upper()
            right = getattr(c, "right", "").upper()
            strike = round(float(getattr(c, "strike", 0.0)), 2)
            exp = str(getattr(c, "lastTradeDateOrContractMonth", "") or "").replace(
                "-", ""
            )
            port_map[(sym, right, strike, exp)] = p

    marks: dict[int, float] = {}
    for trade in trades:
        if not trade.legs:
            continue
        cost_to_close = 0.0
        all_legs_found = True
        for leg in trade.legs:
            right = "P" if leg.option_type.lower() == "put" else "C"
            strike = round(float(leg.strike), 2)
            exp = str(leg.expiration).replace("-", "")
            key = (trade.symbol.upper(), right, strike, exp)
            p = port_map.get(key)
            if p is not None and getattr(p, "marketPrice", None) is not None:
                mkt_price = float(p.marketPrice)
                if "sell" in str(leg.action).lower():
                    cost_to_close += mkt_price * 100 * leg.quantity
                else:
                    cost_to_close -= mkt_price * 100 * leg.quantity
            else:
                all_legs_found = False
                break
        if all_legs_found:
            marks[trade.id] = max(0.0, round(cost_to_close, 2))

    return marks


def apply_marks(ledger: Ledger, marks: dict[int, float]) -> None:
    """Update the current cost-to-close (mark) for open trades by trade id."""
    for trade in ledger.open_trades():
        if trade.id in marks:
            ledger.update_mark(trade, marks[trade.id])


async def sync_positions_and_marks(
    *,
    client: Any,
    session: Any,
    runtime: Any = None,
    live_mark_fn: Optional[Callable[[Any, Trade], Any]] = None,
) -> dict[str, Any]:
    """Reconcile broker fills, update position marks, and persist an equity snapshot."""
    ledger = Ledger(session)
    active_ib = getattr(client, "active_trading_ib", None)
    if active_ib is None or not active_ib.isConnected():
        return {"reconciled": 0, "marks_updated": 0, "error": "IBKR not connected"}

    ib_trades: list[Any] = []
    fills: list[Any] = []
    positions: list[Any] = []
    positions_ok = False

    # Reconcile even on a flat ledger: fees and historical executions can arrive late.
    account = broker_account(client)
    try:
        ib_trades = active_ib.trades()
        fills = active_ib.fills() if hasattr(active_ib, "fills") else []
        try:
            if account and hasattr(active_ib, "reqExecutionsAsync"):
                from ib_async import ExecutionFilter

                fills = list(fills) + list(await active_ib.reqExecutionsAsync(
                    ExecutionFilter(acctCode=account)
                ))
        except Exception:
            logger.warning("Execution history unavailable; replaying cached executions", exc_info=True)
        reconciled = reconcile_broker_activity(ledger, ib_trades, fills, account=account, mode=getattr(runtime,"mode",None))
    except Exception:
        logger.exception("Execution reconciliation failed; retaining pending orders")
        session.rollback()
        reconciled = 0

    try:
        positions = active_ib.positions()
        positions_ok = True
    except Exception as e:
        logger.debug("Reading account positions failed: %s", e)

    # 2a. Ask IB about entry orders the placer stopped managing 15+ minutes ago
    if positions_ok and account:
        try:
            await check_unresolved_entries(ledger, active_ib, account, fills, positions)
        except Exception:
            logger.warning("Active entry-order check failed", exc_info=True)
            session.rollback()
    try:
        warn_missing_fees(session, datetime.now(UTC))
    except Exception:
        logger.warning("Commission check failed", exc_info=True)

    # 2b. Cancel orphaned WORKING trades stuck without broker_order_id
    if positions_ok:
        try:
            reconcile_stale_working_trades(
                ledger,
                ib_trades=ib_trades,
                positions=positions,
                stale_after_seconds=600.0,
                mode=getattr(runtime, "mode", None),
            )
        except Exception as e:
            logger.debug("reconcile_stale_working_trades failed: %s", e)

    # 3. Calculate marks from broker portfolio
    open_trades = [t for t in ledger.open_trades() if t.status is TradeStatus.OPEN]
    combined_portfolio = [
        item for item in active_ib.portfolio()
        if not account or getattr(item, "account", account) == account
    ]

    marks: dict[int, float] = {}
    try:
        marks = calculate_marks_from_portfolio(open_trades, combined_portfolio)
    except Exception as e:
        logger.debug("calculate_marks_from_portfolio failed: %s", e)

    # Apply portfolio marks
    apply_marks(ledger, marks)

    # 4. Fallback for trades not in portfolio (if live_mark_fn provided)
    if live_mark_fn is not None:
        for trade in open_trades:
            if trade.id not in marks and trade.status in (
                TradeStatus.OPEN,
                TradeStatus.WORKING,
            ):
                try:
                    mark = await live_mark_fn(client, trade)
                    ledger.update_mark(trade, mark.cost_to_close)
                    marks[trade.id] = mark.cost_to_close
                except Exception as e:
                    logger.debug(
                        "live_mark fallback failed for Trade #%s: %s", trade.id, e
                    )

    # 5. Persist equity snapshot so frontend equity curve is up to date
    mode = getattr(runtime,"mode",None)
    trades = ledger.all_trades()
    if isinstance(mode,TradingMode):
        modes = account_modes(mode)
        trades = [trade for trade in trades if trade.mode in modes]
    summary = summarize(trades)
    if runtime is not None and not summary.unverified_count:
        import asyncio
        from sqlalchemy import select
        from ..portfolio.benchmark import latest_sp500_close

        sp_close: float | None = None
        try:
            sp_close = await asyncio.to_thread(latest_sp500_close)
        except Exception as e:
            logger.debug(
                "Failed to fetch latest S&P close in sync_positions_and_marks: %s", e
            )

        if sp_close is None:
            last_snap = session.scalars(
                select(EquitySnapshot)
                .where(EquitySnapshot.sp500_close.is_not(None))
                .order_by(EquitySnapshot.ts.desc())
                .limit(1)
            ).first()
            if last_snap is not None:
                sp_close = last_snap.sp500_close

        base_cap = getattr(runtime, "starting_capital", 10000.0)
        session.add(
            EquitySnapshot(
                net_liq=base_cap + summary.realized_pnl + summary.unrealized_pnl,
                realized_pnl_cum=summary.realized_pnl,
                unrealized_pnl=summary.unrealized_pnl,
                sp500_close=sp_close,
            )
        )
        session.commit()

    logger.info(
        "Position sync complete: %d marks updated, unrealized P/L: $%.2f, realized P/L: $%.2f",
        len(marks),
        summary.unrealized_pnl,
        summary.realized_pnl,
    )
    return {
        "reconciled": reconciled,
        "marks_updated": len(marks),
        "unrealized_pnl": summary.unrealized_pnl,
        "realized_pnl": summary.realized_pnl,
    }
