"""P/L aggregation and win/loss classification over ledger trades.

Pure functions over any objects exposing the Trade attributes used here
(``status``, ``entry_credit``, ``current_cost_to_close``, ``realized_pnl``), so
they're trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..db.models import Trade, TradeStatus


@dataclass(frozen=True)
class PnLSummary:
    realized_pnl: float
    unrealized_pnl: float
    open_count: int
    closed_count: int
    wins: int
    losses: int

    unverified_count: int = 0

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def win_rate(self) -> float | None:
        decided = self.wins + self.losses
        return (self.wins / decided) if decided else None

    def profit_pct(self, starting_capital: float) -> float | None:
        if starting_capital <= 0:
            return None
        return self.total_pnl / starting_capital


def summarize(trades: Iterable[Trade]) -> PnLSummary:
    realized = unrealized = 0.0
    open_count = closed_count = wins = losses = unverified_count = 0
    for t in trades:
        if getattr(t, "execution_verified", None) is False:
            unverified_count += 1
            continue
        if getattr(t, "basis_transferred", False):
            continue
        if t.status is TradeStatus.CLOSED and t.realized_pnl is not None:
            realized += t.realized_pnl
            closed_count += 1
            if t.realized_pnl > 0:
                wins += 1
            elif t.realized_pnl < 0:
                losses += 1
        elif t.status in (TradeStatus.OPEN, TradeStatus.WORKING):
            unrealized += t.entry_credit - t.current_cost_to_close - (getattr(t, "entry_commission", 0) or 0)
            open_count += 1
    return PnLSummary(
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        open_count=open_count,
        closed_count=closed_count,
        wins=wins,
        losses=losses,
        unverified_count=unverified_count,
    )
