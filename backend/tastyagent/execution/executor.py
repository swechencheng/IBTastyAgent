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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select, update

from ..config import TradingMode
from ..db.models import Decision, ExecutionOrder, Trade, TradeStatus
from ..decision.orchestrator import CycleResult
from ..portfolio.ledger import Ledger

Placer = Callable[[Trade], Awaitable[str]]


class EntryUnfilledError(RuntimeError):
    """The broker positively confirmed that the last entry order ended unfilled."""


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

    def _update_entry(
        self, trade: Trade, statuses: tuple[TradeStatus, ...], **values
    ) -> bool:
        result = self.ledger.s.execute(
            update(Trade)
            .where(Trade.id == trade.id, Trade.status.in_(statuses))
            .values(**values)
        )
        self.ledger.s.commit()
        self.ledger.s.refresh(trade)
        return result.rowcount == 1

    def _placement_error(
        self, trade: Trade, reason: str, *, unfilled: bool = False
    ) -> None:
        if trade.status not in (TradeStatus.WORKING, TradeStatus.PENDING_APPROVAL):
            return
        pending = self.ledger.s.scalar(
            select(ExecutionOrder.id).where(
                ExecutionOrder.trade_id == trade.id,
                ExecutionOrder.kind == "entry",
                ExecutionOrder.terminal.is_(False),
            ).limit(1)
        )
        if pending or ((trade.order_ref or trade.broker_order_id) and not unfilled):
            self.ledger.record_event(
                trade, "working", f"{reason}; awaiting broker reconciliation"
            )
        elif self._update_entry(
            trade,
            (TradeStatus.WORKING, TradeStatus.PENDING_APPROVAL),
            status=TradeStatus.CANCELED,
            exit_reason=reason,
        ):
            self.ledger.record_event(trade, "canceled", reason)

    async def _place(self, trade: Trade) -> ExecOutcome:
        if self.placer is None:
            return ExecOutcome(trade.id, trade.symbol, "error", "no placer configured")
        try:
            order_id = await self.placer(trade)
        except asyncio.CancelledError:
            self._placement_error(trade, "placement canceled during shutdown")
            raise
        except Exception as e:  # noqa: BLE001 - surface broker errors as outcomes
            self._placement_error(
                trade,
                f"placement error: {e}",
                unfilled=isinstance(e, EntryUnfilledError),
            )
            return ExecOutcome(trade.id, trade.symbol, "error", str(e))
        if self._update_entry(
            trade,
            (TradeStatus.WORKING, TradeStatus.PENDING_APPROVAL),
            status=TradeStatus.WORKING,
            broker_order_id=order_id,
        ):
            self.ledger.record_event(
                trade, "working", f"Order working — submitted to broker (#{order_id})"
            )
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
                buying_power=planned.buying_power,
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
        if self.placer is None:
            return ExecOutcome(trade.id, trade.symbol, "error", "no placer configured")
        if not self._update_entry(
            trade, (TradeStatus.PENDING_APPROVAL,), status=TradeStatus.WORKING
        ):
            return ExecOutcome(
                trade.id, trade.symbol, "error", "approval already claimed"
            )
        return await self._place(trade)

    def reject(self, trade_id: int, reason: str = "approval denied") -> ExecOutcome:
        trade = self.ledger.get(trade_id)
        if trade is None:
            return ExecOutcome(trade_id, "?", "error", "trade not found")
        if not self._update_entry(
            trade,
            (TradeStatus.PENDING_APPROVAL,),
            status=TradeStatus.CANCELED,
            exit_reason=reason,
        ):
            return ExecOutcome(
                trade.id, trade.symbol, "error", "approval already claimed"
            )
        self.ledger.record_event(trade, "canceled", reason)
        return ExecOutcome(trade_id, trade.symbol, "recorded", "canceled")
