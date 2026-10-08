"""Environment / .env loading (depends on pydantic-settings).

Kept separate from ``config.py`` so the stdlib-only strategy/risk core never
imports a third-party package. Import this only from the application wiring
(API, scheduler, integration layer) — not from the deterministic core.
"""

from __future__ import annotations

import logging
import os
from dataclasses import fields
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field
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
    target_short_delta: float | None = None
    spread_long_delta: float | None = None
    min_credit_width_ratio: float | None = None
    max_bid_ask_width_pct: float | None = None
    min_open_interest: int | None = None
    min_daily_volume: int | None = None
    earnings_blackout_days: int | None = None
    universe_top_n: int | None = None
    take_profit_pct: float | None = None
    manage_dte: int | None = None
    tested_delta_threshold: float | None = None
    use_hard_stop: bool | None = None
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
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True
    )

    mode: TradingMode = Field(default=TradingMode.SANDBOX, alias="TASTYAGENT_MODE")
    api_host: str = Field(default="0.0.0.0", alias="TASTYAGENT_HOST")
    api_port: int = Field(default=3060, alias="TASTYAGENT_PORT")

    # IBKR Sandbox (Paper Account) Gateway (default 127.0.0.1:4002)
    ibkr_sandbox_host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices(
            "IBKR_SANDBOX_HOST", "IBKR_HOST", "ibkr_sandbox_host", "ibkr_host"
        ),
        description="Host for IBKR Sandbox paper trading gateway (default: 127.0.0.1)",
    )
    ibkr_sandbox_port: int = Field(
        default=4002,
        validation_alias=AliasChoices(
            "IBKR_SANDBOX_PORT", "IBKR_PORT", "ibkr_sandbox_port", "ibkr_port"
        ),
        description="Port for IBKR Sandbox paper trading gateway (default: 4002)",
    )
    ibkr_sandbox_client_id: int = Field(
        default=55,
        validation_alias=AliasChoices(
            "IBKR_SANDBOX_CLIENT_ID",
            "IBKR_CLIENT_ID",
            "ibkr_sandbox_client_id",
            "ibkr_client_id",
        ),
        description="Client ID for IBKR Sandbox paper trading gateway (default: 55)",
    )

    # IBKR Live (Real Account) Gateway (default 127.0.0.1:4001, also supplies real market data)
    ibkr_live_host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices(
            "IBKR_LIVE_HOST", "IBKR_DATA_HOST", "ibkr_live_host", "ibkr_data_host"
        ),
        description="Host for IBKR Live real account gateway & market data (default: 127.0.0.1)",
    )
    ibkr_live_port: int = Field(
        default=4001,
        validation_alias=AliasChoices(
            "IBKR_LIVE_PORT", "IBKR_DATA_PORT", "ibkr_live_port", "ibkr_data_port"
        ),
        description="Port for IBKR Live real account gateway & market data (default: 4001)",
    )
    ibkr_live_client_id: int = Field(
        default=56,
        validation_alias=AliasChoices(
            "IBKR_LIVE_CLIENT_ID",
            "IBKR_DATA_CLIENT_ID",
            "ibkr_live_client_id",
            "ibkr_data_client_id",
        ),
        description="Client ID for IBKR Live real account gateway (default: 56)",
    )

    ibkr_account: str = Field(
        default="",
        alias="IBKR_ACCOUNT",
        description="IBKR account ID for real-account live trading (e.g. U1234567). Ignored in sandbox mode.",
    )

    # Backward compatibility properties for existing call sites
    @property
    def ibkr_host(self) -> str:
        return self.ibkr_sandbox_host

    @ibkr_host.setter
    def ibkr_host(self, v: str) -> None:
        self.ibkr_sandbox_host = v

    @property
    def ibkr_port(self) -> int:
        return self.ibkr_sandbox_port

    @ibkr_port.setter
    def ibkr_port(self, v: int) -> None:
        self.ibkr_sandbox_port = v

    @property
    def ibkr_client_id(self) -> int:
        return self.ibkr_sandbox_client_id

    @ibkr_client_id.setter
    def ibkr_client_id(self, v: int) -> None:
        self.ibkr_sandbox_client_id = v

    @property
    def ibkr_data_host(self) -> str:
        return self.ibkr_live_host

    @ibkr_data_host.setter
    def ibkr_data_host(self, v: str) -> None:
        self.ibkr_live_host = v

    @property
    def ibkr_data_port(self) -> int:
        return self.ibkr_live_port

    @ibkr_data_port.setter
    def ibkr_data_port(self, v: int) -> None:
        self.ibkr_live_port = v

    @property
    def ibkr_data_client_id(self) -> int:
        return self.ibkr_live_client_id

    @ibkr_data_client_id.setter
    def ibkr_data_client_id(self, v: int) -> None:
        self.ibkr_live_client_id = v

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
    # Working capital sizing mode: custom working capital vs IBKR account Total Cash
    use_custom_working_capital: bool = Field(
        default=True,
        alias="TASTYAGENT_USE_CUSTOM_WORKING_CAPITAL",
        description="If True, size positions against custom working_capital. If False, size against IBKR Total Cash.",
    )
    working_capital: float = Field(default=10_000.0, alias="TASTYAGENT_WORKING_CAPITAL")

    # Scheduler settings
    scheduler_interval_seconds: float = Field(
        default=300.0,
        alias="TASTYAGENT_SCHEDULER_INTERVAL_SECONDS",
    )
    scheduler_market_hours_only: bool = Field(
        default=True,
        alias="TASTYAGENT_SCHEDULER_MARKET_HOURS_ONLY",
    )
    auto_start_scheduler: bool = Field(
        default=False,
        alias="TASTYAGENT_AUTO_START_SCHEDULER",
        description="Whether to automatically start the trading scheduler on startup",
    )

    def strategy_params(self) -> StrategyParams:
        return _merge(StrategyParams(), _StrategyEnv())

    def risk_limits(self) -> RiskLimits:
        return _merge(RiskLimits(), _RiskEnv())


