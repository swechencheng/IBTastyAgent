"""Exit & defense management — close winners, defend/roll losers (tastytrade-style).

Each tick, for every OPEN position we mark it (cost-to-close + current short-leg delta),
ask ``strategy/exits.py`` what to do, and act:
  - CLOSE -> place a closing order, book realized P/L
  - ROLL  -> roll out (21 DTE) or roll the untested side; falls back to CLOSE if no
             credit roll is available

Pure/testable: the live mark and the close/roll broker actions are injected as
``mark_fn`` / ``close_fn`` / ``roll_fn``, so this runs with fakes in unit tests and the
broker in production.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

from ..config import StrategyParams
from ..db.models import Trade, TradeStatus
from ..models import CandidateTrade, OptionType, Strategy, OpenPosition
from ..portfolio.ledger import Ledger
from ..strategy.exits import ExitAction, RollKind, evaluate_exit


@dataclass
class PositionMark:
    cost_to_close: float  # total dollars to buy the position back now
    max_short_delta: float | None = None  # |delta| of the most-tested short leg
    tested_side: OptionType | None = (
        None  # which short leg is tested (for roll-untested)
    )


@dataclass
class RollResult:
    new_candidate: CandidateTrade
    contracts: int
    exit_debit: float  # paid to close the old cycle
    new_order_id: str


MarkFn = Callable[[Trade], Awaitable[PositionMark]]
CloseFn = Callable[[Trade, float], Awaitable[str]]  # (trade, debit_total) -> order id
RollFn = Callable[[Trade, RollKind, PositionMark], Awaitable["RollResult | None"]]


@dataclass
class ExitOutcome:
    trade_id: int
    symbol: str
    action: str  # hold | closed | rolled | error
    reason: str
    detail: str = ""


def trade_to_position(trade: Trade, mark: PositionMark, today: date) -> OpenPosition:
    exp = min((leg.expiration for leg in trade.legs), default=today)
    entry = trade.entry_date or (trade.opened_at.date() if trade.opened_at else today)
    return OpenPosition(
        symbol=trade.symbol,
        strategy=Strategy(trade.strategy),
        legs=(),
        entry_date=entry,
        entry_credit=trade.entry_credit,
        current_cost_to_close=mark.cost_to_close,
        buying_power_reduction=trade.buying_power,
        dte_remaining=(exp - today).days,
        as_of=today,
        current_max_short_delta=mark.max_short_delta,
    )


async def manage_exits(
    ledger: Ledger,
    params: StrategyParams,
    *,
    mark_fn: MarkFn,
    close_fn: CloseFn,
    roll_fn: RollFn | None = None,
    today: date | None = None,
) -> list[ExitOutcome]:
    today = today or date.today()
    outcomes: list[ExitOutcome] = []

    for trade in ledger.open_trades():
        if trade.status is not TradeStatus.OPEN:  # only manage filled positions
            continue
        try:
            mark = await mark_fn(trade)
        except Exception as e:  # noqa: BLE001 - skip a position we can't mark this tick
            outcomes.append(
                ExitOutcome(trade.id, trade.symbol, "error", "mark failed", str(e))
            )
            continue

        ledger.update_mark(trade, mark.cost_to_close)
        decision = evaluate_exit(trade_to_position(trade, mark, today), params)

        if not decision.should_act:
            outcomes.append(
                ExitOutcome(trade.id, trade.symbol, "hold", decision.reason)
            )
            continue

        if decision.action is ExitAction.ROLL:
            if roll_fn is not None:
                try:
                    roll = await roll_fn(trade, decision.roll_kind, mark)
                except Exception as e:  # noqa: BLE001
                    outcomes.append(
                        ExitOutcome(
                            trade.id, trade.symbol, "error", decision.reason, str(e)
                        )
                    )
                    continue
                if roll is not None:
                    new = ledger.record_roll(
                        trade,
                        roll.new_candidate,
                        roll.contracts,
                        roll.exit_debit,
                        decision.reason,
                        roll.new_order_id,
                    )
                    outcomes.append(
                        ExitOutcome(
                            trade.id,
                            trade.symbol,
                            "rolled",
                            decision.reason,
                            f"-> #{new.id}",
                        )
                    )
                    continue

            # If rolling untested side (tested delta threshold reached) and no credit roll is available,
            # do NOT fall through to close! Keep holding the position.
            if decision.roll_kind is RollKind.UNTESTED:
                outcomes.append(
                    ExitOutcome(
                        trade.id,
                        trade.symbol,
                        "hold",
                        f"{decision.reason} (no credit roll; holding)",
                    )
                )
                continue

            # No credit roll available -> fall through to a plain close (e.g. 21-DTE roll).
            decision_reason = decision.reason + " (no credit roll; closed)"
        else:
            decision_reason = decision.reason

        # CLOSE (either an explicit close, or a roll with no credit available).
        try:
            order_id = await close_fn(trade, mark.cost_to_close)
        except Exception as e:  # noqa: BLE001
            outcomes.append(
                ExitOutcome(trade.id, trade.symbol, "error", decision_reason, str(e))
            )
            continue
        ledger.close_trade(
            trade, exit_debit=mark.cost_to_close, exit_reason=decision_reason
        )
        outcomes.append(
            ExitOutcome(
                trade.id, trade.symbol, "closed", decision_reason, f"order {order_id}"
            )
        )

    return outcomes


async def audit_take_profit_orders(
    ledger: Ledger,
    active_broker_order_ids: set[str],
    auto_attach_fn: Callable[[Trade], Awaitable[str]] | None = None,
) -> list[str]:
    """Audit open IBTastyAgent positions to ensure each has an active Take-Profit order on IBKR.

    If an open trade has no active Take-Profit order:
      - Emits an alert warning the user.
      - Optionally calls auto_attach_fn to automatically submit the missing 50% TP order.
    """
    alerts: list[str] = []

    for trade in ledger.open_trades():
        if trade.status is not TradeStatus.OPEN:
            continue

        has_active_tp = (
            trade.tp_order_id and str(trade.tp_order_id) in active_broker_order_ids
        )

        if not has_active_tp:
            msg = (
                f"⚠️ Position Alert: Trade #{trade.id} ({trade.symbol} {trade.strategy}, "
                f"{trade.contracts}x) is OPEN in IBTastyAgent but has NO active Take-Profit order on IBKR!"
            )
            logger.warning(msg)
            alerts.append(msg)

            if auto_attach_fn is not None:
                try:
                    logger.info(
                        "Auto-attaching missing Take-Profit order for Trade #%s...",
                        trade.id,
                    )
                    new_tp_id = await auto_attach_fn(trade)
                    trade.tp_order_id = str(new_tp_id)
                    ledger.s.commit()
                    logger.info(
                        "Successfully attached missing Take-Profit order #%s for Trade #%s",
                        new_tp_id,
                        trade.id,
                    )
                except Exception as e:
                    logger.error(
                        "Failed to auto-attach Take-Profit order for Trade #%s: %s",
                        trade.id,
                        e,
                    )

    return alerts
