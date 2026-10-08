"""Decision cycle orchestrator.

Pipeline (one cycle):
  1. pre-guardrail filter   — drop candidates that fail hard entry rails
  2. LLM selection          — OpenRouter LLM picks/sizes among guardrail-passing candidates
  3. post-LLM re-validation  — re-run guardrails + sizing + portfolio risk on each
                               pick (defense in depth: the LLM can never bypass the
                               rails, and sizing/risk are recomputed against running
                               buying-power as picks accumulate)
  4. emit planned trades + rejections + the LLM's commentary

Candidate *generation* (building CandidateTrades from live chains/greeks/IV rank)
is a separate concern; this orchestrator takes candidates as input so it stays
deterministic and unit-testable with a stubbed selector.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..config import RiskLimits, StrategyParams
from ..models import CandidateTrade
from ..risk.limits import PortfolioState, check_new_entry
from ..strategy.guardrails import validate_candidate
from ..strategy.sizing import size_trade
from .llm import LLMDecision, select_trades


@dataclass
class PortfolioInput:
    """Current account state fed into a decision cycle."""

    net_liq: float
    bp_used: float
    positions_by_symbol: dict[str, int] = field(default_factory=dict)
    realized_pnl_today: float = 0.0
    consecutive_losses: int = 0
    cushion: float | None = None
    is_live: bool = False

    @property
    def open_positions(self) -> int:
        return sum(self.positions_by_symbol.values())


@dataclass
class PlannedTrade:
    candidate: CandidateTrade
    contracts: int
    rationale: str

    @property
    def buying_power(self) -> float:
        return self.contracts * self.candidate.buying_power_reduction


@dataclass
class CycleResult:
    planned: list[PlannedTrade]
    rejected: list[tuple[CandidateTrade, str]]
    commentary: str
    considered: int  # how many candidates passed the pre-guardrail filter


def portfolio_summary(p: PortfolioInput) -> dict:
    """Compact, LLM-facing view of the portfolio."""
    used_pct = (p.bp_used / p.net_liq) if p.net_liq else 0.0
    summary = {
        "net_liq": round(p.net_liq, 2),
        "buying_power_used": round(p.bp_used, 2),
        "buying_power_used_pct": round(used_pct, 4),
        "open_positions": p.open_positions,
        "open_symbols": sorted(p.positions_by_symbol),
    }
    if p.cushion is not None:
        summary["account_cushion_pct"] = round(p.cushion, 4)
    return summary


async def run_cycle(
    candidates: list[CandidateTrade],
    portfolio: PortfolioInput,
    regime: dict,
    params: StrategyParams,
    limits: RiskLimits,
    *,
    selector=select_trades,
) -> CycleResult:
    if portfolio.net_liq <= 0:
        return CycleResult(
            planned=[],
            rejected=[(c, "insufficient capital: net_liq <= 0") for c in candidates],
            commentary=f"Account has non-positive capital (${portfolio.net_liq:,.2f}); new entries halted per capital control.",
            considered=0,
        )

    # Live cushion guard: halt opening new positions if IBKR account cushion < min_cushion_pct (default: 30%)
    if (
        portfolio.is_live
        and portfolio.cushion is not None
        and portfolio.cushion < limits.min_cushion_pct
    ):
        return CycleResult(
            planned=[],
            rejected=[
                (
                    c,
                    f"cushion guard: IBKR account cushion {portfolio.cushion:.1%} < min {limits.min_cushion_pct:.0%}",
                )
                for c in candidates
            ],
            commentary=(
                f"IBKR account cushion ({portfolio.cushion:.1%}) is below the "
                f"{limits.min_cushion_pct:.0%} minimum threshold; opening new positions is halted per live risk guard."
            ),
            considered=0,
        )

    # 1. pre-guardrail filter
    passing = [c for c in candidates if validate_candidate(c, params).ok]

    # 2. adaptive selection over guardrail-passing candidates only
    decision: LLMDecision
    decision, id_map = await selector(passing, portfolio_summary(portfolio), regime)

    # 3. post-LLM re-validation against running state
    planned: list[PlannedTrade] = []
    rejected: list[tuple[CandidateTrade, str]] = []
    running_bp = portfolio.bp_used
    counts = defaultdict(int, dict(portfolio.positions_by_symbol))

    for sel in decision.selections:
        cand = id_map.get(sel.candidate_id)
        if cand is None:
            continue  # hallucinated id (llm layer already filters, belt-and-suspenders)

        gr = validate_candidate(cand, params)
        if not gr.ok:
            rejected.append((cand, "guardrail: " + "; ".join(gr.violations)))
            continue

        sizing = size_trade(cand, portfolio.net_liq, running_bp, limits)
        contracts = min(sel.contracts, sizing.contracts)
        if contracts < 1:
            rejected.append((cand, f"sizing: {sizing.reason}"))
            continue

        incremental_bp = contracts * cand.buying_power_reduction
        state = PortfolioState(
            net_liq=portfolio.net_liq,
            bp_used=running_bp,
            open_positions=sum(counts.values()),
            positions_for_symbol=counts[cand.symbol],
            realized_pnl_today=portfolio.realized_pnl_today,
            consecutive_losses=portfolio.consecutive_losses,
            cushion=portfolio.cushion,
            is_live=portfolio.is_live,
        )
        risk = check_new_entry(cand.symbol, incremental_bp, state, limits)
        if not risk.ok:
            rejected.append((cand, "risk: " + "; ".join(risk.violations)))
            continue

        planned.append(PlannedTrade(cand, contracts, sel.rationale))
        running_bp += incremental_bp
        counts[cand.symbol] += 1

    return CycleResult(
        planned=planned,
        rejected=rejected,
        commentary=decision.commentary,
        considered=len(passing),
    )
