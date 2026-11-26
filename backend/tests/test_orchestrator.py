"""Orchestrator tests with a stubbed LLM selector (no network).

Verifies the safety-critical part: whatever the LLM returns is re-validated against
guardrails, sizing, and portfolio risk before becoming a planned trade.
"""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from tastyagent.config import RiskLimits, StrategyParams
from tastyagent.decision.context import build_regime
from tastyagent.decision.llm import LLMDecision, LLMTradeSelection, candidate_id
from tastyagent.decision.orchestrator import PortfolioInput, run_cycle

from .conftest import make_candidate, make_iron_condor

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


async def test_infeasible_candidates_filtered_before_llm_and_summary_enriched():
    good = make_candidate(symbol="SPY", buying_power_reduction=2000.0)
    oversized = make_candidate(symbol="QQQ", buying_power_reduction=9000.0)
    cooldown = make_candidate(symbol="XLE", buying_power_reduction=1500.0)

    seen_symbols = []
    seen_portfolio = {}

    async def recording_selector(candidates, port_summary, regime):
        seen_symbols.extend(c.symbol for c in candidates)
        seen_portfolio.update(port_summary)
        id_map = {candidate_id(c, i): c for i, c in enumerate(candidates)}
        ids = list(id_map)
        return (
            LLMDecision(
                selections=[
                    LLMTradeSelection(
                        candidate_id=ids[0], contracts=1, rationale="feasible"
                    )
                ],
                commentary="picked only feasible",
            ),
            id_map,
        )

    res = await run_cycle(
        [good, oversized, cooldown],
        portfolio(losing_symbols={"XLE"}, bp_used=5000.0),
        {},
        PARAMS,
        LIMITS,
        selector=recording_selector,
    )
    assert seen_symbols == ["SPY"]
    assert res.considered == 1
    assert [p.candidate.symbol for p in res.planned] == ["SPY"]
    assert any("sizing" in reason for _, reason in res.rejected)
    assert any("risk" in reason for _, reason in res.rejected)
    assert seen_portfolio["max_trade_bp"] == 5000.0
    assert seen_portfolio["total_bp_remaining"] == 35_000.0
    assert "spread_bp_remaining" in seen_portfolio
    assert "undefined_bp_remaining" in seen_portfolio
    assert seen_portfolio["losing_symbols_cooldown"] == ["XLE"]


async def test_low_vix_halts_new_entries_when_enabled():
    good = make_candidate(symbol="SPY", buying_power_reduction=2000.0)
    called = False

    async def fail_if_called(candidates, port_summary, regime):
        nonlocal called
        called = True
        return LLMDecision(selections=[], commentary="should not run"), {}

    res = await run_cycle(
        [good],
        portfolio(),
        {"vix": 14.2},
        PARAMS,
        LIMITS,
        selector=fail_if_called,
    )
    assert not called
    assert res.planned == []
    assert res.considered == 0
    assert len(res.rejected) == 1
    assert "vix guard" in res.rejected[0][1]
    assert "14.20" in res.commentary

    # At or above threshold (15.0) allows entries
    res_ok = await run_cycle(
        [good],
        portfolio(),
        {"vix": 15.0},
        PARAMS,
        LIMITS,
        selector=selector_picking([(0, 1)]),
    )
    assert len(res_ok.planned) == 1

    # Disabled toggle allows entries even when VIX < 15
    res_disabled = await run_cycle(
        [good],
        portfolio(),
        {"vix": 12.1},
        replace(PARAMS, enforce_min_vix=False),
        LIMITS,
        selector=selector_picking([(0, 1)]),
    )
    assert len(res_disabled.planned) == 1


@pytest.mark.parametrize("enforce_min", [True, False])
async def test_high_vix_hides_iron_condor_before_llm(enforce_min):
    params = replace(PARAMS, enforce_min_vix=enforce_min)
    condor = make_iron_condor()
    strangle = make_candidate(symbol="QQQ")
    selector = AsyncMock(side_effect=selector_picking([(0, 1)]))

    result = await run_cycle(
        [condor, strangle],
        portfolio(),
        build_regime({}, params, 25.001),
        params,
        LIMITS,
        selector=selector,
    )

    assert selector.await_args.args[0] == [strangle]
    assert result.considered == 1
    assert [plan.candidate for plan in result.planned] == [strangle]
    assert len(result.rejected) == 1
    rejected, reason = result.rejected[0]
    assert rejected == condor
    assert "vix guard:" in reason.lower() and "iron condor" in reason.lower()


@pytest.mark.parametrize("vix", [24.999, 25.0])
async def test_vix_at_or_below_25_allows_iron_condor(vix):
    condor = make_iron_condor()

    result = await run_cycle(
        [condor],
        portfolio(),
        build_regime({}, PARAMS, vix),
        PARAMS,
        LIMITS,
        selector=selector_picking([(0, 1)]),
    )

    assert result.considered == 1
    assert [plan.candidate for plan in result.planned] == [condor]


@pytest.mark.parametrize(
    ("limit", "vix", "eligible"),
    [(20.5, 20.501, False), (30.5, 30.5, True)],
)
async def test_configured_iron_condor_vix_limit_is_used(limit, vix, eligible):
    params = replace(PARAMS, max_iron_condor_entry_vix=limit)
    condor = make_iron_condor()
    selector = AsyncMock(side_effect=selector_picking([(0, 1)]))

    result = await run_cycle(
        [condor],
        portfolio(),
        build_regime({}, params, vix),
        params,
        LIMITS,
        selector=selector,
    )

    if eligible:
        selector.assert_awaited_once()
        assert [plan.candidate for plan in result.planned] == [condor]
    else:
        selector.assert_not_awaited()
        assert result.planned == []
        rejected, reason = result.rejected[0]
        assert rejected == condor
        assert str(limit) in reason


async def test_high_vix_skips_llm_if_only_condors_are_available():
    condor = make_iron_condor()
    selector = AsyncMock()

    result = await run_cycle(
        [condor],
        portfolio(),
        build_regime({}, PARAMS, 30.0),
        PARAMS,
        LIMITS,
        selector=selector,
    )

    selector.assert_not_awaited()
    assert result.considered == 0
    assert result.planned == []
    assert len(result.rejected) == 1
    assert result.rejected[0][0] == condor


async def test_high_vix_rechecks_condor_policy_after_llm_selection():
    condor = make_iron_condor()
    strangle = make_candidate(symbol="QQQ")

    async def stale_selector(candidates, port_summary, regime):
        assert candidates == [strangle]
        condor_id = candidate_id(condor, 0)
        return (
            LLMDecision(
                selections=[
                    LLMTradeSelection(
                        candidate_id=condor_id, contracts=1, rationale="stale candidate"
                    )
                ],
                commentary="picked a stale candidate",
            ),
            {condor_id: condor},
        )

    result = await run_cycle(
        [condor, strangle],
        portfolio(),
        build_regime({}, PARAMS, 30.0),
        PARAMS,
        LIMITS,
        selector=stale_selector,
    )

    assert result.considered == 1
    assert result.planned == []
