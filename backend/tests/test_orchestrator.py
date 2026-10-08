"""Orchestrator tests with a stubbed LLM selector (no network).

Verifies the safety-critical part: whatever the LLM returns is re-validated against
guardrails, sizing, and portfolio risk before becoming a planned trade.
"""

from tastyagent.config import RiskLimits, StrategyParams
from tastyagent.decision.llm import LLMDecision, LLMTradeSelection, candidate_id
from tastyagent.decision.orchestrator import PortfolioInput, run_cycle

from .conftest import make_candidate

PARAMS = StrategyParams()
# Pin per-trade cap so sizing assertions don't depend on the default.
LIMITS = RiskLimits(max_trade_bp_pct=0.05)  # 5% per-trade, 40% total, 2 per symbol


def selector_picking(picks):
    """picks: list of (index_in_passing, contracts). Adds a hallucinated id too."""

    async def selector(candidates, portfolio, regime):
        id_map = {candidate_id(c, i): c for i, c in enumerate(candidates)}
        ids = list(id_map)
        sels = [
            LLMTradeSelection(candidate_id=ids[i], contracts=n, rationale="stub pick")
            for i, n in picks
            if i < len(ids)
        ]
        sels.append(
            LLMTradeSelection(
                candidate_id="GHOST-naked_put-99", contracts=1, rationale="ghost"
            )
        )
        return LLMDecision(selections=sels, commentary="stub"), id_map

    return selector


def portfolio(net_liq=100_000.0, bp_used=0.0, positions=None, **kw):
    return PortfolioInput(
        net_liq=net_liq, bp_used=bp_used, positions_by_symbol=positions or {}, **kw
    )


async def test_pre_guardrail_filters_before_llm():
    good = make_candidate(symbol="SPY")
    bad = make_candidate(symbol="QQQ", iv_rank=0.05)  # fails IVR guardrail
    # selector tries to pick both passing candidates (only `good` is passing)
    res = await run_cycle(
        [good, bad],
        portfolio(),
        {},
        PARAMS,
        LIMITS,
        selector=selector_picking([(0, 1), (1, 1)]),
    )
    assert res.considered == 1
    assert len(res.planned) == 1
    assert res.planned[0].candidate.symbol == "SPY"


async def test_hallucinated_id_ignored():
    good = make_candidate(symbol="SPY")
    res = await run_cycle(
        [good], portfolio(), {}, PARAMS, LIMITS, selector=selector_picking([])
    )  # only the ghost pick
    assert res.planned == []


async def test_sizing_caps_llm_quantity():
    # 2000 BP/contract, 5% of 100k = 5k/trade -> max 2 contracts even if LLM asks 10
    c = make_candidate(symbol="SPY", buying_power_reduction=2000.0)
    res = await run_cycle(
        [c], portfolio(), {}, PARAMS, LIMITS, selector=selector_picking([(0, 10)])
    )
    assert len(res.planned) == 1
    assert res.planned[0].contracts == 2


async def test_oversized_contract_rejected_by_sizing():
    c = make_candidate(symbol="SPY", buying_power_reduction=9000.0)  # > 5k per-trade
    res = await run_cycle(
        [c], portfolio(), {}, PARAMS, LIMITS, selector=selector_picking([(0, 1)])
    )
    assert res.planned == []
    assert any("sizing" in reason for _, reason in res.rejected)


async def test_per_symbol_concentration_rejects_third():
    cands = [
        make_candidate(symbol="SPY", buying_power_reduction=2000.0) for _ in range(3)
    ]
    res = await run_cycle(
        cands,
        portfolio(),
        {},
        PARAMS,
        LIMITS,
        selector=selector_picking([(0, 1), (1, 1), (2, 1)]),
    )
    assert len(res.planned) == 2  # max_positions_per_symbol = 2
    assert any("risk" in reason for _, reason in res.rejected)


async def test_running_bp_accumulates_across_picks():
    # total budget 40% of 100k = 40k; 9k/contract trades, but per-trade cap 5% = 5k
    # so each trade sizes to 0 -> use smaller bp to test total-cap accumulation
    cands = [
        make_candidate(symbol=f"S{i}", buying_power_reduction=2000.0) for i in range(3)
    ]
    res = await run_cycle(
        cands,
        portfolio(bp_used=37_000.0),
        {},
        PARAMS,
        LIMITS,
        selector=selector_picking([(0, 1), (1, 1), (2, 1)]),
    )
    # 37k used, 40k cap -> only 3k headroom -> first trade (2k) ok, leaves 1k -> rest rejected
    assert len(res.planned) == 1
    planned_bp = sum(p.buying_power for p in res.planned)
    assert 37_000 + planned_bp <= LIMITS.max_total_bp_pct * 100_000


async def test_non_positive_capital_halts_entries():
    c = make_candidate(symbol="SPY")
    # When net_liq is 0 or negative (insufficient/zero Total Cash), no entries can be planned
    res = await run_cycle(
        [c],
        portfolio(net_liq=0.0),
        {},
        PARAMS,
        LIMITS,
        selector=selector_picking([(0, 1)]),
    )
    assert len(res.planned) == 0
    assert len(res.rejected) == 1
    assert "insufficient capital" in res.rejected[0][1]


async def test_live_cushion_below_threshold_halts_entries():
    c = make_candidate(symbol="SPY")
    # In live mode with cushion = 25% (< 30% min), new entries must be halted immediately
    live_p = portfolio(net_liq=100_000.0)
    live_p.cushion = 0.25
    live_p.is_live = True

    res = await run_cycle(
        [c],
        live_p,
        {},
        PARAMS,
        LIMITS,
        selector=selector_picking([(0, 1)]),
    )
    assert len(res.planned) == 0
    assert len(res.rejected) == 1
    assert "cushion guard" in res.rejected[0][1]
    assert "25.0%" in res.commentary


async def test_sandbox_mode_allows_low_cushion():
    c = make_candidate(symbol="SPY")
    # In sandbox mode (is_live=False), cushion < 30% does NOT halt
    sandbox_p = portfolio(net_liq=100_000.0)
    sandbox_p.cushion = 0.20
    sandbox_p.is_live = False

    res = await run_cycle(
        [c],
        sandbox_p,
        {},
        PARAMS,
        LIMITS,
        selector=selector_picking([(0, 1)]),
    )
    assert len(res.planned) == 1
