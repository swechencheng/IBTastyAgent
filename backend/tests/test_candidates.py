from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from tastyagent.config import StrategyParams
from tastyagent.models import Action, Strategy
from tastyagent.strategy.candidates import (
    build_strangle_candidate,
    estimate_undefined_bp,
    pick_expiration,
)
from tastyagent.strategy.guardrails import validate_candidate
from tastyagent.ibkr.marketdata import OptionSnapshot

PARAMS = StrategyParams()
TODAY = date(2026, 1, 1)


def test_pick_expiration_nearest_in_window():
    exps = [TODAY + timedelta(days=d) for d in (10, 40, 45, 50, 90)]
    assert pick_expiration(exps, PARAMS, TODAY) == TODAY + timedelta(days=45)


def test_pick_expiration_none_when_all_outside_window():
    exps = [TODAY + timedelta(days=d) for d in (5, 10, 120)]
    assert pick_expiration(exps, PARAMS, TODAY) is None


def test_estimate_undefined_bp():
    assert estimate_undefined_bp(100.0) == 2000.0


def test_probability_of_profit():
    from tastyagent.models import probability_of_profit

    assert round(probability_of_profit([-0.16, 0.16]), 2) == 0.68  # 16Δ strangle
    assert round(probability_of_profit([-0.16]), 2) == 0.84  # naked put
    assert probability_of_profit([]) == 1.0
    assert probability_of_profit([-0.9, 0.9]) == 0.0  # clamped


def test_candidate_pop_property():
    c = build_strangle_candidate(
        "SPY",
        100.0,
        0.45,
        45,
        _opt(90),
        _opt(110),
        _snap("xp", 1.0, 1.1, -0.16),
        _snap("xc", 1.2, 1.3, 0.16),
    )
    assert round(c.probability_of_profit, 2) == 0.68  # 16Δ strangle


def _opt(strike):
    return SimpleNamespace(
        strike_price=Decimal(str(strike)),
        expiration_date=TODAY + timedelta(days=45),
        streamer_symbol=f"x{strike}",
    )


def _snap(sym, bid, ask, delta):
    return OptionSnapshot(
        streamer_symbol=sym, bid=Decimal(str(bid)), ask=Decimal(str(ask)), delta=delta
    )


def test_build_strangle_candidate_passes_guardrails():
    c = build_strangle_candidate(
        "SPY",
        100.0,
        0.45,
        45,
        _opt(90),
        _opt(110),
        _snap("xp", 1.0, 1.1, -0.16),
        _snap("xc", 1.2, 1.3, 0.15),
    )
    assert c is not None
    assert c.strategy is Strategy.SHORT_STRANGLE
    assert round(c.net_credit, 2) == 230.0  # (1.05 + 1.25) * 100
    assert c.max_loss == float("inf")
    assert c.legs[0].action is Action.SELL_TO_OPEN
    assert c.max_short_leg_delta == 0.16
    assert validate_candidate(c, PARAMS).ok


def test_build_returns_none_without_quotes():
    c = build_strangle_candidate(
        "SPY",
        100.0,
        0.45,
        45,
        _opt(90),
        _opt(110),
        OptionSnapshot("xp", None, None, -0.16),
        _snap("xc", 1.2, 1.3, 0.15),
    )
    assert c is None


def test_naked_put_candidate():
    from tastyagent.strategy.candidates import build_naked_put_candidate

    c = build_naked_put_candidate(
        "SPY", 100.0, 0.45, 45, _opt(95), _snap("x95", 0.98, 1.02, -0.16)
    )
    assert c.strategy is Strategy.NAKED_PUT
    assert round(c.net_credit, 2) == 100.0  # mid 1.00 * 100
    assert round(c.max_loss, 2) == 95 * 100 - 100  # strike notional - credit
    assert validate_candidate(c, PARAMS).ok


def test_put_credit_spread_is_defined_risk():
    from tastyagent.strategy.candidates import build_credit_spread_candidate
    from tastyagent.models import OptionType

    c = build_credit_spread_candidate(
        "SPY",
        100.0,
        0.45,
        45,
        _opt(95),
        _opt(90),
        _snap("xs", 2.08, 2.12, -0.24),
        _snap("xl", 0.39, 0.41, -0.10),
        option_type=OptionType.PUT,
        strategy=Strategy.PUT_CREDIT_SPREAD,
    )
    assert c.strategy is Strategy.PUT_CREDIT_SPREAD
    assert round(c.net_credit, 2) == 170.0  # (2.10 - 0.40) * 100
    assert round(c.max_loss, 2) == 330.0  # (5 width - 1.70) * 100
    assert c.buying_power_reduction == c.max_loss  # defined risk
    assert c.max_loss != float("inf")
    assert round(c.strike_width, 2) == 5.0
    assert round(c.credit_width_ratio, 3) == 0.340
    assert validate_candidate(c, PARAMS).ok


def test_credit_spread_violates_one_third_width_rule():
    from tastyagent.strategy.candidates import build_credit_spread_candidate
    from tastyagent.models import OptionType

    c = build_credit_spread_candidate(
        "SPY",
        100.0,
        0.45,
        45,
        _opt(95),
        _opt(90),
        _snap("xs", 0.98, 1.02, -0.24),  # 1.00 mid - 0.40 mid = 0.60 credit on 5.0 width (12%)
        _snap("xl", 0.39, 0.41, -0.10),
        option_type=OptionType.PUT,
        strategy=Strategy.PUT_CREDIT_SPREAD,
    )
    assert c is not None
    assert round(c.credit_width_ratio, 3) == 0.120
    res = validate_candidate(c, PARAMS)
    assert not res.ok
    assert any("1/3 rule" in v for v in res.violations)


def test_credit_spread_requires_a_credit():
    from tastyagent.strategy.candidates import build_credit_spread_candidate
    from tastyagent.models import OptionType

    c = build_credit_spread_candidate(
        "SPY",
        100.0,
        0.45,
        45,
        _opt(95),
        _opt(90),
        _snap("xs", 0.39, 0.41, -0.28),
        _snap("xl", 0.98, 1.02, -0.10),  # long worth more
        option_type=OptionType.PUT,
        strategy=Strategy.PUT_CREDIT_SPREAD,
    )
    assert c is None


def test_iron_condor_candidate():
    from tastyagent.strategy.candidates import build_iron_condor_candidate

    ps, pl, cs, cl = _opt(90), _opt(85), _opt(110), _opt(115)
    snaps = {
        ps.streamer_symbol: _snap(ps.streamer_symbol, 1.23, 1.27, -0.16),
        pl.streamer_symbol: _snap(pl.streamer_symbol, 0.39, 0.41, -0.07),
        cs.streamer_symbol: _snap(cs.streamer_symbol, 1.23, 1.27, 0.15),
        cl.streamer_symbol: _snap(cl.streamer_symbol, 0.39, 0.41, 0.07),
    }
    c = build_iron_condor_candidate("SPY", 100.0, 0.45, 45, ps, pl, cs, cl, snaps)
    assert c.strategy is Strategy.IRON_CONDOR
    assert len(c.legs) == 4
    assert round(c.net_credit, 2) == 170.0  # (1.25 + 1.25) - (0.40 + 0.40), *100
    assert round(c.max_loss, 2) == 330.0  # (5 width - 1.70) * 100
    assert c.buying_power_reduction == 330.0
    assert round(c.strike_width, 2) == 5.0
    assert round(c.credit_width_ratio, 3) == 0.340
    assert validate_candidate(c, PARAMS).ok
