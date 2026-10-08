"""Core domain models shared across the strategy, risk, and execution layers.

Stdlib-only so the safety-critical core stays dependency-free and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


def probability_of_profit(short_deltas: list[float]) -> float:
    """Model-free PoP estimate from short-leg deltas.

    Delta approximates an option's probability of finishing in-the-money, so the
    probability that all short legs expire OTM (the trade is profitable) is
    ~ 1 - sum(|short deltas|). This is the heuristic behind "16-delta strangle ≈
    ~68% PoP". It ignores the small breakeven cushion from the credit, so it's a
    slightly conservative estimate. Clamped to [0, 1].
    """
    pop = 1.0 - sum(abs(d) for d in short_deltas)
    return max(0.0, min(1.0, pop))


class Strategy(str, Enum):
    SHORT_STRANGLE = "short_strangle"
    NAKED_PUT = "naked_put"
    NAKED_CALL = "naked_call"
    SHORT_STRADDLE = "short_straddle"
    IRON_CONDOR = "iron_condor"
    PUT_CREDIT_SPREAD = "put_credit_spread"
    CALL_CREDIT_SPREAD = "call_credit_spread"

    @property
    def is_defined_risk(self) -> bool:
        return self in (
            Strategy.IRON_CONDOR,
            Strategy.PUT_CREDIT_SPREAD,
            Strategy.CALL_CREDIT_SPREAD,
        )


class OptionType(str, Enum):
    PUT = "put"
    CALL = "call"


class Action(str, Enum):
    SELL_TO_OPEN = "sell_to_open"
    BUY_TO_OPEN = "buy_to_open"
    SELL_TO_CLOSE = "sell_to_close"
    BUY_TO_CLOSE = "buy_to_close"


@dataclass(frozen=True)
class Leg:
    """One option leg of a candidate or open trade."""

    option_type: OptionType
    strike: float
    expiration: date
    action: Action
    quantity: int = 1
    delta: float = 0.0  # signed greek delta of the leg as quoted

    @property
    def is_short(self) -> bool:
        return self.action in (Action.SELL_TO_OPEN, Action.SELL_TO_CLOSE)


@dataclass(frozen=True)
class Liquidity:
    bid_ask_width_pct: float  # (ask - bid) / mid
    open_interest: int
    daily_volume: int


@dataclass(frozen=True)
class CandidateTrade:
    """A proposed opening trade, before guardrail/risk validation."""

    symbol: str
    strategy: Strategy
    legs: tuple[Leg, ...]
    dte: int
    net_credit: float  # credit received to open (per contract, dollars)
    max_profit: float  # dollars per position
    max_loss: float  # dollars per position; float('inf') for undefined risk
    buying_power_reduction: float  # dollars of BP per position
    underlying_price: float
    iv_rank: float  # 0..1
    liquidity: Liquidity
    earnings_in_days: int | None = None  # None == no earnings scheduled in window

    @property
    def max_short_leg_delta(self) -> float:
        shorts = [abs(leg.delta) for leg in self.legs if leg.is_short]
        return max(shorts) if shorts else 0.0

    @property
    def probability_of_profit(self) -> float:
        return probability_of_profit([leg.delta for leg in self.legs if leg.is_short])

    @property
    def strike_width(self) -> float | None:
        """Strike width of defined-risk spread or iron condor (in dollars/share)."""
        return calculate_strike_width(self.strategy, self.legs)

    @property
    def credit_width_ratio(self) -> float | None:
        """Ratio of per-share net credit collected to strike width (Tastytrade 1/3 rule)."""
        width = self.strike_width
        if width and width > 0:
            return (self.net_credit / 100.0) / width
        return None


def calculate_strike_width(strategy: Strategy | str, legs: tuple[Leg, ...] | list[Any]) -> float | None:
    """Calculate the defined-risk strike width for credit spreads and iron condors.

    - Credit Spread: abs(short_strike - long_strike)
    - Iron Condor: max(put_wing_width, call_wing_width)
    - Undefined-risk trades (strangles, naked puts/calls): returns None.
    """
    strat_val = strategy.value if isinstance(strategy, Strategy) else str(strategy).lower()
    if strat_val in (
        Strategy.PUT_CREDIT_SPREAD.value,
        Strategy.CALL_CREDIT_SPREAD.value,
    ):
        short_legs = [
            l
            for l in legs
            if (getattr(l, "is_short", False) or "sell" in str(getattr(l, "action", "")).lower())
        ]
        long_legs = [
            l
            for l in legs
            if (
                (hasattr(l, "is_short") and not l.is_short)
                or "buy" in str(getattr(l, "action", "")).lower()
            )
        ]
        if short_legs and long_legs:
            return round(abs(short_legs[0].strike - long_legs[0].strike), 4)
    elif strat_val == Strategy.IRON_CONDOR.value:
        def _is_short(l) -> bool:
            return getattr(l, "is_short", False) or "sell" in str(getattr(l, "action", "")).lower()

        def _is_long(l) -> bool:
            return (hasattr(l, "is_short") and not l.is_short) or "buy" in str(getattr(l, "action", "")).lower()

        def _is_put(l) -> bool:
            t = getattr(l, "option_type", "")
            val = t.value if hasattr(t, "value") else str(t)
            return val.lower().endswith("put")

        def _is_call(l) -> bool:
            t = getattr(l, "option_type", "")
            val = t.value if hasattr(t, "value") else str(t)
            return val.lower().endswith("call")

        ps = [l for l in legs if _is_put(l) and _is_short(l)]
        pl = [l for l in legs if _is_put(l) and _is_long(l)]
        cs = [l for l in legs if _is_call(l) and _is_short(l)]
        cl = [l for l in legs if _is_call(l) and _is_long(l)]

        put_w = abs(ps[0].strike - pl[0].strike) if ps and pl else 0.0
        call_w = abs(cs[0].strike - cl[0].strike) if cs and cl else 0.0
        w = max(put_w, call_w)
        return round(w, 4) if w > 0 else None
    return None


@dataclass(frozen=True)
class OpenPosition:
    """An open trade we are managing."""

    symbol: str
    strategy: Strategy
    legs: tuple[Leg, ...]
    entry_date: date
    entry_credit: float  # credit received at open (per position, dollars)
    current_cost_to_close: float  # debit to close now (per position, dollars)
    buying_power_reduction: float
    dte_remaining: int
    as_of: date
    current_max_short_delta: float | None = (
        None  # live |delta| of the most-tested short leg
    )
    strike_width: float | None = None
    contracts: int = 1

    def __post_init__(self):
        if self.strike_width is None and self.legs:
            calculated = calculate_strike_width(self.strategy, self.legs)
            if calculated is not None:
                object.__setattr__(self, "strike_width", calculated)

    @property
    def days_held(self) -> int:
        return max((self.as_of - self.entry_date).days, 0)

    @property
    def profit_pct(self) -> float:
        """Fraction of max profit captured. Positive=winning, negative=losing.

        For a credit position max profit == entry credit; current profit is
        the entry credit minus what it costs to close now.
        """
        if self.entry_credit <= 0:
            return 0.0
        return (self.entry_credit - self.current_cost_to_close) / self.entry_credit
