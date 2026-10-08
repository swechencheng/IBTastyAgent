"""Hard entry guardrails encoding TastyTrade mechanics.

A candidate trade MUST pass every guardrail to be eligible. These run BEFORE the
LLM sees candidates and AGAIN after the LLM selects (defense in depth) — the LLM
can never bypass them.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import StrategyParams
from ..models import CandidateTrade


@dataclass(frozen=True)
class GuardrailResult:
    ok: bool
    violations: tuple[str, ...]

    @classmethod
    def passed(cls) -> "GuardrailResult":
        return cls(ok=True, violations=())


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

    # Delta-based strike selection: cap risk on short legs.
    short_delta = candidate.max_short_leg_delta
    if short_delta > params.max_short_leg_delta:
        v.append(
            f"short-leg delta {short_delta:.2f} > max {params.max_short_leg_delta:.2f}"
        )

    # Must collect a credit to open (premium-selling agent).
    if candidate.net_credit <= 0:
        v.append(f"net_credit {candidate.net_credit:.2f} is not a credit")
    elif candidate.strategy.is_defined_risk:
        width = candidate.strike_width
        if width is not None and width > 0:
            credit_ps = candidate.net_credit / 100.0
            ratio = credit_ps / width
            if ratio < params.min_credit_width_ratio:
                v.append(
                    f"credit/width ratio {ratio:.3f} (${credit_ps:.2f}/${width:.2f}) < "
                    f"min {params.min_credit_width_ratio:.3f} (1/3 rule)"
                )

    # Liquidity filters.
    liq = candidate.liquidity
    if liq.bid_ask_width_pct > params.max_bid_ask_width_pct:
        v.append(
            f"bid/ask width {liq.bid_ask_width_pct:.2%} > max "
            f"{params.max_bid_ask_width_pct:.2%}"
        )
    if liq.open_interest < params.min_open_interest:
        v.append(f"open interest {liq.open_interest} < min {params.min_open_interest}")
    if liq.daily_volume < params.min_daily_volume:
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


def filter_candidates(
    candidates: list[CandidateTrade], params: StrategyParams
) -> list[CandidateTrade]:
    """Keep only candidates that pass all guardrails."""
    return [c for c in candidates if validate_candidate(c, params).ok]
