from dataclasses import replace
from datetime import date, timedelta

from tastyagent.config import StrategyParams
from tastyagent.models import OpenPosition, Strategy
from tastyagent.strategy.exits import ExitAction, RollKind, evaluate_exit

P = StrategyParams()
TODAY = date(2026, 3, 1)


def pos(*, entry_credit=250.0, cost=250.0, days_held=10, dte_remaining=40, delta=None):
    return OpenPosition(
        symbol="SPY",
        strategy=Strategy.SHORT_STRANGLE,
        legs=(),
        entry_date=TODAY - timedelta(days=days_held),
        entry_credit=entry_credit,
        current_cost_to_close=cost,
        buying_power_reduction=2000.0,
        dte_remaining=dte_remaining,
        as_of=TODAY,
        current_max_short_delta=delta,
    )


def test_hold_when_nothing_triggers():
    d = evaluate_exit(pos(cost=230.0, days_held=10, dte_remaining=40, delta=0.18), P)
    assert d.action is ExitAction.HOLD


def test_take_profit_at_fifty():
    d = evaluate_exit(pos(cost=125.0, dte_remaining=40), P)
    assert d.action is ExitAction.CLOSE
    assert "max-profit" in d.reason


def test_ahead_of_pace_closes():
    d = evaluate_exit(pos(cost=150.0, days_held=8, dte_remaining=40), P)  # 40% by day 8
    assert d.action is ExitAction.CLOSE
    assert "ahead of pace" in d.reason


def test_roll_out_at_21_dte():
    d = evaluate_exit(
        pos(cost=200.0, dte_remaining=21, days_held=30), P
    )  # 20% profit, at DTE
    assert d.action is ExitAction.ROLL
    assert d.roll_kind is RollKind.OUT


def test_profit_takes_precedence_over_dte():
    d = evaluate_exit(
        pos(cost=125.0, dte_remaining=21), P
    )  # 50% AND at DTE -> take profit
    assert d.action is ExitAction.CLOSE


def test_roll_untested_when_tested():
    d = evaluate_exit(
        pos(cost=300.0, dte_remaining=40, days_held=12, delta=0.50), P
    )  # losing, tested (0.50 >= 0.45)
    assert d.action is ExitAction.ROLL
    assert d.roll_kind is RollKind.UNTESTED


def test_below_tested_threshold_holds():
    d = evaluate_exit(pos(cost=300.0, dte_remaining=40, days_held=12, delta=0.35), P)
    assert d.action is ExitAction.HOLD


def test_hard_stop_off_by_default():
    # big loss but stop disabled and not tested -> hold (tastytrade default)
    d = evaluate_exit(
        pos(entry_credit=250.0, cost=800.0, dte_remaining=40, delta=0.20), P
    )
    assert d.action is ExitAction.HOLD


def test_hard_stop_when_enabled():
    params = replace(P, use_hard_stop=True)
    d = evaluate_exit(
        pos(entry_credit=250.0, cost=800.0, dte_remaining=40, delta=0.20), params
    )
    assert d.action is ExitAction.CLOSE
    assert "stop" in d.reason