def find_env_file() -> Path:
    """Resolve the active .env file location."""
    candidate = Path(__file__).resolve().parents[1] / ".env"
    if candidate.exists():
        return candidate
    cwd_candidate = Path(".env").resolve()
    if cwd_candidate.exists():
        return cwd_candidate
    from dotenv import find_dotenv

    found = find_dotenv()
    if found:
        return Path(found).resolve()
    return candidate


def find_frontend_env_file() -> Path:
    """Resolve the frontend .env.local file location."""
    candidate = Path(__file__).resolve().parents[2] / "frontend" / ".env.local"
    if candidate.exists():
        return candidate
    candidate_alt = Path(__file__).resolve().parents[1] / "frontend" / ".env.local"
    if candidate_alt.exists():
        return candidate_alt
    return candidate


def load_frontend_env() -> dict[str, str]:
    from dotenv import dotenv_values

    target = find_frontend_env_file()
    if target.exists():
        return {k: str(v) for k, v in dotenv_values(target).items() if v is not None}
    return {}


def persist_frontend_env(updates: dict[str, Any]) -> None:
    from dotenv import set_key

    target = find_frontend_env_file()
    logger = logging.getLogger("tastyagent.settings")
    try:
        if not target.exists() and target.parent.exists():
            target.touch()
        for k, v in updates.items():
            if v is not None:
                set_key(str(target), k, str(v), quote_mode="never")
        logger.info("Persisted %d frontend settings to %s", len(updates), target)
    except Exception as e:
        logger.warning("Failed to persist frontend settings to %s: %s", target, e)


