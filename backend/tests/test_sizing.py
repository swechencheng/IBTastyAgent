from tastyagent.config import RiskLimits
from tastyagent.strategy.sizing import size_trade

from .conftest import make_candidate

# Pin the per-trade cap so these sizing assertions don't depend on the default.
LIMITS = RiskLimits(max_trade_bp_pct=0.05)  # per-trade 5%, total 40%


def test_sizes_to_per_trade_cap():
    # net liq 100k -> per-trade budget 5k; BP 2k/contract -> 2 contracts
    c = make_candidate(buying_power_reduction=2000.0)
    r = size_trade(c, net_liq=100_000, current_bp_used=0, limits=LIMITS)
    assert r.contracts == 2
    assert r.bp_used == 4000.0


def test_total_cap_constrains():
    # already at 38k of 40k total budget on 100k -> only 2k left -> 1 contract
    c = make_candidate(buying_power_reduction=2000.0)
    r = size_trade(c, net_liq=100_000, current_bp_used=38_000, limits=LIMITS)
    assert r.contracts == 1


def test_zero_when_no_budget():
    c = make_candidate(buying_power_reduction=2000.0)
    r = size_trade(c, net_liq=100_000, current_bp_used=40_000, limits=LIMITS)
    assert r.contracts == 0


def test_zero_when_contract_too_large():
    c = make_candidate(buying_power_reduction=9000.0)  # > 5k per-trade budget
    r = size_trade(c, net_liq=100_000, current_bp_used=0, limits=LIMITS)
    assert r.contracts == 0


def test_invalid_inputs():
    c = make_candidate(buying_power_reduction=0.0)
    assert (
        size_trade(c, net_liq=100_000, current_bp_used=0, limits=LIMITS).contracts == 0
    )
    c2 = make_candidate(buying_power_reduction=2000.0)
    assert size_trade(c2, net_liq=0, current_bp_used=0, limits=LIMITS).contracts == 0


def test_strategy_bp_constrains():
    from tastyagent.models import Strategy
    # Defined-risk spread: max_spread_bp_pct = 0.20 ($20,000 on $100k net liq)
    # Even if total BP has plenty of room (current_bp_used = 10,000),
    # if spread BP is at 18,500 of 20,000 -> only 1,500 remaining -> 0 contracts of a 2,000 BP trade
    c = make_candidate(strategy=Strategy.PUT_CREDIT_SPREAD, buying_power_reduction=2000.0)
    r = size_trade(
        c,
        net_liq=100_000,
        current_bp_used=10_000,
        limits=LIMITS,
        current_strategy_bp_used=18_500,
    )
    assert r.contracts == 0

    # If spread BP is at 17,000 of 20,000 -> 3,000 remaining -> 1 contract
    r2 = size_trade(
        c,
        net_liq=100_000,
        current_bp_used=10_000,
        limits=LIMITS,
        current_strategy_bp_used=17_000,
    )
    assert r2.contracts == 1
