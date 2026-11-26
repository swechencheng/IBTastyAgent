"""Position sizing: buying-power based, respecting per-trade and portfolio caps."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import select
from sqlalchemy.orm import object_session

from ..config import RiskLimits, StrategyParams
from ..db.models import ExecutionOrder, Trade, TradeStatus
from ..models import CandidateTrade
from .guardrails import entry_width_ratio


def minimum_entry_credit(
    mid_per_share: float,
    strike_width: float | None,
    width_ratio: float,
    additional_min_credit: float = 0.0,
) -> float:
    """Lowest executable per-share credit, rounded upward to the next penny."""
    floor = max(
        Decimal("0.05"),
        Decimal(str(mid_per_share)) * Decimal("0.90"),
        Decimal(str(additional_min_credit)),
    )
    if strike_width is not None:
        floor = max(floor, Decimal(str(strike_width)) * Decimal(str(width_ratio)))
    return float(floor.quantize(Decimal("0.01"), rounding=ROUND_CEILING))


def entry_bp_per_contract(candidate: CandidateTrade, params: StrategyParams) -> float:
    width = candidate.strike_width
    if width is None:
        return candidate.buying_power_reduction
    ratio = entry_width_ratio(
        candidate.symbol,
        candidate.strategy.value,
        params.min_credit_width_ratio,
        params.index_call_min_credit_width_ratio,
    )
    floor = minimum_entry_credit(candidate.max_profit / 100, width, ratio)
    return max(0.0, round((width - floor) * 100, 2))


def effective_buying_power(trade: Trade, params: StrategyParams | None = None) -> float:
    """Revalue recognized defined-risk positions and reserve unfilled entry exposure."""
    width = trade.strike_width
    if trade.status == TradeStatus.OPEN:
        used = trade.buying_power or 0.0
        if width is not None:
            used = max(
                0.0, round(width * 100 * trade.contracts - trade.entry_credit, 2)
            )
        # A partially executed entry is OPEN for the units we own. The remaining
        # units can still fill until the broker confirms that child order is terminal.
        session = object_session(trade)
        if session is not None and trade.order_ref:
            intent = session.scalar(
                select(ExecutionOrder).where(
                    ExecutionOrder.trade_id == trade.id,
                    ExecutionOrder.kind == "entry",
                    ExecutionOrder.order_ref == trade.order_ref,
                    ExecutionOrder.terminal.is_(False),
                )
            )
            if intent is not None and intent.applied < intent.requested:
                used += (
                    intent.spec["bp"]
                    * (intent.requested - intent.applied)
                    / intent.requested
                )
        return round(used, 2)
    if width is None or not trade.contracts:
        return trade.buying_power or 0.0
    params = params or StrategyParams()
    ratio = entry_width_ratio(
        trade.symbol,
        trade.strategy,
        params.min_credit_width_ratio,
        params.index_call_min_credit_width_ratio,
    )
    minimum = minimum_entry_credit(
        trade.entry_credit / (100 * trade.contracts), width, ratio
    )
    return max(
        trade.buying_power or 0.0, round((width - minimum) * 100 * trade.contracts, 2)
    )


@dataclass(frozen=True)
class SizingResult:
    contracts: int  # 0 means the trade cannot be placed within limits
    bp_used: float  # dollars of buying power the sized trade consumes
    reason: str


def size_trade(
    candidate: CandidateTrade,
    net_liq: float,
    current_bp_used: float,
    limits: RiskLimits,
    *,
    current_strategy_bp_used: float | None = None,
    params: StrategyParams | None = None,
) -> SizingResult:
    """Determine how many contracts to trade within BP caps.

    - per-trade BP must not exceed ``max_trade_bp_pct`` of net liquidation value
    - aggregate BP (existing + new) must not exceed ``max_total_bp_pct``
    - strategy-specific BP must not exceed ``max_spread_bp_pct`` or ``max_undefined_bp_pct``
    """
    per_contract_bp = entry_bp_per_contract(candidate, params or StrategyParams())
    if per_contract_bp <= 0 or net_liq <= 0:
        return SizingResult(0, 0.0, "invalid BP or net liq")

    per_trade_budget = limits.max_trade_bp_pct * net_liq
    total_budget_remaining = limits.max_total_bp_pct * net_liq - current_bp_used
    budget = min(per_trade_budget, total_budget_remaining)

    if current_strategy_bp_used is not None:
        strategy_cap_pct = (
            limits.max_spread_bp_pct
            if candidate.strategy.is_defined_risk
            else limits.max_undefined_bp_pct
        )
        strategy_budget_remaining = (
            strategy_cap_pct * net_liq - current_strategy_bp_used
        )
        budget = min(budget, strategy_budget_remaining)

    if budget < per_contract_bp:
        return SizingResult(
            0,
            0.0,
            f"need ${per_contract_bp:,.0f}/contract but only ${budget:,.0f} available",
        )

    contracts = int(math.floor(budget / per_contract_bp))
    bp_used = contracts * per_contract_bp
    return SizingResult(
        contracts, bp_used, f"{contracts} contract(s), ${bp_used:,.0f} BP"
    )
