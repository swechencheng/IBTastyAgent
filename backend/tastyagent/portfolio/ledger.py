"""The trade ledger: record decisions/trades and drive their status transitions.

This is the agent's own source of truth (the sandbox wipes state every 24h).

Money convention: a premium-selling trade's ``max_profit`` is the credit received
per contract (dollars), so the total entry credit = ``max_profit * contracts`` and
``buying_power = buying_power_reduction * contracts``. Realized P/L on close =
entry credit kept minus the debit paid to close.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import StrategyParams, TradingMode
from ..db.models import Decision, Trade, TradeEvent, TradeLeg, TradeStatus, utcnow
from ..models import CandidateTrade
from ..strategy.sizing import entry_bp_per_contract


class Ledger:
    def __init__(self, session: Session) -> None:
        self.s = session

    # --- lifecycle events (drive the live timeline + push notifications) ---
    def record_event(self, trade: Trade, kind: str, detail: str = "") -> TradeEvent:
        ev = TradeEvent(trade_id=trade.id, kind=kind, detail=detail)
        self.s.add(ev)
        self.s.commit()
        return ev

    # --- decisions ---
    def record_decision(
        self, mode: TradingMode | str, commentary: str, considered: int
    ) -> Decision:
        mode_val = mode.value if hasattr(mode, "value") else str(mode)
        d = Decision(mode=mode_val, commentary=commentary, considered=considered)
        self.s.add(d)
        self.s.commit()
        return d

    # --- trade creation ---
    def _new_trade(
        self,
        candidate: CandidateTrade,
        contracts: int,
        rationale: str,
        mode: TradingMode | str,
        status: TradeStatus,
        decision: Decision | None,
        buying_power: float | None = None,
    ) -> Trade:
        mode_val = mode.value if hasattr(mode, "value") else str(mode)
        trade = Trade(
            decision_id=decision.id if decision else None,
            symbol=candidate.symbol,
            strategy=candidate.strategy.value,
            contracts=contracts,
            status=status,
            mode=mode_val,
            rationale=rationale,
            entry_credit=candidate.max_profit * contracts,
            buying_power=(
                buying_power if buying_power is not None
                else entry_bp_per_contract(candidate, StrategyParams()) * contracts
            ),
            dte_at_entry=candidate.dte,
            current_cost_to_close=candidate.max_profit
            * contracts,  # 0% profit at entry
        )
        trade.legs = [
            TradeLeg(
                option_type=leg.option_type.value,
                strike=leg.strike,
                expiration=leg.expiration,
                action=leg.action.value,
                quantity=leg.quantity * contracts,
                delta=leg.delta,
            )
            for leg in candidate.legs
        ]
        self.s.add(trade)
        self.s.commit()
        if status is TradeStatus.REJECTED:
            self.record_event(trade, "rejected", rationale)
        elif status is TradeStatus.PENDING_APPROVAL:
            self.record_event(trade, "planned", "Awaiting your approval")
        else:
            self.record_event(trade, "planned", "Order created")
        return trade

    def record_planned(
        self, candidate, contracts, rationale, mode, decision=None, *, buying_power=None
    ) -> Trade:
        """Record a trade awaiting action. Initial status depends on the mode."""
        status = {
            TradingMode.LIVE_APPROVAL: TradeStatus.PENDING_APPROVAL,
            TradingMode.BACKTEST: TradeStatus.PLANNED,
        }.get(mode, TradeStatus.WORKING)
        return self._new_trade(candidate, contracts, rationale, mode, status, decision, buying_power)

    def record_rejected(self, candidate, reason, mode, decision=None) -> Trade:
        t = self._new_trade(candidate, 0, reason, mode, TradeStatus.REJECTED, decision)
        return t

    # --- status transitions ---
    def mark_working(self, trade: Trade, broker_order_id: str) -> None:
        if trade.status is TradeStatus.OPEN:
            return
        trade.status = TradeStatus.WORKING
        trade.broker_order_id = broker_order_id
        self.s.commit()
        self.record_event(
            trade,
            "working",
            f"Order working — submitted to broker (#{broker_order_id})",
        )

    def mark_open(self, trade: Trade, opened_at: datetime | None = None) -> None:
        if trade.status is TradeStatus.OPEN:
            return
        trade.status = TradeStatus.OPEN
        trade.opened_at = opened_at or utcnow()
        if trade.entry_date is None and trade.opened_at is not None:
            trade.entry_date = trade.opened_at.date()
        self.s.commit()
        self.record_event(trade, "open", "Filled — position open")

    def update_mark(self, trade: Trade, current_cost_to_close: float) -> None:
        trade.current_cost_to_close = current_cost_to_close
        self.s.commit()

    def close_trade(
        self,
        trade: Trade,
        exit_debit: float,
        exit_reason: str,
        closed_at: datetime | None = None,
        kind: str = "closed",
    ) -> None:
        trade.exit_debit = exit_debit
        trade.exit_reason = exit_reason
        trade.realized_pnl = trade.entry_credit - exit_debit
        trade.status = TradeStatus.CLOSED
        trade.closed_at = closed_at or utcnow()
        self.s.commit()
        verb = "Rolled" if kind == "rolled" else "Closed"
        self.record_event(
            trade, kind, f"{verb} — {exit_reason} (realized {trade.realized_pnl:+,.2f})"
        )

    def cancel_trade(self, trade: Trade, reason: str = "canceled") -> None:
        trade.status = TradeStatus.CANCELED
        trade.exit_reason = reason
        self.s.commit()
        self.record_event(trade, "canceled", reason)

    def record_roll(
        self, old_trade, new_candidate, contracts, exit_debit, reason, new_order_id
    ) -> Trade:
        """Close the old cycle (booking realized P/L) and open the rolled position.

        Models a roll as close-old + open-new; the new trade is linked back via its
        rationale and starts WORKING with the broker order id.
        """
        self.close_trade(
            old_trade, exit_debit=exit_debit, exit_reason=reason, kind="rolled"
        )
        new = self._new_trade(
            new_candidate,
            contracts,
            f"rolled from #{old_trade.id}: {reason}",
            TradingMode(old_trade.mode),
            TradeStatus.WORKING,
            None,
        )
        new.broker_order_id = new_order_id
        self.s.commit()
        self.record_event(
            new,
            "working",
            f"Rolled from #{old_trade.id} — order working (#{new_order_id})",
        )
        return new

    # --- queries ---
    def get(self, trade_id: int) -> Trade | None:
        return self.s.get(Trade, trade_id)

    def _by_status(self, *statuses: TradeStatus) -> list[Trade]:
        stmt = (
            select(Trade).where(Trade.status.in_(statuses)).order_by(Trade.created_at)
        )
        return list(self.s.scalars(stmt))

    def open_trades(self) -> list[Trade]:
        return self._by_status(TradeStatus.OPEN, TradeStatus.WORKING)

    def closed_trades(self) -> list[Trade]:
        return self._by_status(TradeStatus.CLOSED)

    def pending_approval(self) -> list[Trade]:
        return self._by_status(TradeStatus.PENDING_APPROVAL)

    def all_trades(self) -> list[Trade]:
        return list(self.s.scalars(select(Trade).order_by(Trade.created_at)))

    def positions_by_symbol(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in self.open_trades():
            counts[t.symbol] = counts.get(t.symbol, 0) + 1
        return counts
