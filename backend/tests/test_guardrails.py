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
    # $1.00 credit ($100 total): ratio = 1.00 / 5.0 = 0.200 < 0.250 -> rejected
    bad = make_candidate(
        strategy=Strategy.PUT_CREDIT_SPREAD,
        legs=legs,
        net_credit=100.0,
        max_profit=100.0,
        max_loss=400.0,
        buying_power_reduction=400.0,
    )
    res_bad = validate_candidate(bad, PARAMS)
    assert not res_bad.ok
    assert any("1/3 rule" in v for v in res_bad.violations)

    # $1.50 credit ($150 total): ratio = 1.50 / 5.0 = 0.300 >= 0.250 -> passes
    good = make_candidate(
        strategy=Strategy.PUT_CREDIT_SPREAD,
        legs=legs,
        net_credit=150.0,
        max_profit=150.0,
        max_loss=350.0,
        buying_power_reduction=350.0,
    )
    res_good = validate_candidate(good, PARAMS)
    assert res_good.ok

    # Index call credit spread: $1.05 credit on $5.0 width (ratio 0.21) passes index 0.20 rule
    index_call_legs = (
        make_leg(OptionType.CALL, 100.0, Action.SELL_TO_OPEN, 0.20),
        make_leg(OptionType.CALL, 105.0, Action.BUY_TO_OPEN, 0.08),
    )
    index_good = make_candidate(
        symbol="SPY",
        strategy=Strategy.CALL_CREDIT_SPREAD,
        legs=index_call_legs,
        net_credit=105.0,
        max_profit=105.0,
        max_loss=395.0,
        buying_power_reduction=395.0,
    )
    assert validate_candidate(index_good, PARAMS).ok


def test_validate_candidate_allows_none_oi_and_volume():
    cand = make_candidate(
        liquidity=Liquidity(bid_ask_width_pct=0.05, open_interest=None, daily_volume=None)
    )
    assert validate_candidate(cand, PARAMS).ok


def test_validate_candidate_spread_vs_undefined_delta_caps():
    from tastyagent.models import Action, OptionType, Strategy
    from .conftest import make_leg

    spread_32 = make_candidate(
        symbol="XLF",
        strategy=Strategy.PUT_CREDIT_SPREAD,
        legs=(
            make_leg(OptionType.PUT, 45.0, Action.SELL_TO_OPEN, -0.32),
            make_leg(OptionType.PUT, 42.0, Action.BUY_TO_OPEN, -0.10),
        ),
        net_credit=85.0,
        max_profit=85.0,
        max_loss=215.0,
        buying_power_reduction=215.0,
    )
    assert validate_candidate(spread_32, PARAMS).ok

    strangle_32 = make_candidate(
        symbol="XLF",
        strategy=Strategy.SHORT_STRANGLE,
        legs=(
            make_leg(OptionType.PUT, 45.0, Action.SELL_TO_OPEN, -0.32),
            make_leg(OptionType.CALL, 52.0, Action.SELL_TO_OPEN, 0.18),
        ),
    )
    assert not validate_candidate(strangle_32, PARAMS).ok


def test_validate_roll_candidate_rules():
    from tastyagent.models import Action, OptionType, Strategy
    from tastyagent.strategy.guardrails import validate_roll_candidate
    from .conftest import make_leg

    # 1. UNTESTED roll exempt from IVR, earnings, delta cap, and entry DTE window
    untested = make_candidate(
        iv_rank=0.05,
        dte=18,
        earnings_in_days=1,
        legs=(
            make_leg(OptionType.PUT, 95.0, Action.SELL_TO_OPEN, -0.48),
            make_leg(OptionType.CALL, 108.0, Action.SELL_TO_OPEN, 0.20),
        ),
        liquidity=Liquidity(bid_ask_width_pct=0.25, open_interest=None, daily_volume=None),
        net_credit=350.0,
    )
    res_untested = validate_roll_candidate(
        untested,
        roll_kind="untested",
        exit_debit_total=320.0,
        contracts=1,
        original_dte_remaining=18,
        original_strike_width=None,
        params=PARAMS,
    )
    assert res_untested.ok

    # 2. OUT roll with low credit/width ratio passes when credit >= exit debit & width non-expanding
    out_spread = make_candidate(
        symbol="XLE",
        strategy=Strategy.PUT_CREDIT_SPREAD,
        iv_rank=0.10,
        dte=45,
        earnings_in_days=2,
        legs=(
            make_leg(OptionType.PUT, 50.0, Action.SELL_TO_OPEN, -0.58),
            make_leg(OptionType.PUT, 45.0, Action.BUY_TO_OPEN, -0.35),
        ),
        net_credit=190.0,  # 1.90 / 5 = 38% (or even 0.95 / 5 = 19% if cost=80)
        max_profit=190.0,
        max_loss=310.0,
        buying_power_reduction=310.0,
    )
    assert validate_roll_candidate(
        out_spread,
        roll_kind="out",
        exit_debit_total=180.0,
        contracts=1,
        original_dte_remaining=12,
        original_strike_width=5.0,
        params=PARAMS,
    ).ok

    # 3. Debit roll (new credit < exit debit) must be rejected
    debit_roll = validate_roll_candidate(
        out_spread,
        roll_kind="out",
        exit_debit_total=420.0,  # 2 contracts or high debit > 190
        contracts=1,
        original_dte_remaining=12,
        original_strike_width=5.0,
        params=PARAMS,
    )
    assert not debit_roll.ok

    # 4. Widening spread risk on roll must be rejected
    wider_roll = validate_roll_candidate(
        out_spread,
        roll_kind="out",
        exit_debit_total=150.0,
        contracts=1,
        original_dte_remaining=12,
        original_strike_width=2.5,
        params=PARAMS,
    )
    assert not wider_roll.ok
