"""Hard entry guardrails encoding TastyTrade mechanics.

A candidate trade MUST pass every guardrail to be eligible. These run BEFORE the
LLM sees candidates and AGAIN after the LLM selects (defense in depth) — the LLM
can never bypass them.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import StrategyParams
from ..models import CandidateTrade

# Indices and broad sector/commodity/bond ETFs have lower upside crash velocity on
# call spreads than single equities and use `index_call_min_credit_width_ratio`.
INDEX_OR_ETF_SYMBOLS = frozenset(
    {
        "SPX",
        "SPY",
        "QQQ",
        "IWM",
        "DIA",
        "XLE",
        "XLF",
        "XLK",
        "XLV",
        "XLY",
        "GLD",
        "SLV",
        "TLT",
        "HYG",
        "EEM",
        "SMH",
        "SOXX",
        "IGV",
        "IBIT",
    }
)


@dataclass(frozen=True)
class GuardrailResult:
    ok: bool
    violations: tuple[str, ...]

    @classmethod
    def passed(cls) -> "GuardrailResult":
        return cls(ok=True, violations=())


def entry_width_ratio(symbol: str, strategy: str, default: float, index_call: float) -> float:
    if strategy == "call_credit_spread" and symbol.upper() in INDEX_OR_ETF_SYMBOLS:
        return index_call
    return default


def _check_wing_geometry(candidate: CandidateTrade) -> list[str]:
    """Ensure multi-leg trades do not invert strikes or invert protective wings."""
    v: list[str] = []
    short_puts = [l.strike for l in candidate.legs if l.option_type.value == "put" and l.is_short]
    long_puts = [l.strike for l in candidate.legs if l.option_type.value == "put" and not l.is_short]
    short_calls = [l.strike for l in candidate.legs if l.option_type.value == "call" and l.is_short]
    long_calls = [l.strike for l in candidate.legs if l.option_type.value == "call" and not l.is_short]

    if short_puts and short_calls and max(short_puts) >= min(short_calls):
        v.append(
            f"inverted strangle/condor strikes: short put {max(short_puts)} >= short call {min(short_calls)}"
        )
    if short_puts and long_puts and max(long_puts) >= min(short_puts):
        v.append(
            f"invalid put spread wings: long put {max(long_puts)} >= short put {min(short_puts)}"
        )
    if short_calls and long_calls and min(long_calls) <= max(short_calls):
        v.append(
            f"invalid call spread wings: long call {min(long_calls)} <= short call {max(short_calls)}"
        )
    return v


def validate_candidate(
    candidate: CandidateTrade, params: StrategyParams
) -> GuardrailResult:
    """Validate a single candidate against the entry rails."""
    v: list[str] = []

    # Sell premium only when implied volatility is elevated.
    if candidate.iv_rank < params.min_iv_rank:
        v.append(f"iv_rank {candidate.iv_rank:.2f} < min {params.min_iv_rank:.2f}")

    # Days to expiration window (~45 DTE).
    if not (params.min_dte <= candidate.dte <= params.max_dte):
        v.append(f"dte {candidate.dte} outside [{params.min_dte}, {params.max_dte}]")

    # Delta-based strike selection: defined-risk spreads sell slightly closer (~30Δ)
    # than undefined-risk strangles/naked puts (~16-25Δ) to meet credit/width rules.
    max_delta = (
        params.spread_max_short_leg_delta
        if candidate.strategy.is_defined_risk
        else params.max_short_leg_delta
    )
    short_delta = candidate.max_short_leg_delta
    if short_delta > max_delta:
        v.append(f"short-leg delta {short_delta:.2f} > max {max_delta:.2f}")

    v.extend(_check_wing_geometry(candidate))

    # Must collect a credit to open (premium-selling agent).
    if candidate.net_credit <= 0:
        v.append(f"net_credit {candidate.net_credit:.2f} is not a credit")
    elif candidate.strategy.is_defined_risk:
        width = candidate.strike_width
        if width is not None and width > 0:
            credit_ps = candidate.net_credit / 100.0
            ratio = credit_ps / width
            min_ratio = entry_width_ratio(
                candidate.symbol, candidate.strategy.value,
                params.min_credit_width_ratio, params.index_call_min_credit_width_ratio,
            )
            if ratio < min_ratio:
                v.append(
                    f"credit/width ratio {ratio:.3f} (${credit_ps:.2f}/${width:.2f}) < "
                    f"min {min_ratio:.3f} (1/3 rule / 25%-33% rule)"
                )

    # Liquidity filters (check OI/volume only when actually reported, not placeholder-faked).
    liq = candidate.liquidity
    if liq.bid_ask_width_pct > params.max_bid_ask_width_pct:
        v.append(
            f"bid/ask width {liq.bid_ask_width_pct:.2%} > max "
            f"{params.max_bid_ask_width_pct:.2%}"
        )
    if liq.open_interest is not None and liq.open_interest < params.min_open_interest:
        v.append(f"open interest {liq.open_interest} < min {params.min_open_interest}")
    if liq.daily_volume is not None and liq.daily_volume < params.min_daily_volume:
        v.append(f"daily volume {liq.daily_volume} < min {params.min_daily_volume}")

    # Earnings blackout: avoid opening into an imminent earnings event.
    if (
        candidate.earnings_in_days is not None
        and candidate.earnings_in_days < params.earnings_blackout_days
    ):
        v.append(
            f"earnings in {candidate.earnings_in_days}d < blackout "
            f"{params.earnings_blackout_days}d"
        )

    return GuardrailResult(ok=not v, violations=tuple(v))


def validate_roll_candidate(
    replacement: CandidateTrade,
    *,
    roll_kind: str,
    exit_debit_total: float,
    contracts: int,
    original_dte_remaining: int,
    original_strike_width: float | None,
    params: StrategyParams,
    max_roll_bid_ask_width_pct: float = 0.50,
) -> GuardrailResult:
    """Validate a defensive or time-extension roll replacement.

    Exempts new-entry constraints (min_iv_rank, earnings blackout, entry delta caps,
    and entry credit/width ratios) and, for untested-side adjustments, exempts DTE
    because rolling the untested side stays in the same expiration cycle.
    Instead verifies: net credit >= close debit, non-expanding defined-risk width,
    non-inverted strike geometry, and quoted markets.
    """
    v: list[str] = []

    if replacement.net_credit <= 0:
        v.append(f"roll net_credit {replacement.net_credit:.2f} is not a credit")
    else:
        total_new_credit = replacement.net_credit * max(1, contracts)
        if total_new_credit + 1e-6 < exit_debit_total:
            v.append(
                f"roll credit ${total_new_credit:.2f} < close debit ${exit_debit_total:.2f} (not a net credit roll)"
            )

    if roll_kind == "out":
        if replacement.dte <= original_dte_remaining:
            v.append(
                f"roll-out dte {replacement.dte} did not extend beyond {original_dte_remaining}"
            )
        if replacement.dte > params.max_dte + 10:
            v.append(
                f"roll-out dte {replacement.dte} > max {params.max_dte + 10}"
            )

    if (
        original_strike_width is not None
        and replacement.strike_width is not None
        and replacement.strike_width > original_strike_width + 1e-6
    ):
        v.append(
            f"roll widens strike width from ${original_strike_width:.2f} to ${replacement.strike_width:.2f}"
        )

    v.extend(_check_wing_geometry(replacement))

    if replacement.liquidity.bid_ask_width_pct >= max_roll_bid_ask_width_pct:
        v.append(
            f"roll quote bid/ask width {replacement.liquidity.bid_ask_width_pct:.2%} >= max {max_roll_bid_ask_width_pct:.2%}"
        )

    return GuardrailResult(ok=not v, violations=tuple(v))


def filter_candidates(
    candidates: list[CandidateTrade], params: StrategyParams
) -> list[CandidateTrade]:
    """Keep only candidates that pass all guardrails."""
    return [c for c in candidates if validate_candidate(c, params).ok]


def is_vix_below_min(vix: float | None, params: StrategyParams) -> bool:
    """Return True when the minimum-VIX entry filter is enabled and VIX < min_vix."""
    return (
        bool(params.enforce_min_vix)
        and vix is not None
        and float(vix) < params.min_vix
    )
