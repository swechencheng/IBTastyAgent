"""Executor: turn a decision cycle's planned trades into orders, per trading mode.

Mode routing:
- SANDBOX / LIVE_AUTO : place immediately via the injected ``placer``.
- LIVE_APPROVAL       : record as PENDING_APPROVAL and wait — nothing is placed until
                        ``approve()`` is called (the dashboard's one-click gate).
- BACKTEST            : record as PLANNED only; never touches a broker.

The ``placer`` is an async callable ``(Trade) -> broker_order_id`` injected by the
caller (a sandbox/live adapter over ``tt/orders.py``, or a fake in tests). The
executor never imports the broker directly, so its routing logic is unit-testable.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from ..config import TradingMode
from ..db.models import Decision, Trade, TradeStatus
from ..decision.orchestrator import CycleResult
from ..portfolio.ledger import Ledger

Placer = Callable[[Trade], Awaitable[str]]


@dataclass
class ExecOutcome:
    trade_id: int
    symbol: str
    action: str  # placed | queued_for_approval | recorded | rejected | error
    detail: str = ""


class Executor:
    def __init__(
        self, ledger: Ledger, mode: TradingMode, placer: Placer | None = None
    ) -> None:
        self.ledger = ledger
        self.mode = mode
        self.placer = placer

    def _places_immediately(self) -> bool:
        return self.mode in (TradingMode.SANDBOX, TradingMode.LIVE_AUTO)

    async def _place(self, trade: Trade) -> ExecOutcome:
        if self.placer is None:
            return ExecOutcome(trade.id, trade.symbol, "error", "no placer configured")
        try:
            order_id = await self.placer(trade)
        except asyncio.CancelledError:
            self.ledger.cancel_trade(trade, reason="placement canceled during shutdown")
            raise
        except Exception as e:  # noqa: BLE001 - surface broker errors as outcomes
            self.ledger.cancel_trade(trade, reason=f"placement error: {e}")
            return ExecOutcome(trade.id, trade.symbol, "error", str(e))
        self.ledger.mark_working(trade, order_id)
        return ExecOutcome(trade.id, trade.symbol, "placed", f"order {order_id}")

    async def execute_cycle(
        self, result: CycleResult, decision: Decision
    ) -> list[ExecOutcome]:
        outcomes: list[ExecOutcome] = []

        # Persist rejected candidates for the audit trail / dashboard.
        for cand, reason in result.rejected:
            t = self.ledger.record_rejected(cand, reason, self.mode, decision)
            outcomes.append(ExecOutcome(t.id, t.symbol, "rejected", reason))

        for planned in result.planned:
            trade = self.ledger.record_planned(
                planned.candidate,
                planned.contracts,
                planned.rationale,
                self.mode,
                decision,
            )
            if self.mode is TradingMode.LIVE_APPROVAL:
                outcomes.append(
                    ExecOutcome(trade.id, trade.symbol, "queued_for_approval")
                )
            elif self._places_immediately():
                outcomes.append(await self._place(trade))
            else:  # BACKTEST
                outcomes.append(
                    ExecOutcome(trade.id, trade.symbol, "recorded", "planned")
                )
        return outcomes

    # --- live approval queue ---
    async def approve(self, trade_id: int) -> ExecOutcome:
        trade = self.ledger.get(trade_id)
        if trade is None:
            return ExecOutcome(trade_id, "?", "error", "trade not found")
        if trade.status is not TradeStatus.PENDING_APPROVAL:
            return ExecOutcome(
                trade_id, trade.symbol, "error", f"not pending ({trade.status.value})"
            )
        return await self._place(trade)

    def reject(self, trade_id: int, reason: str = "approval denied") -> ExecOutcome:
        trade = self.ledger.get(trade_id)
        if trade is None:
            return ExecOutcome(trade_id, "?", "error", "trade not found")
        self.ledger.cancel_trade(trade, reason=reason)
        return ExecOutcome(trade_id, trade.symbol, "recorded", "canceled")
