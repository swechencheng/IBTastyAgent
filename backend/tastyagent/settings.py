"""Environment / .env loading (depends on pydantic-settings).

Kept separate from ``config.py`` so the stdlib-only strategy/risk core never
imports a third-party package. Import this only from the application wiring
(API, scheduler, integration layer) — not from the deterministic core.
"""

from __future__ import annotations

from dataclasses import fields

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .config import RiskLimits, StrategyParams, TradingMode


class _StrategyEnv(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TASTYAGENT_", env_file=".env", extra="ignore"
    )
    min_iv_rank: float | None = None
    min_dte: int | None = None
    max_dte: int | None = None
    target_dte: int | None = None
    max_short_leg_delta: float | None = None
    max_bid_ask_width_pct: float | None = None
    min_open_interest: int | None = None
    min_daily_volume: int | None = None
    earnings_blackout_days: int | None = None
    take_profit_pct: float | None = None
    manage_dte: int | None = None
    stop_loss_multiple: float | None = None


class _RiskEnv(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TASTYAGENT_", env_file=".env", extra="ignore"
    )
    max_trade_bp_pct: float | None = None
    max_total_bp_pct: float | None = None
    max_positions: int | None = None
    max_positions_per_symbol: int | None = None
    max_daily_loss_pct: float | None = None
    consecutive_loss_halt: int | None = None
    kill_switch: bool | None = None


def _merge(defaults, env_model):
    """Overlay any non-None env values onto a frozen dataclass instance."""
    overrides = {
        f.name: getattr(env_model, f.name)
        for f in fields(defaults)
        if getattr(env_model, f.name, None) is not None
    }
    return type(defaults)(**{**defaults.__dict__, **overrides})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mode: TradingMode = Field(default=TradingMode.SANDBOX, alias="TASTYAGENT_MODE")
    api_host: str = Field(default="0.0.0.0", alias="TASTYAGENT_HOST")
    api_port: int = Field(default=3060, alias="TASTYAGENT_PORT")

    # IBKR Connection & Trading (default to local gateway / TWS)
    ibkr_host: str = Field(default="127.0.0.1", alias="IBKR_HOST")
    ibkr_port: int = Field(default=4002, alias="IBKR_PORT")
    ibkr_client_id: int = Field(default=55, alias="IBKR_CLIENT_ID")
    ibkr_account: str = Field(
        default="",
        alias="IBKR_ACCOUNT",
        description="IBKR account ID for real-account live trading (e.g. U1234567). Ignored in sandbox mode.",
    )

    # IBKR Real-time Market Data Connection (dual-gateway support)
    ibkr_data_host: str = Field(default="127.0.0.1", alias="IBKR_DATA_HOST")
    ibkr_data_port: int = Field(default=4001, alias="IBKR_DATA_PORT")
    ibkr_data_client_id: int = Field(default=56, alias="IBKR_DATA_CLIENT_ID")

    # IBKR Market Scanner & Execution
    ibkr_scan_code: str = Field(
        default="OPT_VOLUME_MOST_ACTIVE", alias="IBKR_SCAN_CODE"
    )
    ibkr_scan_rows: int = Field(default=25, alias="IBKR_SCAN_ROWS")
    ibkr_walk_step: float = Field(default=0.01, alias="IBKR_WALK_STEP")
    ibkr_walk_interval: int = Field(default=5, alias="IBKR_WALK_INTERVAL")
    ibkr_attach_tp: bool = Field(default=True, alias="IBKR_ATTACH_TP")
    ibkr_tp_pct: float = Field(default=0.50, alias="IBKR_TP_PCT")
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(
        default="deepseek/deepseek-v4.1-flash", alias="OPENROUTER_MODEL"
    )
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL"
    )
    openrouter_site_url: str = Field(
        default="https://github.com/swechencheng/IBTastyAgent",
        alias="OPENROUTER_SITE_URL",
    )
    openrouter_app_name: str = Field(
        default="IBTastyAgent", alias="OPENROUTER_APP_NAME"
    )
    # Capital the agent sizes against. The sandbox account seeds at ~$1M with no
    # withdrawal endpoint, so the agent simulates this working capital instead.
    working_capital: float = Field(default=10_000.0, alias="TASTYAGENT_WORKING_CAPITAL")

    def strategy_params(self) -> StrategyParams:
        return _merge(StrategyParams(), _StrategyEnv())

    def risk_limits(self) -> RiskLimits:
        return _merge(RiskLimits(), _RiskEnv())


def load_settings() -> Settings:
    return Settings()
