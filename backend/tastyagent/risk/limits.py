"""Portfolio-level risk checks applied after strategy selection / sizing.

These are the last gate before an order is placed and run on the LLM's final
choices too. Any failure blocks the new entry (existing positions are managed
by ``exits.py`` independently).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import RiskLimits


@dataclass(frozen=True)
class PortfolioState:
    net_liq: float
    bp_used: float  # current aggregate buying power in use (dollars)
    open_positions: int
    positions_for_symbol: int  # open positions in the candidate's symbol
    realized_pnl_today: float  # signed dollars realized so far today
    consecutive_losses: int  # losing trades closed in a row
    cushion: float | None = None  # IBKR margin cushion ratio (Excess Liquidity / Net Liq)
    is_live: bool = False  # whether currently executing in live trading mode


@dataclass(frozen=True)
class RiskResult:
    ok: bool
    violations: tuple[str, ...]


def check_new_entry(
    symbol: str,
    incremental_bp: float,
    state: PortfolioState,
    limits: RiskLimits,
) -> RiskResult:
    """Decide whether a new entry consuming ``incremental_bp`` is permitted."""
    v: list[str] = []

    if limits.kill_switch:
        v.append("kill switch engaged")

    if state.is_live and state.cushion is not None and state.cushion < limits.min_cushion_pct:
        v.append(
            f"IBKR account cushion {state.cushion:.1%} < min {limits.min_cushion_pct:.0%}"
        )

    if state.open_positions >= limits.max_positions:
        v.append(f"open positions {state.open_positions} >= max {limits.max_positions}")

    if state.positions_for_symbol >= limits.max_positions_per_symbol:
        v.append(
            f"{symbol} positions {state.positions_for_symbol} >= max "
            f"{limits.max_positions_per_symbol}"
        )

    if state.net_liq > 0:
        projected_bp_pct = (state.bp_used + incremental_bp) / state.net_liq
        if projected_bp_pct > limits.max_total_bp_pct:
            v.append(
                f"projected BP {projected_bp_pct:.0%} > max {limits.max_total_bp_pct:.0%}"
            )

        daily_loss_pct = -state.realized_pnl_today / state.net_liq
        if daily_loss_pct >= limits.max_daily_loss_pct:
            v.append(
                f"daily loss {daily_loss_pct:.1%} >= max {limits.max_daily_loss_pct:.1%}"
            )

    if state.consecutive_losses >= limits.consecutive_loss_halt:
        v.append(
            f"{state.consecutive_losses} consecutive losses >= halt "
            f"{limits.consecutive_loss_halt}"
        )

    return RiskResult(ok=not v, violations=tuple(v))
