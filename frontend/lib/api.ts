export function getApiBase(): string {
  if (process.env.NEXT_PUBLIC_API_BASE) {
    return process.env.NEXT_PUBLIC_API_BASE;
  }
  if (typeof window !== "undefined" && window.location.hostname) {
    return `${window.location.protocol}//${window.location.hostname}:3060`;
  }
  return "http://localhost:3060";
}

export const API_BASE = getApiBase();

export interface ConnectionStatus {
  real_connected: boolean;
  paper_connected: boolean;
  status: "connected" | "warning" | "disconnected" | string;
  detail: string;
}

export interface Status {
  mode: string;
  kill_switch: boolean;
  market_open: boolean;
  starting_capital: number;
  requires_approval: boolean;
  scheduler_running: boolean;
  connection?: ConnectionStatus | null;
}

export interface Pnl {
  realized_pnl: number;
  unrealized_pnl: number;
  total_pnl: number;
  open_count: number;
  closed_count: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  profit_pct: number | null;
  starting_capital: number;
}

export interface Leg {
  option_type: string;
  strike: number;
  expiration: string;
  action: string;
  quantity: number;
  delta: number;
}

export interface TradeEvent {
  ts: string;
  kind: string;
  detail: string;
}

export interface Trade {
  id: number;
  symbol: string;
  strategy: string;
  contracts: number;
  status: string;
  mode: string;
  rationale: string;
  entry_credit: number;
  buying_power: number;
  dte_at_entry: number;
  current_cost_to_close: number;
  unrealized_pnl: number;
  probability_of_profit: number;
  realized_pnl: number | null;
  exit_reason: string | null;
  is_win: boolean | null;
  opened_at: string | null;
  closed_at: string | null;
  created_at: string | null;
  legs: Leg[];
  events: TradeEvent[];
}

export interface EventFeedItem {
  id: number;
  ts: string;
  trade_id: number;
  symbol: string;
  strategy: string;
  kind: string;
  detail: string;
}

export interface BenchmarkPoint {
  date: string;
  value: number;
}

export interface Benchmark {
  strategy_return_pct: number;
  sp500_return_pct: number;
  outperformance_pct: number | null;
  strategy_curve: BenchmarkPoint[];
  sp500_curve: BenchmarkPoint[];
  use_custom_working_capital?: boolean;
}

export const fetcher = (path: string) =>
  fetch(`${getApiBase()}${path}`).then((r) => {
    if (!r.ok) throw new Error(`${path}: ${r.status}`);
    return r.json();
  });

async function post(path: string, body?: unknown) {
  const r = await fetch(`${getApiBase()}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

export const setMode = (mode: string) => post("/api/mode", { mode });
export const setKillSwitch = (engaged: boolean) =>
  post("/api/kill-switch", { engaged });
export const approveTrade = (id: number) => post(`/api/approvals/${id}/approve`);
export const rejectTrade = (id: number) => post(`/api/approvals/${id}/reject`);
export interface WatchlistItem {
  symbol: string;
  enabled: boolean;
  source: string;
}

export interface RankedSymbol {
  symbol: string;
  iv_rank: number | null;
  iv_percentile: number | null;
  liquidity_rating: number | null;
}

export interface ScannerWatchlist {
  name: string;
  group: string | null;
  symbols: string[];
}

export type TastytradeWatchlist = ScannerWatchlist;

export const addToWatchlist = (symbol: string) => post("/api/watchlist", { symbol });
export const removeFromWatchlist = (symbol: string) =>
  fetch(`${getApiBase()}/api/watchlist/${encodeURIComponent(symbol)}`, { method: "DELETE" });
export const toggleWatchlist = (symbol: string, enabled: boolean) =>
  post(`/api/watchlist/${encodeURIComponent(symbol)}/toggle`, { enabled });
export const importWatchlist = (symbols: string[], source: string) =>
  post("/api/watchlist/import", { symbols, source });

export const runCycle = () => post("/api/cycle/run");
export const startScheduler = (interval_seconds?: number, market_hours_only?: boolean) => {
  const body: Record<string, unknown> = {};
  if (interval_seconds !== undefined) body.interval_seconds = interval_seconds;
  if (market_hours_only !== undefined) body.market_hours_only = market_hours_only;
  return post("/api/scheduler/start", Object.keys(body).length > 0 ? body : undefined);
};
export const stopScheduler = () => post("/api/scheduler/stop");

// ---- Settings -------------------------------------------------------------
export interface SchedulerConfig {
  interval_seconds: number;
  market_hours_only: boolean;
}

export interface IBKRConfig {
  sandbox_host?: string;
  sandbox_port?: number;
  sandbox_client_id?: number;
  live_host?: string;
  live_port?: number;
  live_client_id?: number;
  host: string;
  port: number;
  client_id: number;
  account: string;
  data_host: string;
  data_port: number;
  data_client_id: number;
  scan_code: string;
  scan_rows: number;
  walk_step: number;
  walk_interval: number;
  attach_tp: boolean;
  tp_pct: number;
}

export interface LLMConfig {
  api_key: string;
  model: string;
  base_url: string;
  site_url: string;
  app_name: string;
}

export interface SystemConfig {
  api_host: string;
  api_port: number;
  frontend_port: number;
  frontend_api_base: string;
  auto_start_scheduler: boolean;
}

export interface Settings {
  mode: string;
  kill_switch: boolean;
  use_custom_working_capital: boolean;
  working_capital: number;
  account_cash_usd?: number | null;
  account_cash_base?: number | null;
  account_base_currency?: string | null;
  account_cushion?: number | null;
  scheduler: SchedulerConfig;
  strategy: Record<string, number | boolean>;
  risk: Record<string, number | boolean>;
  auto_start_scheduler?: boolean;
  ibkr: IBKRConfig;
  llm: LLMConfig;
  system: SystemConfig;
}

export interface SettingsUpdate {
  use_custom_working_capital?: boolean;
  working_capital?: number;
  scheduler_interval_seconds?: number;
  scheduler_market_hours_only?: boolean;
  auto_start_scheduler?: boolean;
  strategy?: Record<string, number | boolean>;
  risk?: Record<string, number | boolean>;
  ibkr?: Partial<IBKRConfig>;
  llm?: Partial<LLMConfig>;
  system?: Partial<SystemConfig>;
}

export async function putSettings(body: SettingsUpdate): Promise<Settings> {
  const r = await fetch(`${getApiBase()}/api/settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`PUT /api/settings: ${r.status}`);
  return r.json();
}

