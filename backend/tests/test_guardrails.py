from tastyagent.config import StrategyParams
from tastyagent.models import Liquidity
from tastyagent.strategy.guardrails import filter_candidates, validate_candidate

from .conftest import make_candidate

PARAMS = StrategyParams()


def test_healthy_candidate_passes():
    assert validate_candidate(make_candidate(), PARAMS).ok


def test_low_iv_rank_rejected():
    res = validate_candidate(make_candidate(iv_rank=0.10), PARAMS)
    assert not res.ok
    assert any("iv_rank" in v for v in res.violations)


def test_dte_out_of_window_rejected():
    assert not validate_candidate(make_candidate(dte=7), PARAMS).ok
    assert not validate_candidate(make_candidate(dte=90), PARAMS).ok


def test_excess_short_delta_rejected():
    from tastyagent.models import Action, OptionType
    from .conftest import make_leg

    fat = make_candidate(
        legs=(make_leg(OptionType.PUT, 95.0, Action.SELL_TO_OPEN, -0.45),),
    )
    res = validate_candidate(fat, PARAMS)
    assert not res.ok
    assert any("delta" in v for v in res.violations)


def test_non_credit_rejected():
    assert not validate_candidate(make_candidate(net_credit=0.0), PARAMS).ok


def test_illiquid_rejected():
    illiquid = make_candidate(
        liquidity=Liquidity(bid_ask_width_pct=0.30, open_interest=10, daily_volume=5)
    )
    res = validate_candidate(illiquid, PARAMS)
    assert not res.ok
    assert len(res.violations) >= 3


def test_earnings_blackout_rejected():
    assert not validate_candidate(make_candidate(earnings_in_days=2), PARAMS).ok
    assert validate_candidate(make_candidate(earnings_in_days=30), PARAMS).ok


def test_filter_keeps_only_passing():
    good = make_candidate()
    bad = make_candidate(iv_rank=0.05)
    assert filter_candidates([good, bad], PARAMS) == [good]


def test_defined_risk_one_third_width_rule():
    from tastyagent.models import Action, OptionType, Strategy
    from .conftest import make_leg

    # $5.00 width spread (95 short put, 90 long put)
    legs = (
        make_leg(OptionType.PUT, 95.0, Action.SELL_TO_OPEN, -0.20),
        make_leg(OptionType.PUT, 90.0, Action.BUY_TO_OPEN, -0.08),
    )
    # $1.50 credit ($150 total): ratio = 1.50 / 5.0 = 0.300 < 0.333 -> rejected
    bad = make_candidate(
        strategy=Strategy.PUT_CREDIT_SPREAD,
        legs=legs,
        net_credit=150.0,
        max_profit=150.0,
        max_loss=350.0,
        buying_power_reduction=350.0,
    )
    res_bad = validate_candidate(bad, PARAMS)
    assert not res_bad.ok
    assert any("1/3 rule" in v for v in res_bad.violations)

    # $1.70 credit ($170 total): ratio = 1.70 / 5.0 = 0.340 >= 0.333 -> passes
    good = make_candidate(
        strategy=Strategy.PUT_CREDIT_SPREAD,
        legs=legs,
        net_credit=170.0,
        max_profit=170.0,
        max_loss=330.0,
        buying_power_reduction=330.0,
    )
    res_good = validate_candidate(good, PARAMS)
    assert res_good.ok
