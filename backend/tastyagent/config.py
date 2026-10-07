"""Configuration: trading mode, strategy parameters, and risk limits.

All tunable thresholds live here so the *rules* stay fixed while their *parameters*
can adapt and be backtested. This module is intentionally stdlib-only so the
safety-critical strategy/risk core is importable and testable without any
third-party dependencies. Environment/.env loading lives in ``settings.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TradingMode(str, Enum):
    """Lifecycle stages. Promotion between them is manual and recorded."""

    BACKTEST = "backtest"
    SANDBOX = "sandbox"
    LIVE_APPROVAL = "live_approval"  # one-click approval required per order
    LIVE_AUTO = "live_auto"  # opt-in autonomous live trading

    @property
    def is_live(self) -> bool:
        return self in (TradingMode.LIVE_APPROVAL, TradingMode.LIVE_AUTO)

    @property
    def requires_approval(self) -> bool:
        """Live-approval mode gates every order behind a user click."""
        return self is TradingMode.LIVE_APPROVAL

    @property
    def uses_cert_environment(self) -> bool:
        """Sandbox uses TastyTrade's cert (paper) environment."""
        return self is TradingMode.SANDBOX


@dataclass(frozen=True)
class StrategyParams:
    """Entry/exit thresholds encoding TastyTrade mechanics."""

    # --- Entry guardrails ---
    min_iv_rank: float = 0.30  # only sell premium when IV rank is elevated
    min_dte: int = 30
    max_dte: int = 55
    target_dte: int = 45
    max_short_leg_delta: float = 0.25  # abs delta cap on any short leg (~16-25 delta)
    target_short_delta: float = 0.24  # delta we aim for when choosing short strikes
    spread_long_delta: float = (
        0.07  # delta for protective long wings (spreads / condors)
    )
    # Liquidity filters
    max_bid_ask_width_pct: float = 0.10  # width / mid
    min_open_interest: int = 500
    min_daily_volume: int = 100
    earnings_blackout_days: int = 7  # avoid opening within N days of earnings
    universe_top_n: int = 15  # only do chain/greeks work on the top-N highest-IVR names

    # --- Exit / management ---
    take_profit_pct: float = 0.50  # manage winners at 50% of max profit
    manage_dte: int = 21  # roll out at 21 DTE
    tested_delta_threshold: float = 0.45  # short-leg |delta| that counts as "tested"
    use_hard_stop: bool = (
        False  # tastytrade leans on management, not stops; off by default
    )
    stop_loss_multiple: float = 2.0  # if use_hard_stop: close at N x credit loss

    def __post_init__(self):
        if self.target_short_delta > self.max_short_leg_delta:
            raise ValueError(
                f"target_short_delta ({self.target_short_delta}) cannot exceed "
                f"max_short_leg_delta ({self.max_short_leg_delta})"
            )
        if self.max_short_leg_delta >= self.tested_delta_threshold:
            raise ValueError(
                f"max_short_leg_delta ({self.max_short_leg_delta}) must be strictly "
                f"less than tested_delta_threshold ({self.tested_delta_threshold})"
            )


@dataclass(frozen=True)
class RiskLimits:
    """Portfolio-level safety rails enforced after strategy selection."""

    max_trade_bp_pct: float = 0.25  # max buying-power reduction per trade vs net liq
    # ^ 25% at $10k = $2,500/trade. 5% was too tight for defined-risk spreads at this
    #   working capital (most got rejected on sizing); tune live via PUT /api/settings.
    max_total_bp_pct: float = 0.40  # max aggregate BP usage vs net liq
    max_positions: int = 15
    max_positions_per_symbol: int = 2
    max_daily_loss_pct: float = 0.03  # halt new entries after this daily drawdown
    consecutive_loss_halt: int = 5  # halt after N losing trades in a row
    kill_switch: bool = False  # hard stop on all new orders