export interface PnlSyncResult {
  reconciled: number;
  marks_updated: number;
  unrealized_pnl: number;
  realized_pnl: number;
  error?: string;
}

export async function syncPnl(): Promise<PnlSyncResult> {
  const r = await fetch(`${getApiBase()}/api/pnl/sync`, { method: "POST" });
  if (!r.ok) throw new Error(`POST /api/pnl/sync: ${r.status}`);
  return r.json();
}

export interface ResetSandboxResult {
  status: string;
  message: string;
  trades_deleted: number;
  decisions_deleted: number;
  orders_cancelled: number;
}

export async function resetSandbox(): Promise<ResetSandboxResult> {
  const r = await fetch(`${getApiBase()}/api/sandbox/reset`, { method: "POST" });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || `POST /api/sandbox/reset: ${r.status}`);
  }
  return r.json();
}

// ---- Activity -------------------------------------------------------------
export interface ActivityTrade {
  symbol: string;
  strategy: string;
  contracts: number;
  credit: number;
  pop: number;
  status: string;
  detail: string;
  realized_pnl: number | null;
}

export interface ReasoningItem {
  symbol: string;
  text: string;
  tone: "placed" | "rejected" | "managed";
}

export interface ActivityItem {
  id: number;
  created_at: string;
  mode: string;
  commentary: string;
  considered: number;
  placed: number;
  rejected: number;
  managed: number;
  symbols: string[];
  reasoning: ReasoningItem[];
  planned_trades: ActivityTrade[];
  placed_trades: ActivityTrade[];
  rejected_trades: ActivityTrade[];
  managed_trades: ActivityTrade[];
}

// ---- Formatters -----------------------------------------------------------
/** Whole-dollar, unsigned magnitude with $ — e.g. $10,000 */
export const fmtMoney0 = (n: number | null | undefined) =>
  n == null ? "—" : `$${Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;

/** Signed money with 2 decimals and a true minus sign — e.g. +$420.18 / −$112.40 */
export const fmtMoneySigned = (n: number | null | undefined) => {
  if (n == null) return "—";
  const sign = n > 0 ? "+" : n < 0 ? "−" : "";
  return `${sign}$${Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

/** Rounded currency, no sign — legacy callers */
export const fmtMoney = (n: number | null | undefined) =>
  n == null
    ? "—"
    : n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

/** Fraction (0.042) → "+4.20%". Pass alreadyPct=true for values already in percent. */
export const fmtPctSigned = (n: number | null | undefined, alreadyPct = false) => {
  if (n == null) return "—";
  const v = alreadyPct ? n : n * 100;
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}${Math.abs(v).toFixed(2)}%`;
};

export const fmtPct = (n: number | null | undefined) =>
  n == null ? "—" : `${(n * 100).toFixed(1)}%`;
