"""Exit & defense decisions — manage winners AND losers the tastytrade way.

Decision order (first match wins):
  1. Take profit   -> CLOSE   (50% max profit, or an ahead-of-pace milestone)
  2. 21-DTE        -> ROLL out (close the tested cycle, reopen ~45 DTE for a credit)
  3. Tested        -> ROLL the untested side toward the money for a credit (basis reduction)
  4. Hard stop     -> CLOSE   (only if enabled; off by default — tastytrade manages, not stops)
  5. otherwise     -> HOLD

Profit-taking precedes defense: a winner sitting at 21 DTE is closed for the gain, not rolled.
The roll *execution* lives in ``execution/exit_manager.py`` (broker actions injected); this module
only decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..config import StrategyParams
from ..models import OpenPosition
from .profit_schedule import ahead_of_pace_milestone


class ExitAction(str, Enum):
    HOLD = "hold"
    CLOSE = "close"
    ROLL = "roll"


class RollKind(str, Enum):
    OUT = "out"  # roll out in time at 21 DTE
    UNTESTED = "untested"  # roll the untested side toward the money


@dataclass(frozen=True)
class ExitDecision:
    action: ExitAction
    reason: str
    roll_kind: RollKind | None = None

    @property
    def should_act(self) -> bool:
        return self.action is not ExitAction.HOLD


def evaluate_exit(position: OpenPosition, params: StrategyParams) -> ExitDecision:
    profit_pct = position.profit_pct
    days_held = position.days_held

    # 1. Take profit first — lock in winners even if they're near 21 DTE.
    target_profit_pct = params.take_profit_pct
    target_desc = f"{params.take_profit_pct:.0%} max-profit target"

    if (
        position.strategy.is_defined_risk
        and position.strike_width is not None
        and position.strike_width > 0
        and position.entry_credit > 0
    ):
        width_profit_dollars = (
            position.strike_width
            * params.min_credit_width_ratio
            * 100.0
            * position.contracts
        )
        credit_profit_dollars = params.take_profit_pct * position.entry_credit
        # If 33% strike width target > 50% net credit, take 50% net credit.
        # Otherwise (33% strike width target <= 50% net credit), take 33% strike width.
        if width_profit_dollars < credit_profit_dollars:
            target_profit_pct = width_profit_dollars / position.entry_credit
            target_desc = (
                f"1/3 width profit target (${width_profit_dollars:.2f}, "
                f"{target_profit_pct:.0%} of credit)"
            )

    if profit_pct >= target_profit_pct:
        return ExitDecision(
            ExitAction.CLOSE,
            f"profit {profit_pct:.0%} >= {target_desc}",
        )
    milestone = ahead_of_pace_milestone(position.strategy, profit_pct, days_held)
    if milestone is not None:
        return ExitDecision(
            ExitAction.CLOSE,
            f"profit {profit_pct:.0%} hit {milestone:.0%} milestone by day {days_held} "
            "(ahead of pace)",
        )

    # 2. 21-DTE management — roll out to the next cycle for a credit.
    if position.dte_remaining <= params.manage_dte:
        return ExitDecision(
            ExitAction.ROLL,
            f"{position.dte_remaining} DTE <= {params.manage_dte}: roll out to next cycle",
            roll_kind=RollKind.OUT,
        )

    # 3. Tested before 21 DTE — roll the untested side in for a credit (defend the basis).
    if (
        position.current_max_short_delta is not None
        and position.current_max_short_delta >= params.tested_delta_threshold
    ):
        return ExitDecision(
            ExitAction.ROLL,
            f"short-leg delta {position.current_max_short_delta:.2f} >= "
            f"{params.tested_delta_threshold:.2f}: roll untested side in",
            roll_kind=RollKind.UNTESTED,
        )

    # 4. Optional hard stop (off by default).
    if params.use_hard_stop and profit_pct <= -params.stop_loss_multiple:
        return ExitDecision(
            ExitAction.CLOSE,
            f"loss {profit_pct:.0%} of credit <= -{params.stop_loss_multiple:.0f}x stop",
        )

    return ExitDecision(ExitAction.HOLD, "no exit or defense trigger")
