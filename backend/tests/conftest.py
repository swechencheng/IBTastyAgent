"""Shared test builders for the deterministic core."""

from __future__ import annotations

from datetime import date, timedelta

from tastyagent.models import (
    Action,
    CandidateTrade,
    Leg,
    Liquidity,
    OpenPosition,
    OptionType,
    Strategy,
)


def make_leg(
    option_type: OptionType = OptionType.PUT,
    strike: float = 100.0,
    action: Action = Action.SELL_TO_OPEN,
    delta: float = -0.16,
    dte: int = 45,
) -> Leg:
    return Leg(
        option_type=option_type,
        strike=strike,
        expiration=date(2026, 1, 1) + timedelta(days=dte),
        action=action,
        delta=delta,
    )


def make_candidate(**overrides) -> CandidateTrade:
    """A healthy, guardrail-passing strangle candidate; override fields per test."""
    defaults = dict(
        symbol="SPY",
        strategy=Strategy.SHORT_STRANGLE,
        legs=(
            make_leg(OptionType.PUT, 90.0, Action.SELL_TO_OPEN, -0.16),
            make_leg(OptionType.CALL, 110.0, Action.SELL_TO_OPEN, 0.16),
        ),
        dte=45,
        net_credit=2.50,
        max_profit=250.0,
        max_loss=float("inf"),
        buying_power_reduction=2000.0,
        underlying_price=100.0,
        iv_rank=0.45,
        liquidity=Liquidity(
            bid_ask_width_pct=0.04, open_interest=5000, daily_volume=2000
        ),
        earnings_in_days=None,
    )
    defaults.update(overrides)
    return CandidateTrade(**defaults)


def make_iron_condor() -> CandidateTrade:
    return make_candidate(
        strategy=Strategy.IRON_CONDOR,
        legs=(
            make_leg(OptionType.PUT, 90, Action.SELL_TO_OPEN, -0.30),
            make_leg(OptionType.PUT, 85, Action.BUY_TO_OPEN, -0.10),
            make_leg(OptionType.CALL, 110, Action.SELL_TO_OPEN, 0.30),
            make_leg(OptionType.CALL, 115, Action.BUY_TO_OPEN, 0.10),
        ),
        net_credit=150.0,
        max_profit=150.0,
        max_loss=350.0,
        buying_power_reduction=350.0,
    )


def make_position(
    *,
    strategy: Strategy = Strategy.SHORT_STRANGLE,
    entry_credit: float = 250.0,
    current_cost_to_close: float = 125.0,
    days_held: int = 10,
    dte_remaining: int = 35,
) -> OpenPosition:
    entry = date(2026, 2, 1)
    return OpenPosition(
        symbol="SPY",
        strategy=strategy,
        legs=(make_leg(),),
        entry_date=entry,
        entry_credit=entry_credit,
        current_cost_to_close=current_cost_to_close,
        buying_power_reduction=2000.0,
        dte_remaining=dte_remaining,
        as_of=entry + timedelta(days=days_held),
    )