def persist_settings_to_env(
    updates: dict[str, Any], env_file: Path | str | None = None
) -> None:
    """Synchronize modified dashboard settings back to the .env file."""
    from dotenv import set_key

    target = Path(env_file).resolve() if env_file else find_env_file()
    logger = logging.getLogger("tastyagent.settings")

    key_value_pairs: list[tuple[str, str]] = []

    if "mode" in updates and updates["mode"] is not None:
        key_value_pairs.append(("TASTYAGENT_MODE", str(updates["mode"]).lower()))

    if (
        "use_custom_working_capital" in updates
        and updates["use_custom_working_capital"] is not None
    ):
        key_value_pairs.append(
            (
                "TASTYAGENT_USE_CUSTOM_WORKING_CAPITAL",
                "true" if updates["use_custom_working_capital"] else "false",
            )
        )

    if "working_capital" in updates and updates["working_capital"] is not None:
        key_value_pairs.append(
            ("TASTYAGENT_WORKING_CAPITAL", str(updates["working_capital"]))
        )

    if (
        "scheduler_interval_seconds" in updates
        and updates["scheduler_interval_seconds"] is not None
    ):
        key_value_pairs.append(
            (
                "TASTYAGENT_SCHEDULER_INTERVAL_SECONDS",
                str(updates["scheduler_interval_seconds"]),
            )
        )

    if (
        "scheduler_market_hours_only" in updates
        and updates["scheduler_market_hours_only"] is not None
    ):
        key_value_pairs.append(
            (
                "TASTYAGENT_SCHEDULER_MARKET_HOURS_ONLY",
                "true" if updates["scheduler_market_hours_only"] else "false",
            )
        )

    if (
        "auto_start_scheduler" in updates
        and updates["auto_start_scheduler"] is not None
    ):
        key_value_pairs.append(
            (
                "TASTYAGENT_AUTO_START_SCHEDULER",
                "true" if updates["auto_start_scheduler"] else "false",
            )
        )

    strategy = updates.get("strategy")
    if isinstance(strategy, dict):
        for k, v in strategy.items():
            if v is not None:
                env_key = f"TASTYAGENT_{k.upper()}"
                env_val = "true" if v is True else ("false" if v is False else str(v))
                key_value_pairs.append((env_key, env_val))

    risk = updates.get("risk")
    if isinstance(risk, dict):
        for k, v in risk.items():
            if k == "kill_switch":
                continue
            if v is not None:
                env_key = f"TASTYAGENT_{k.upper()}"
                env_val = "true" if v is True else ("false" if v is False else str(v))
                key_value_pairs.append((env_key, env_val))

    ibkr = updates.get("ibkr")
    if isinstance(ibkr, dict):
        mapping: dict[str, list[str]] = {
            "sandbox_host": ["IBKR_SANDBOX_HOST", "IBKR_HOST"],
            "sandbox_port": ["IBKR_SANDBOX_PORT", "IBKR_PORT"],
            "sandbox_client_id": ["IBKR_SANDBOX_CLIENT_ID", "IBKR_CLIENT_ID"],
            "live_host": ["IBKR_LIVE_HOST", "IBKR_DATA_HOST"],
            "live_port": ["IBKR_LIVE_PORT", "IBKR_DATA_PORT"],
            "live_client_id": ["IBKR_LIVE_CLIENT_ID", "IBKR_DATA_CLIENT_ID"],
            "host": ["IBKR_SANDBOX_HOST", "IBKR_HOST"],
            "port": ["IBKR_SANDBOX_PORT", "IBKR_PORT"],
            "client_id": ["IBKR_SANDBOX_CLIENT_ID", "IBKR_CLIENT_ID"],
            "account": ["IBKR_ACCOUNT"],
            "data_host": ["IBKR_LIVE_HOST", "IBKR_DATA_HOST"],
            "data_port": ["IBKR_LIVE_PORT", "IBKR_DATA_PORT"],
            "data_client_id": ["IBKR_LIVE_CLIENT_ID", "IBKR_DATA_CLIENT_ID"],
            "scan_code": ["IBKR_SCAN_CODE"],
            "scan_rows": ["IBKR_SCAN_ROWS"],
            "walk_step": ["IBKR_WALK_STEP"],
            "walk_interval": ["IBKR_WALK_INTERVAL"],
            "attach_tp": ["IBKR_ATTACH_TP"],
            "tp_pct": ["IBKR_TP_PCT"],
        }
        for k, env_keys in mapping.items():
            if k in ibkr and ibkr[k] is not None:
                v = ibkr[k]
                env_val = "true" if v is True else ("false" if v is False else str(v))
                for env_k in env_keys:
                    key_value_pairs.append((env_k, env_val))

    llm = updates.get("llm")
    if isinstance(llm, dict):
        mapping = {
            "api_key": "OPENROUTER_API_KEY",
            "model": "OPENROUTER_MODEL",
            "base_url": "OPENROUTER_BASE_URL",
            "site_url": "OPENROUTER_SITE_URL",
            "app_name": "OPENROUTER_APP_NAME",
        }
        for k, env_k in mapping.items():
            if k in llm and llm[k] is not None:
                v = str(llm[k]).strip()
                if k == "api_key" and ("••••" in v or "..." in v):
                    continue
                key_value_pairs.append((env_k, v))

    system = updates.get("system")
    if isinstance(system, dict):
        if "api_host" in system and system["api_host"] is not None:
            key_value_pairs.append(("TASTYAGENT_HOST", str(system["api_host"])))
        if "api_port" in system and system["api_port"] is not None:
            key_value_pairs.append(("TASTYAGENT_PORT", str(system["api_port"])))
        if (
            "auto_start_scheduler" in system
            and system["auto_start_scheduler"] is not None
        ):
            key_value_pairs.append(
                (
                    "TASTYAGENT_AUTO_START_SCHEDULER",
                    "true" if system["auto_start_scheduler"] else "false",
                )
            )

        fe_updates: dict[str, Any] = {}
        if "frontend_port" in system and system["frontend_port"] is not None:
            fe_updates["PORT"] = system["frontend_port"]
        if "frontend_api_base" in system and system["frontend_api_base"] is not None:
            fe_updates["NEXT_PUBLIC_API_BASE"] = system["frontend_api_base"]
        if fe_updates:
            persist_frontend_env(fe_updates)

    if not key_value_pairs:
        return

    try:
        if not target.exists() and target.parent.exists():
            target.touch()
        for k, v in key_value_pairs:
            set_key(str(target), k, v, quote_mode="never")
            os.environ[k] = v
        logger.info("Persisted %d settings to %s", len(key_value_pairs), target)
    except Exception as e:
        logger.warning("Failed to persist settings to %s: %s", target, e)


def load_settings() -> Settings:
    return Settings()

