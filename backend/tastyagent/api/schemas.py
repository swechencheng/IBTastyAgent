"""Pydantic response models for the dashboard API."""

from __future__ import annotations

from datetime import date, datetime, timezone

from pydantic import BaseModel, ConfigDict, field_serializer


def _iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class LegOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    option_type: str
    strike: float
    expiration: date
    action: str
    quantity: int
    delta: float


class TradeEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    ts: datetime
    kind: str
    detail: str

    @field_serializer("ts", when_used="json")
    def serialize_ts(self, v: datetime) -> str | None:
        return _iso_utc(v)


class TradeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    symbol: str
    strategy: str
    contracts: int
    status: str
    mode: str
    rationale: str
    entry_credit: float
    buying_power: float
    dte_at_entry: int
    current_cost_to_close: float
    unrealized_pnl: float
    probability_of_profit: float
    realized_pnl: float | None
    exit_reason: str | None
    is_win: bool | None
    opened_at: datetime | None
    closed_at: datetime | None
    created_at: datetime | None = None
    legs: list[LegOut]
    events: list[TradeEventOut] = []

    @field_serializer("opened_at", "closed_at", "created_at", when_used="json")
    def serialize_dts(self, v: datetime | None) -> str | None:
        return _iso_utc(v)


class PnLOut(BaseModel):
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    open_count: int
    closed_count: int
    wins: int
    losses: int
    win_rate: float | None
    profit_pct: float | None
    starting_capital: float


class ConnectionStatusOut(BaseModel):
    real_connected: bool
    paper_connected: bool
    status: str  # "connected" | "warning" | "disconnected"
    detail: str


class StatusOut(BaseModel):
    mode: str
    kill_switch: bool
    market_open: bool
    starting_capital: float
    requires_approval: bool
    scheduler_running: bool = False
    connection: ConnectionStatusOut | None = None


class BenchmarkPoint(BaseModel):
    date: date
    value: float


class BenchmarkOut(BaseModel):
    strategy_return_pct: float
    sp500_return_pct: float
    outperformance_pct: float | None = None
    strategy_curve: list[BenchmarkPoint]
    sp500_curve: list[BenchmarkPoint]
    use_custom_working_capital: bool = True


class ActionResult(BaseModel):
    trade_id: int
    symbol: str
    action: str
    detail: str = ""


class ModeRequest(BaseModel):
    mode: str


class KillSwitchRequest(BaseModel):
    engaged: bool


class WatchlistItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    symbol: str
    enabled: bool
    source: str


class WatchlistAdd(BaseModel):
    symbol: str


class WatchlistImport(BaseModel):
    symbols: list[str]
    source: str = "imported"


class ToggleRequest(BaseModel):
    enabled: bool


class RankedSymbol(BaseModel):
    symbol: str
    iv_rank: float | None
    iv_percentile: float | None
    liquidity_rating: int | None


class TastytradeWatchlist(BaseModel):
    name: str
    group: str | None = None
    symbols: list[str]


class SchedulerConfig(BaseModel):
    interval_seconds: float
    market_hours_only: bool


class IBKRConfig(BaseModel):
    # Sandbox (Paper Account) Gateway
    sandbox_host: str = "127.0.0.1"
    sandbox_port: int = 4002
    sandbox_client_id: int = 55
    # Live (Real Account) Gateway
    live_host: str = "127.0.0.1"
    live_port: int = 4001
    live_client_id: int = 56
    account: str = ""
    # Legacy aliases (backward compatibility)
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 55
    data_host: str = "127.0.0.1"
    data_port: int = 4001
    data_client_id: int = 56
    # Scanner & Execution
    scan_code: str = "OPT_VOLUME_MOST_ACTIVE"
    scan_rows: int = 25
    walk_step: float = 0.01
    walk_interval: int = 5
    attach_tp: bool = True
    tp_pct: float = 0.50


class LLMConfig(BaseModel):
    api_key: str = ""
    model: str = "deepseek/deepseek-v4.1-flash"
    base_url: str = "https://openrouter.ai/api/v1"
    site_url: str = "https://github.com/swechencheng/IBTastyAgent"
    app_name: str = "IBTastyAgent"


class SystemConfig(BaseModel):
    api_host: str = "0.0.0.0"
    api_port: int = 3060
    frontend_port: int = 3066
    frontend_api_base: str = "http://localhost:3060"
    auto_start_scheduler: bool = False


class SettingsOut(BaseModel):
    mode: str
    kill_switch: bool
    use_custom_working_capital: bool = True
    working_capital: float
    account_cash_usd: float | None = None
    account_cash_base: float | None = None
    account_base_currency: str | None = None
    account_cushion: float | None = None
    scheduler: SchedulerConfig
    strategy: dict  # StrategyParams fields
    risk: dict  # RiskLimits fields (excluding kill_switch, which is its own toggle)
    ibkr: IBKRConfig = IBKRConfig()
    llm: LLMConfig = LLMConfig()
    system: SystemConfig = SystemConfig()


class SettingsUpdate(BaseModel):
    """Partial update — send only the groups/fields that changed."""

    use_custom_working_capital: bool | None = None
    working_capital: float | None = None
    scheduler_interval_seconds: float | None = None
    scheduler_market_hours_only: bool | None = None
    auto_start_scheduler: bool | None = None
    strategy: dict | None = None
    risk: dict | None = None
    ibkr: dict | None = None
    llm: dict | None = None
    system: dict | None = None


class EventFeedItem(BaseModel):
    """A trade lifecycle event with its symbol — drives live toast/desktop pushes."""

    id: int
    ts: datetime
    trade_id: int
    symbol: str
    strategy: str
    kind: str
    detail: str

    @field_serializer("ts", when_used="json")
    def serialize_ts(self, v: datetime) -> str | None:
        return _iso_utc(v)


class ActivityTrade(BaseModel):
    """One trade row inside a decision-cycle breakdown table."""

    symbol: str
    strategy: str
    contracts: int
    credit: float
    pop: float
    status: str
    detail: str = ""  # rejection reason / exit reason
    realized_pnl: float | None = None


class ReasoningItem(BaseModel):
    """A per-ticker bullet of LLM reasoning for a cycle."""

    symbol: str
    text: str
    tone: str  # placed | rejected | managed


class ActivityItem(BaseModel):
    id: int
    created_at: datetime
    mode: str
    commentary: str
    considered: int
    placed: int
    rejected: int
    managed: int
    symbols: list[str]
    reasoning: list[ReasoningItem] = []
    planned_trades: list[ActivityTrade] = []
    placed_trades: list[ActivityTrade] = []
    rejected_trades: list[ActivityTrade] = []
    managed_trades: list[ActivityTrade] = []

    @field_serializer("created_at", when_used="json")
    def serialize_created_at(self, v: datetime) -> str | None:
        return _iso_utc(v)


class ResetSandboxOut(BaseModel):
    status: str
    message: str
    trades_deleted: int
    decisions_deleted: int
    orders_cancelled: int
