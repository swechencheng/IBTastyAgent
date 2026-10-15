"""Fill tracking and exit application — reconcile our ledger with broker state.

Kept broker-agnostic: callers pass plain dicts (order-id -> status, trade-id -> mark)
gathered from the broker, so this is deterministic and unit-testable. The exit
decisions themselves come from ``strategy/exits.py``; this module just applies them
to the ledger.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from ..db.models import EquitySnapshot, Trade, TradeStatus
from ..portfolio.ledger import Ledger
from ..portfolio.pnl import summarize

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
    """Flip WORKING trades to OPEN (filled) or CANCELED (dead) per broker status."""
    for trade in ledger.open_trades():
        if trade.status is not TradeStatus.WORKING or not trade.broker_order_id:
            continue
        status = order_status.get(trade.broker_order_id)
        if status in FILLED:
            ledger.mark_open(trade)
        elif status in DEAD:
            ledger.cancel_trade(trade, reason=f"broker: {status}")


def reconcile_closing_orders(
    ledger: Ledger,
    ib_trades: list[Any],
    active_ib: Any = None,
) -> int:
    """Close OPEN trades in ledger if their closing orders have filled on the broker."""
    closed_count = 0
    trades_by_ref: dict[str, Any] = {}
    trades_by_id: dict[str, Any] = {}
    for t in ib_trades:
        order = getattr(t, "order", None)
        if order is not None:
            ref = getattr(order, "orderRef", "")
            if ref:
                trades_by_ref[ref] = t
            oid = getattr(order, "orderId", None)
            if oid:
                trades_by_id[str(oid)] = t

    for trade in ledger.open_trades():
        if trade.status is not TradeStatus.OPEN:
            continue

        close_ref = f"TastyAgent_CLOSE_{trade.id}"
        tp_ref = f"TastyAgent_TP_{trade.id}"

        close_trade = trades_by_ref.get(close_ref)
        tp_trade = trades_by_ref.get(tp_ref) or (
            trades_by_id.get(str(trade.tp_order_id)) if trade.tp_order_id else None
        )

        filled_trade = None
        exit_reason = ""
        is_tp = False

        if (
            close_trade
            and getattr(getattr(close_trade, "orderStatus", None), "status", "")
            in FILLED
        ):
            filled_trade = close_trade
            exit_reason = "Closed via broker close order"
        elif (
            tp_trade
            and getattr(getattr(tp_trade, "orderStatus", None), "status", "") in FILLED
        ):
            filled_trade = tp_trade
            exit_reason = "Take-profit order filled"
            is_tp = True

        if filled_trade is not None:
            exit_debit = 0.0
            closed_at = None
            fills = getattr(filled_trade, "fills", [])
            bag_fill = next(
                (
                    f
                    for f in fills
                    if getattr(getattr(f, "contract", None), "secType", "") == "BAG"
                ),
                None,
            )
            if bag_fill is not None:
                p = getattr(getattr(bag_fill, "execution", None), "price", 0.0)
                exit_debit = abs(float(p)) * 100.0 * trade.contracts
                closed_at = getattr(getattr(bag_fill, "execution", None), "time", None)
            elif fills:
                leg_debit = 0.0
                for f in fills:
                    price = float(getattr(getattr(f, "execution", None), "price", 0.0))
                    side = (
                        str(getattr(getattr(f, "execution", None), "side", ""))
                        .strip()
                        .upper()
                    )
                    if side == "BOT":
                        leg_debit += price
                    else:
                        leg_debit -= price
                    if closed_at is None:
                        closed_at = getattr(
                            getattr(f, "execution", None), "time", None
                        )
                exit_debit = abs(leg_debit) * 100.0 * trade.contracts
            else:
                exit_debit = (
                    trade.take_profit_debit
                    if is_tp and trade.take_profit_debit is not None
                    else (trade.current_cost_to_close or trade.entry_credit)
                )

            # Cancel remaining TP order if this was a manual/defensive close
            if not is_tp and trade.tp_order_id and active_ib is not None:
                try:
                    for ot in active_ib.openTrades():
                        if str(ot.order.orderId) == str(
                            trade.tp_order_id
                        ) or getattr(ot.order, "orderRef", "") == tp_ref:
                            active_ib.cancelOrder(ot.order)
                            logger.info(
                                "Cancelled orphaned TP order #%s for closed Trade #%s",
                                trade.tp_order_id,
                                trade.id,
                            )
                except Exception as e:
                    logger.debug("Failed to cancel TP order: %s", e)

            ledger.close_trade(
                trade,
                exit_debit=exit_debit,
                exit_reason=exit_reason,
                closed_at=closed_at,
            )
            closed_count += 1
            logger.info(
                "Trade #%s (%s %s) reconciled as CLOSED (%s, debit: $%.2f)",
                trade.id,
                trade.symbol,
                trade.strategy,
                exit_reason,
                exit_debit,
            )

    return closed_count


def reconcile_positions_from_broker(ledger: Ledger, positions: list[Any]) -> int:
    """Transition WORKING trades to OPEN if all their option legs exist in broker positions.

    Useful when order fills happened across session restarts or when order-id lookups expire.
    """
    if not positions:
        return 0

    pos_keys = set()
    for p in positions:
        c = getattr(p, "contract", None)
        if c is not None:
            sym = getattr(c, "symbol", "").upper()
            right = getattr(c, "right", "").upper()
            strike = round(float(getattr(c, "strike", 0.0)), 2)
            exp = str(getattr(c, "lastTradeDateOrContractMonth", "") or "").replace(
                "-", ""
            )
            pos_keys.add((sym, right, strike, exp))

    transitioned = 0
    for trade in ledger.open_trades():
        if trade.status is not TradeStatus.WORKING or not trade.legs:
            continue
        all_legs_in_pos = True
        for leg in trade.legs:
            right = "P" if leg.option_type.lower() == "put" else "C"
            strike = round(float(leg.strike), 2)
            exp = str(leg.expiration).replace("-", "")
            key = (trade.symbol.upper(), right, strike, exp)
            if key not in pos_keys:
                all_legs_in_pos = False
                break
        if all_legs_in_pos:
            ledger.mark_open(trade)
            transitioned += 1
            logger.info(
                "Trade #%s (%s %s) verified in broker positions -> status transitioned to OPEN",
                trade.id,
                trade.symbol,
                trade.strategy,
            )

    return transitioned


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
                    cost_to_close += mkt_price * 100 * trade.contracts
                else:
                    cost_to_close -= mkt_price * 100 * trade.contracts
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
    open_trades = ledger.open_trades()
    if not open_trades:
        return {
            "reconciled": 0,
            "marks_updated": 0,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
        }

    active_ib = getattr(client, "active_trading_ib", None)
    if active_ib is None or not active_ib.isConnected():
        return {"reconciled": 0, "marks_updated": 0, "error": "IBKR not connected"}

    # 1. Reconcile fills from active trades & order statuses
    try:
        ib_trades = active_ib.trades()
        status_map = {str(t.order.orderId): t.orderStatus.status for t in ib_trades}
        reconcile_fills(ledger, status_map)
        reconcile_closing_orders(ledger, ib_trades, active_ib)
    except Exception as e:
        logger.debug("reconcile_fills / reconcile_closing_orders failed: %s", e)

    # 2. Reconcile working trades against current positions
    try:
        positions = active_ib.positions()
        reconcile_positions_from_broker(ledger, positions)
    except Exception as e:
        logger.debug("reconcile_positions_from_broker failed: %s", e)

    # 3. Calculate marks from broker portfolio
    combined_portfolio = list(active_ib.portfolio()) if active_ib else []
    if (
        client
        and hasattr(client, "data_ib")
        and client.data_ib
        and client.data_ib.isConnected()
    ):
        combined_portfolio.extend(client.data_ib.portfolio())

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
    summary = summarize(ledger.all_trades())
    if runtime is not None:
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
        "reconciled": len(open_trades),
        "marks_updated": len(marks),
        "unrealized_pnl": summary.unrealized_pnl,
        "realized_pnl": summary.realized_pnl,
    }
