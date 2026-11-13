"""Per-strategy ahead-of-pace profit-taking schedule.

TastyTrade research gives, per strategy, the *average days held* to reach each
profit milestone. We use these as an **ahead-of-pace exit trigger**: close a
winner the instant its current profit reaches a milestone AT OR FASTER than the
table's average-days for that milestone (reaching a target ahead of schedule is
the favorable signal to lock the win in).

This can only ever close a trade *earlier* than the standard backstops
(manage-at-50%, manage_dte) handled in ``exits.py`` — it never holds past them.
"""

from __future__ import annotations

from ..models import Strategy

# milestone (fraction of max profit) -> average days held to reach it.
# Ordered ascending by milestone. Start at 40% to avoid snipping tiny 20% wins
# on day 1-5 after slippage/commissions while still capturing fast IV crushes.
# Strategies without a table fall back to the backstops only.
PROFIT_SCHEDULES: dict[Strategy, tuple[tuple[float, int], ...]] = {
    Strategy.SHORT_STRANGLE: (
        (0.40, 10),
        (0.50, 19),
        (0.70, 28),
        (0.90, 35),
    ),
    Strategy.NAKED_PUT: (
        (0.40, 7),
        (0.50, 14),
        (0.70, 26),
        (0.90, 38),
    ),
    Strategy.NAKED_CALL: (
        (0.40, 7),
        (0.50, 14),
        (0.70, 28),
        (0.90, 43),
    ),
    # Straddles are generally avoided by TastyTrade; included only if enabled.
    Strategy.SHORT_STRADDLE: (
        (0.40, 29),
        (0.50, 35),
        (0.60, 42),
        # 70% milestone maps to "near expiration" -> handled by the manage_dte backstop.
    ),
}


def ahead_of_pace_milestone(
    strategy: Strategy, profit_pct: float, days_held: int
) -> float | None:
    """Return the highest milestone reached at or ahead of pace, else None.

    A milestone ``M`` qualifies when ``profit_pct >= M`` and the trade has been
    held no longer than the average days for ``M``. Returns the largest such
    ``M`` so the rationale can report the strongest signal.
    """
    schedule = PROFIT_SCHEDULES.get(strategy)
    if not schedule:
        return None
    best: float | None = None
    for milestone, avg_days in schedule:
        if profit_pct >= milestone and days_held <= avg_days:
            best = milestone if best is None else max(best, milestone)
    return best
