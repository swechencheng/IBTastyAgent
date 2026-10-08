"""In-memory runtime state for the API: mode, kill switch, starting capital, placer."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable

from ..config import RiskLimits, StrategyParams, TradingMode
from ..db.models import Trade

Placer = Callable[[Trade], Awaitable[str]]


@dataclass
class Runtime:
    mode: TradingMode = TradingMode.SANDBOX
    use_custom_working_capital: bool = True
    starting_capital: float = 10_000.0  # working capital the agent sizes against
    kill_switch: bool = False
    strategy: StrategyParams = field(default_factory=StrategyParams)
    risk: RiskLimits = field(default_factory=RiskLimits)
    scheduler_interval_seconds: float = 300.0
    scheduler_market_hours_only: bool = True
    scheduler_open_delay_minutes: int = 15
    auto_start_scheduler: bool = False
    ibkr_walk_step: float = 0.01
    ibkr_walk_interval: int = 5
    ibkr_attach_tp: bool = True
    ibkr_tp_pct: float = 0.50
    settings: Any | None = None
    placer: Placer | None = None  # set when a live/sandbox broker adapter is wired

    def risk_limits(self) -> RiskLimits:
        """Effective risk limits, reflecting the live kill-switch toggle."""
        return replace(self.risk, kill_switch=self.kill_switch)
