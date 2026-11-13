"""Decision cycle orchestrator.

Pipeline (one cycle):
  1. pre-filter              — drop candidates failing hard entry rails, initial sizing, or portfolio risk
  2. LLM selection           — OpenRouter LLM picks/sizes among feasible candidates only
  3. post-LLM re-validation  — re-run guardrails + sizing + portfolio risk on each
                               pick (defense in depth: the LLM can never bypass the
                               rails, and sizing/risk are recomputed against running
                               buying-power and slot limits as picks accumulate)
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
    losing_symbols: set[str] = field(default_factory=set)
    spread_bp_used: float = 0.0
    undefined_bp_used: float = 0.0

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


def portfolio_summary(
    p: PortfolioInput, limits: RiskLimits | None = None
) -> dict:
    """Compact, LLM-facing view of the portfolio and remaining capacity."""
    used_pct = (p.bp_used / p.net_liq) if p.net_liq else 0.0
    summary: dict = {
        "net_liq": round(p.net_liq, 2),
        "buying_power_used": round(p.bp_used, 2),
        "buying_power_used_pct": round(used_pct, 4),
        "open_positions": p.open_positions,
        "open_symbols": sorted(p.positions_by_symbol),
    }
    if p.losing_symbols:
        summary["losing_symbols_cooldown"] = sorted(p.losing_symbols)
    if limits is not None:
        summary["max_trade_bp"] = round(limits.max_trade_bp_pct * p.net_liq, 2)
        summary["total_bp_remaining"] = round(
            max(0.0, limits.max_total_bp_pct * p.net_liq - p.bp_used), 2
        )
        summary["spread_bp_remaining"] = round(
            max(0.0, limits.max_spread_bp_pct * p.net_liq - p.spread_bp_used), 2
        )
        summary["undefined_bp_remaining"] = round(
            max(0.0, limits.max_undefined_bp_pct * p.net_liq - p.undefined_bp_used), 2
        )
        summary["open_position_slots_remaining"] = max(
            0, limits.max_positions - p.open_positions
        )
    if p.cushion is not None:
        summary["account_cushion_pct"] = round(p.cushion, 4)
    return summary


def _evaluate_candidate(
    cand: CandidateTrade,
    portfolio: PortfolioInput,
    params: StrategyParams,
    limits: RiskLimits,
    *,
    running_bp: float,
    running_spread_bp: float,
    running_undefined_bp: float,
    open_positions: int,
    symbol_positions: int,
    requested_contracts: int | None = None,
) -> tuple[int, str | None, str | None]:
    """Validate guardrails, sizing, and portfolio risk for one candidate.

    Returns `(contracts, failure_stage, reason)` where `failure_stage` is one of
    `"guardrail"`, `"sizing"`, `"risk"`, or `None` when feasible.
    """
    gr = validate_candidate(cand, params)
    if not gr.ok:
        return 0, "guardrail", "guardrail: " + "; ".join(gr.violations)

    current_strat_bp = (
        running_spread_bp if cand.strategy.is_defined_risk else running_undefined_bp
    )
    sizing = size_trade(
        cand,
        portfolio.net_liq,
        running_bp,
        limits,
        current_strategy_bp_used=current_strat_bp,
    )
    contracts = (
        sizing.contracts
        if requested_contracts is None
        else min(requested_contracts, sizing.contracts)
    )
    if contracts < 1:
        return 0, "sizing", f"sizing: {sizing.reason}"

    eval_contracts = 1 if requested_contracts is None else contracts
    incremental_bp = eval_contracts * cand.buying_power_reduction
    state = PortfolioState(
        net_liq=portfolio.net_liq,
        bp_used=running_bp,
        open_positions=open_positions,
        positions_for_symbol=symbol_positions,
        realized_pnl_today=portfolio.realized_pnl_today,
        consecutive_losses=portfolio.consecutive_losses,
        cushion=portfolio.cushion,
        is_live=portfolio.is_live,
        losing_symbols=frozenset(portfolio.losing_symbols),
        spread_bp_used=running_spread_bp,
        undefined_bp_used=running_undefined_bp,
    )
    risk = check_new_entry(
        cand.symbol,
        incremental_bp,
        state,
        limits,
        is_defined_risk=cand.strategy.is_defined_risk,
    )
    if not risk.ok:
        return 0, "risk", "risk: " + "; ".join(risk.violations)

    return contracts, None, None


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

    # 1. pre-filter by entry guardrails AND initial sizing/risk feasibility
    feasible: list[CandidateTrade] = []
    pre_rejected: list[tuple[CandidateTrade, str]] = []
    for c in candidates:
        _, stage, reason = _evaluate_candidate(
            c,
            portfolio,
            params,
            limits,
            running_bp=portfolio.bp_used,
            running_spread_bp=portfolio.spread_bp_used,
            running_undefined_bp=portfolio.undefined_bp_used,
            open_positions=portfolio.open_positions,
            symbol_positions=portfolio.positions_by_symbol.get(c.symbol, 0),
        )
        if stage is None:
            feasible.append(c)
        elif stage in {"sizing", "risk"} and reason is not None:
            pre_rejected.append((c, reason))

    if not feasible:
        return CycleResult(
            planned=[],
            rejected=pre_rejected,
            commentary="No feasible candidates passed guardrails, sizing, and risk checks.",
            considered=0,
        )

    # 2. adaptive selection over feasible candidates only
    decision: LLMDecision
    decision, id_map = await selector(feasible, portfolio_summary(portfolio, limits), regime)

    # 3. post-LLM re-validation against running state
    planned: list[PlannedTrade] = []
    rejected: list[tuple[CandidateTrade, str]] = list(pre_rejected)
    running_bp = portfolio.bp_used
    running_spread_bp = portfolio.spread_bp_used
    running_undefined_bp = portfolio.undefined_bp_used
    counts = defaultdict(int, dict(portfolio.positions_by_symbol))

    for sel in decision.selections:
        cand = id_map.get(sel.candidate_id)
        if cand is None:
            continue  # hallucinated id (llm layer already filters, belt-and-suspenders)

        contracts, _, reason = _evaluate_candidate(
            cand,
            portfolio,
            params,
            limits,
            running_bp=running_bp,
            running_spread_bp=running_spread_bp,
            running_undefined_bp=running_undefined_bp,
            open_positions=sum(counts.values()),
            symbol_positions=counts[cand.symbol],
            requested_contracts=sel.contracts,
        )
        if contracts < 1:
            if reason is not None:
                rejected.append((cand, reason))
            continue

        incremental_bp = contracts * cand.buying_power_reduction
        planned.append(PlannedTrade(cand, contracts, sel.rationale))
        running_bp += incremental_bp
        if cand.strategy.is_defined_risk:
            running_spread_bp += incremental_bp
        else:
            running_undefined_bp += incremental_bp
        counts[cand.symbol] += 1

    return CycleResult(
        planned=planned,
        rejected=rejected,
        commentary=decision.commentary,
        considered=len(feasible),
    )
