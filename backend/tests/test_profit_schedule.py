from tastyagent.models import Strategy
from tastyagent.strategy.profit_schedule import ahead_of_pace_milestone


def test_strangle_hits_milestone_ahead_of_pace():
    # 40% profit by day 8 (avg for 40% is 10) -> qualifies
    assert ahead_of_pace_milestone(Strategy.SHORT_STRANGLE, 0.40, 8) == 0.40


def test_strangle_behind_pace_does_not_trigger():
    # 40% profit but at day 12 (> avg 10) and not yet at 50% -> no trigger
    assert ahead_of_pace_milestone(Strategy.SHORT_STRANGLE, 0.40, 12) is None


def test_returns_highest_qualifying_milestone():
    # 90% by day 20: qualifies for 50% (<=19? no, day 20>19) but 70%(<=28) and 90%(<=35)
    assert ahead_of_pace_milestone(Strategy.SHORT_STRANGLE, 0.90, 20) == 0.90


def test_naked_call_differs_from_put_at_high_milestones():
    # 70% at day 27: naked put avg is 26 (behind), naked call avg is 28 (ahead)
    assert ahead_of_pace_milestone(Strategy.NAKED_PUT, 0.70, 27) is None
    assert ahead_of_pace_milestone(Strategy.NAKED_CALL, 0.70, 27) == 0.70


def test_below_first_milestone_returns_none():
    assert ahead_of_pace_milestone(Strategy.SHORT_STRANGLE, 0.10, 3) is None
    # 20% early micro-exit was removed so fast winners aren't snipped too early
    assert ahead_of_pace_milestone(Strategy.SHORT_STRANGLE, 0.23, 1) is None
    assert ahead_of_pace_milestone(Strategy.PUT_CREDIT_SPREAD, 0.23, 1) is None


def test_strategy_without_schedule_returns_none():
    assert ahead_of_pace_milestone(Strategy.IRON_CONDOR, 0.90, 1) is None
