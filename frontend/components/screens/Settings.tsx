"use client";

import { useEffect, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import {
  AlertTriangle,
  Bot,
  Check,
  Cpu,
  Eye,
  EyeOff,
  HandCoins,
  RotateCcw,
  Server,
  Shield,
  SlidersHorizontal,
  Sparkles,
} from "lucide-react";
import { toast } from "sonner";

import { fetcher, putSettings, resetSandbox, Settings as SettingsT, SettingsUpdate, fmtMoney0 } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ErrorNote, Loading, PageHeader } from "@/components/common";
import { cn } from "@/lib/utils";

type Form = {
  // --- Agent ---
  useCustomCapital: boolean;
  working_capital: number;
  intervalMin: number;
  marketHours: boolean;
  autoStartScheduler: boolean;

  // --- Strategy ---
  minIvr: number;
  dteMin: number;
  dteTarget: number;
  dteMax: number;
  shortDelta: number;
  maxShortDelta: number;
  spreadLongDelta: number;
  testedDelta: number;
  topN: number;
  maxBidAskWidthPct: number;
  minOpenInterest: number;
  minDailyVolume: number;
  earningsBlackoutDays: number;

  // --- Management ---
  takeProfit: number;
  manageDte: number;
  hardStop: boolean;
  stopMult: number;

  // --- Risk limits ---
  bpPerTrade: number;
  bpTotal: number;
  maxPos: number;
  maxPerSym: number;
  dailyHalt: number;
  consecutiveLossHalt: number;

  // --- IBKR Gateway ---
  ibkrHost: string;
  ibkrPort: number;
  ibkrClientId: number;
  ibkrAccount: string;
  ibkrDataHost: string;
  ibkrDataPort: number;
  ibkrDataClientId: number;
  ibkrScanCode: string;
  ibkrScanRows: number;
  ibkrWalkStep: number;
  ibkrWalkInterval: number;
  ibkrAttachTp: boolean;
  ibkrTpPct: number;

  // --- AI & Model (LLM) ---
  llmApiKey: string;
  llmModel: string;
  llmBaseUrl: string;
  llmSiteUrl: string;
  llmAppName: string;

  // --- System & Network ---
  apiHost: string;
  apiPort: number;
  frontendPort: number;
  frontendApiBase: string;
};

const n = (v: unknown, d = 0) => (typeof v === "number" && Number.isFinite(v) ? v : d);
const s = (v: unknown, d = "") => (typeof v === "string" ? v : d);

function fromSettings(sData: SettingsT): Form {
  const st = sData.strategy || {};
  const rk = sData.risk || {};
  const ib = sData.ibkr || ({} as any);
  const lm = sData.llm || ({} as any);
  const sy = sData.system || ({} as any);

  return {
    useCustomCapital: sData.use_custom_working_capital ?? true,
    working_capital: sData.working_capital ?? 10000,
    intervalMin: Math.round(n(sData.scheduler?.interval_seconds, 300) / 60),
    marketHours: !!sData.scheduler?.market_hours_only,
    autoStartScheduler: !!(sy.auto_start_scheduler ?? sData.auto_start_scheduler),

    minIvr: Math.round(n(st.min_iv_rank, 0.3) * 100),
    dteMin: n(st.min_dte, 30),
    dteTarget: n(st.target_dte, 45),
    dteMax: n(st.max_dte, 55),
    shortDelta: Math.round(n(st.target_short_delta, 0.24) * 100),
    maxShortDelta: Math.round(n(st.max_short_leg_delta, 0.25) * 100),
    spreadLongDelta: Math.round(n(st.spread_long_delta, 0.05) * 100),
    testedDelta: Math.round(n(st.tested_delta_threshold, 0.45) * 100),
    topN: n(st.universe_top_n, 15),
    maxBidAskWidthPct: Math.round(n(st.max_bid_ask_width_pct, 0.10) * 100),
    minOpenInterest: n(st.min_open_interest, 500),
    minDailyVolume: n(st.min_daily_volume, 100),
    earningsBlackoutDays: n(st.earnings_blackout_days, 7),

    takeProfit: Math.round(n(st.take_profit_pct, 0.5) * 100),
    manageDte: n(st.manage_dte, 21),
    hardStop: !!st.use_hard_stop,
    stopMult: n(st.stop_loss_multiple, 2),

    bpPerTrade: Math.round(n(rk.max_trade_bp_pct, 0.05) * 100),
    bpTotal: Math.round(n(rk.max_total_bp_pct, 0.4) * 100),
    maxPos: n(rk.max_positions, 15),
    maxPerSym: n(rk.max_positions_per_symbol, 2),
    dailyHalt: Math.round(n(rk.max_daily_loss_pct, 0.03) * 100),
    consecutiveLossHalt: n(rk.consecutive_loss_halt, 5),

    ibkrHost: s(ib.host, "127.0.0.1"),
    ibkrPort: n(ib.port, 4002),
    ibkrClientId: n(ib.client_id, 55),
    ibkrAccount: s(ib.account, ""),
    ibkrDataHost: s(ib.data_host, "127.0.0.1"),
    ibkrDataPort: n(ib.data_port, 4001),
    ibkrDataClientId: n(ib.data_client_id, 56),
    ibkrScanCode: s(ib.scan_code, "OPT_VOLUME_MOST_ACTIVE"),
    ibkrScanRows: n(ib.scan_rows, 25),
    ibkrWalkStep: n(ib.walk_step, 0.01),
    ibkrWalkInterval: n(ib.walk_interval, 5),
    ibkrAttachTp: ib.attach_tp !== undefined ? !!ib.attach_tp : true,
    ibkrTpPct: Math.round(n(ib.tp_pct, 0.50) * 100),

    llmApiKey: s(lm.api_key, ""),
    llmModel: s(lm.model, "deepseek/deepseek-v4.1-flash"),
    llmBaseUrl: s(lm.base_url, "https://openrouter.ai/api/v1"),
    llmSiteUrl: s(lm.site_url, "https://github.com/swechencheng/IBTastyAgent"),
    llmAppName: s(lm.app_name, "IBTastyAgent"),

    apiHost: s(sy.api_host, "0.0.0.0"),
    apiPort: n(sy.api_port, 3060),
    frontendPort: n(sy.frontend_port, 3066),
    frontendApiBase: s(sy.frontend_api_base, "http://localhost:3060"),
  };
}

function toPayload(f: Form): SettingsUpdate {
  return {
    use_custom_working_capital: f.useCustomCapital,
    working_capital: f.working_capital,
    scheduler_interval_seconds: f.intervalMin * 60,
    scheduler_market_hours_only: f.marketHours,
    auto_start_scheduler: f.autoStartScheduler,
    strategy: {
      min_iv_rank: f.minIvr / 100,
      min_dte: f.dteMin,
      target_dte: f.dteTarget,
      max_dte: f.dteMax,
      target_short_delta: f.shortDelta / 100,
      max_short_leg_delta: f.maxShortDelta / 100,
      spread_long_delta: f.spreadLongDelta / 100,
      tested_delta_threshold: f.testedDelta / 100,
      universe_top_n: f.topN,
      max_bid_ask_width_pct: f.maxBidAskWidthPct / 100,
      min_open_interest: f.minOpenInterest,
      min_daily_volume: f.minDailyVolume,
      earnings_blackout_days: f.earningsBlackoutDays,
      take_profit_pct: f.takeProfit / 100,
      manage_dte: f.manageDte,
      use_hard_stop: f.hardStop,
      stop_loss_multiple: f.stopMult,
    },
    risk: {
      max_trade_bp_pct: f.bpPerTrade / 100,
      max_total_bp_pct: f.bpTotal / 100,
      max_positions: f.maxPos,
      max_positions_per_symbol: f.maxPerSym,
      max_daily_loss_pct: f.dailyHalt / 100,
      consecutive_loss_halt: f.consecutiveLossHalt,
    },
    ibkr: {
      host: f.ibkrHost,
      port: f.ibkrPort,
      client_id: f.ibkrClientId,
      account: f.ibkrAccount,
      data_host: f.ibkrDataHost,
      data_port: f.ibkrDataPort,
      data_client_id: f.ibkrDataClientId,
      scan_code: f.ibkrScanCode,
      scan_rows: f.ibkrScanRows,
      walk_step: f.ibkrWalkStep,
      walk_interval: f.ibkrWalkInterval,
      attach_tp: f.ibkrAttachTp,
      tp_pct: f.ibkrTpPct / 100,
    },
    llm: {
      api_key: f.llmApiKey,
      model: f.llmModel,
      base_url: f.llmBaseUrl,
      site_url: f.llmSiteUrl,
      app_name: f.llmAppName,
    },
    system: {
      api_host: f.apiHost,
      api_port: f.apiPort,
      frontend_port: f.frontendPort,
      frontend_api_base: f.frontendApiBase,
      auto_start_scheduler: f.autoStartScheduler,
    },
  };
}

const TABS = [
  { id: "agent", label: "Agent", icon: Bot },
  { id: "strategy", label: "Strategy", icon: SlidersHorizontal },
  { id: "management", label: "Management", icon: HandCoins },
  { id: "risk", label: "Risk limits", icon: Shield },
  { id: "ibkr", label: "IBKR Gateway", icon: Cpu },
  { id: "llm", label: "AI & Model", icon: Sparkles },
  { id: "system", label: "System & Network", icon: Server },
];

function Field({
  label,
  help,
  unit,
  derived,
  children,
}: {
  label: string;
  help?: string;
  unit?: string;
  derived?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="border-b border-border py-3.5 last:border-0">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 sm:gap-6">
        <div>
          <div className="text-sm font-medium">{label}</div>
          {help && <div className="mt-0.5 max-w-[540px] text-xs leading-snug text-muted-foreground">{help}</div>}
        </div>
        <div className="flex shrink-0 items-center gap-2 self-start sm:self-center">
          {children}
          {unit && <span className="font-mono text-xs text-text-faint">{unit}</span>}
        </div>
      </div>
      {derived && (
        <div
          className={cn(
            "mt-2 inline-block rounded-md px-2.5 py-1 font-mono text-xs",
            derived.startsWith("⚠️")
              ? "bg-loss-soft text-loss font-medium"
              : "bg-gain-soft text-gain"
          )}
        >
          {derived}
        </div>
      )}
    </div>
  );
}

function NInput({
  value,
  onChange,
  disabled = false,
  step = 1,
  w = 88,
}: {
  value: number;
  onChange: (v: number) => void;
  disabled?: boolean;
  step?: number;
  w?: number;
}) {
  return (
    <Input
      type="number"
      step={step}
      value={Number.isFinite(value) ? value : 0}
      onChange={(e) => onChange(Number(e.target.value))}
      disabled={disabled}
      className={cn("h-9 font-mono", disabled && "opacity-60 cursor-not-allowed")}
      style={{ width: w }}
    />
  );
}

function SInput({
  value,
  onChange,
  disabled = false,
  placeholder,
  w = 240,
}: {
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
  placeholder?: string;
  w?: number;
}) {
  return (
    <Input
      type="text"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      disabled={disabled}
      placeholder={placeholder}
      className={cn("h-9 font-mono text-xs", disabled && "opacity-60 cursor-not-allowed")}
      style={{ width: w }}
    />
  );
}

export default function Settings({
  mode,
  onModeChange,
}: {
  mode: string;
  onModeChange: (m: string) => void;
}) {
  const { mutate } = useSWRConfig();
  const { data, error } = useSWR<SettingsT>("/api/settings", fetcher);
  const [tab, setTab] = useState("agent");
  const [form, setForm] = useState<Form | null>(null);
  const [snap, setSnap] = useState("");
  const [saving, setSaving] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [showApiKey, setShowApiKey] = useState(false);

  const handleResetSandbox = async () => {
    setResetting(true);
    try {
      const res = await resetSandbox();
      toast.success(res.message || "Sandbox reset successfully");
      setResetOpen(false);

      if (typeof window !== "undefined") {
        window.dispatchEvent(new CustomEvent("tastyagent:sandbox_reset"));
        localStorage.removeItem("tastyagent_last_read_event_id");
      }

      mutate("/api/positions");
      mutate("/api/trades");
      mutate("/api/trades/closed");
      mutate("/api/approvals");
      mutate("/api/pnl");
      mutate("/api/activity");
      mutate("/api/events");
      mutate("/api/benchmark");
      mutate("/api/status");
    } catch (err: any) {
      toast.error(err.message || "Failed to reset sandbox");
    } finally {
      setResetting(false);
    }
  };

  useEffect(() => {
    if (data && form === null) {
      const f = fromSettings(data);
      setForm(f);
      setSnap(JSON.stringify(f));
    }
  }, [data, form]);

  if (error) return <ErrorNote msg="Could not load settings." />;
  if (!data || !form) return <Loading />;

  const set = <K extends keyof Form>(k: K, v: Form[K]) => setForm((p) => (p ? { ...p, [k]: v } : p));
  const dirty = JSON.stringify(form) !== snap;

  const validationError = (() => {
    if (!form) return null;
    if (form.working_capital <= 0) return "Working capital must be greater than 0";
    if (form.shortDelta <= 0) return "Target short delta must be greater than 0";
    if (form.shortDelta > form.maxShortDelta) {
      return `Target short delta (${form.shortDelta}Δ) cannot exceed Max short leg delta (${form.maxShortDelta}Δ)`;
    }
    if (form.maxShortDelta >= form.testedDelta) {
      return `Max short leg delta (${form.maxShortDelta}Δ) must be strictly less than Tested delta threshold (${form.testedDelta}Δ)`;
    }
    if (form.testedDelta > 90) return "Tested delta threshold must be ≤ 90Δ";
    if (form.dteMin > form.dteTarget || form.dteTarget > form.dteMax) {
      return `DTE window must satisfy min (${form.dteMin}) ≤ target (${form.dteTarget}) ≤ max (${form.dteMax})`;
    }
    if (form.bpPerTrade <= 0 || form.bpPerTrade > 100) return "Max trade BP % must be between 1% and 100%";
    if (form.bpTotal < form.bpPerTrade) return "Max total BP % must be greater than or equal to per-trade BP %";
    if (form.ibkrPort <= 0 || form.ibkrPort > 65535) return "Invalid IBKR trading gateway port";
    if (form.ibkrDataPort <= 0 || form.ibkrDataPort > 65535) return "Invalid IBKR market data port";
    if (form.apiPort <= 0 || form.apiPort > 65535) return "Invalid backend API port";
    if (form.frontendPort <= 0 || form.frontendPort > 65535) return "Invalid frontend port";
    return null;
  })();

  const save = async () => {
    if (validationError) {
      toast.error(validationError);
      return;
    }
    setSaving(true);
    try {
      const next = await putSettings(toPayload(form));
      mutate("/api/settings", next, false);
      mutate("/api/status");
      const f = fromSettings(next);
      setForm(f);
      setSnap(JSON.stringify(f));
      toast.success("Settings saved and synced to .env files");
    } catch {
      toast.error("Could not save settings");
    } finally {
      setSaving(false);
    }
  };

  const SaveBar = () =>
    dirty ? (
      <div className="flex items-center gap-3">
        {validationError && (
          <span className="text-xs text-loss font-medium hidden sm:inline">{validationError}</span>
        )}
        <Button size="sm" onClick={save} disabled={saving || !!validationError}>
          {saving ? "Saving…" : "Save changes"}
        </Button>
      </div>
    ) : (
      <span className="inline-flex items-center gap-1.5 text-xs text-text-faint">
        <Check className="size-3.5 text-gain" /> Saved
      </span>
    );

  return (
    <div className="max-w-[1080px]">
      <PageHeader title="Settings">
        Manage the agent end-to-end. All parameters synchronize to backend/.env and frontend/.env.local and persist across restarts.
      </PageHeader>

      <Tabs value={tab} onValueChange={setTab} className="mb-[18px]">
        <TabsList className="flex-wrap">
          {TABS.map((t) => (
            <TabsTrigger key={t.id} value={t.id}>
              <t.icon className="size-4 mr-1.5" /> {t.label}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      {/* 1. AGENT TAB */}
      {tab === "agent" && (
        <div className="space-y-4">
          <Card>
            <CardHeader className="flex-row items-center justify-between space-y-0">
              <CardTitle><Bot className="size-4" /> Agent execution & capital</CardTitle>
              <SaveBar />
            </CardHeader>
            <CardContent>
              <Field label="Trading mode" help="Switching to a live mode requires a typed confirm in the dialog.">
                <Tabs value={mode} onValueChange={onModeChange}>
                  <TabsList>
                    <TabsTrigger value="sandbox">Sandbox</TabsTrigger>
                    <TabsTrigger value="live_approval">Live-approval</TabsTrigger>
                    <TabsTrigger value="live_auto">Live-auto</TabsTrigger>
                  </TabsList>
                </Tabs>
              </Field>
              <Field
                label="Custom working capital"
                help="When ON, size positions against custom capital below. When OFF, size dynamically against connected IBKR account Total Cash."
              >
                <Switch
                  checked={form.useCustomCapital}
                  onCheckedChange={(v) => set("useCustomCapital", v)}
                  aria-label="Custom working capital"
                />
              </Field>
              <Field
                label="Working capital"
                help={
                  form.useCustomCapital
                    ? "Capital the agent sizes positions against (fixed simulation)."
                    : "Automatically using IBKR account Total Cash (USD equivalent across all forex positions)."
                }
                unit="$"
                derived={
                  form.useCustomCapital
                    ? `Per-trade BP cap = ${form.bpPerTrade}% × ${fmtMoney0(form.working_capital)} = ${fmtMoney0(Math.round((form.working_capital * form.bpPerTrade) / 100))}`
                    : data?.account_cash_usd != null
                    ? `IBKR Total Cash: ${fmtMoney0(data.account_cash_usd)} USD (Base: ${data.account_base_currency ?? "USD"} ${fmtMoney0(data.account_cash_base ?? data.account_cash_usd)}) · Per-trade cap = ${fmtMoney0(Math.round((data.account_cash_usd * form.bpPerTrade) / 100))}`
                    : `Syncing with IBKR Total Cash... · Per-trade cap = ${form.bpPerTrade}%`
                }
              >
                <NInput
                  value={form.useCustomCapital ? form.working_capital : (data?.account_cash_usd ?? form.working_capital)}
                  onChange={(v) => set("working_capital", v)}
                  disabled={!form.useCustomCapital}
                  w={104}
                />
              </Field>
              <Field label="Cycle interval" help="How often the loop ticks while Auto scheduler is active (min 30s)." unit="min">
                <NInput value={form.intervalMin} onChange={(v) => set("intervalMin", v)} />
              </Field>
              <Field label="Market hours only" help="When ON, the scheduler won't fire outside regular US market hours.">
                <Switch checked={form.marketHours} onCheckedChange={(v) => set("marketHours", v)} aria-label="Market hours only" />
              </Field>
              <Field
                label="Auto-start scheduler"
                help="When ON, automatically begins scheduled trading cycles immediately upon backend daemon startup (TASTYAGENT_AUTO_START_SCHEDULER)."
              >
                <Switch
                  checked={form.autoStartScheduler}
                  onCheckedChange={(v) => set("autoStartScheduler", v)}
                  aria-label="Auto-start scheduler"
                />
              </Field>
            </CardContent>
          </Card>

          <Card className="border-loss/30 bg-loss/[0.02]">
            <CardHeader className="flex-row items-center justify-between space-y-0">
              <CardTitle className="text-loss flex items-center gap-2">
                <AlertTriangle className="size-4 text-loss" /> Danger zone
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 py-2">
                <div>
                  <div className="text-sm font-medium text-foreground">Reset sandbox</div>
                  <div className="mt-1 text-xs text-muted-foreground max-w-[540px]">
                    Zero out and clear all sandbox positions, open orders, trade history, decisions, and performance metrics to start fresh. Any working sandbox orders on IBKR will be cancelled.
                  </div>
                </div>
                <Button
                  variant="destructive"
                  className="bg-loss hover:bg-loss/90 text-white font-medium shrink-0"
                  onClick={() => setResetOpen(true)}
                >
                  <RotateCcw className="size-3.5 mr-1.5" />
                  Reset sandbox
                </Button>
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      {/* 2. STRATEGY TAB */}
      {tab === "strategy" && (
        <div className="space-y-4">
          <Card>
            <CardHeader className="flex-row items-center justify-between space-y-0">
              <CardTitle><SlidersHorizontal className="size-4" /> Strike selection & DTE window</CardTitle>
              <SaveBar />
            </CardHeader>
            <CardContent>
              <Field label="Min IV rank" help="Only sell premium when IV rank is at least this high." unit="IVR">
                <NInput value={form.minIvr} onChange={(v) => set("minIvr", v)} />
              </Field>
              <Field label="DTE window (min / target / max)" help="Days-to-expiration range for selecting expiration cycles.">
                <span className="flex gap-1.5">
                  <NInput value={form.dteMin} onChange={(v) => set("dteMin", v)} w={64} />
                  <NInput value={form.dteTarget} onChange={(v) => set("dteTarget", v)} w={64} />
                  <NInput value={form.dteMax} onChange={(v) => set("dteMax", v)} w={64} />
                </span>
              </Field>
              <Field
                label="Target short delta"
                help="Target strike selection delta for short legs (e.g. 24Δ ≈ 52% OTM / ~70% PoP)."
                unit="Δ"
                derived={
                  form.shortDelta > form.maxShortDelta
                    ? `⚠️ Guard violation: Target short delta (${form.shortDelta}Δ) cannot exceed Max short delta cap (${form.maxShortDelta}Δ)`
                    : undefined
                }
              >
                <NInput value={form.shortDelta} onChange={(v) => set("shortDelta", v)} />
              </Field>
              <Field
                label="Max short leg delta"
                help="Hard cap on any short leg at entry. Target short delta must not exceed this."
                unit="Δ"
                derived={
                  form.maxShortDelta >= form.testedDelta
                    ? `⚠️ Guard violation: Max short delta (${form.maxShortDelta}Δ) must be strictly less than Tested threshold (${form.testedDelta}Δ)`
                    : undefined
                }
              >
                <NInput value={form.maxShortDelta} onChange={(v) => set("maxShortDelta", v)} />
              </Field>
              <Field
                label="Spread long wing delta"
                help="Target delta for buying protective outer wings on defined-risk spreads (default: 5Δ)."
                unit="Δ"
              >
                <NInput value={form.spreadLongDelta} onChange={(v) => set("spreadLongDelta", v)} />
              </Field>
              <Field
                label="Tested delta threshold"
                help="Short-leg delta that triggers defense (roll untested side in). Must be higher than max short leg delta."
                unit="Δ"
                derived={`Defends when short leg reaches ${form.testedDelta}Δ (safety buffer: +${form.testedDelta - form.maxShortDelta}Δ above max entry)`}
              >
                <NInput value={form.testedDelta} onChange={(v) => set("testedDelta", v)} />
              </Field>
              <Field label="Universe top-N" help="How many highest-IVR names get option chain evaluations each cycle." unit="names">
                <NInput value={form.topN} onChange={(v) => set("topN", v)} />
              </Field>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex-row items-center justify-between space-y-0">
              <CardTitle><SlidersHorizontal className="size-4" /> Liquidity & market filters</CardTitle>
              <SaveBar />
            </CardHeader>
            <CardContent>
              <Field
                label="Max bid-ask spread width"
                help="Maximum acceptable (ask - bid) / mid ratio for option legs (default: 10%, relaxed to 50% in sandbox)."
                unit="%"
              >
                <NInput value={form.maxBidAskWidthPct} onChange={(v) => set("maxBidAskWidthPct", v)} />
              </Field>
              <Field
                label="Min open interest"
                help="Minimum open interest required on strike candidate contracts (default: 500 contracts)."
                unit="contracts"
              >
                <NInput value={form.minOpenInterest} onChange={(v) => set("minOpenInterest", v)} w={100} />
              </Field>
              <Field
                label="Min daily option volume"
                help="Minimum daily contract volume required on strike candidate (default: 100 contracts)."
                unit="contracts"
              >
                <NInput value={form.minDailyVolume} onChange={(v) => set("minDailyVolume", v)} w={100} />
              </Field>
              <Field
                label="Earnings blackout window"
                help="Number of days before upcoming company earnings announcement to avoid opening new positions (default: 7 days)."
                unit="days"
              >
                <NInput value={form.earningsBlackoutDays} onChange={(v) => set("earningsBlackoutDays", v)} />
              </Field>
            </CardContent>
          </Card>
        </div>
      )}

      {/* 3. MANAGEMENT TAB */}
      {tab === "management" && (
        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <CardTitle><HandCoins className="size-4" /> Management & exit rules</CardTitle>
            <SaveBar />
          </CardHeader>
          <CardContent>
            <Field label="Take-profit" help="Close a winning trade at this percentage of max credit received." unit="%">
              <NInput value={form.takeProfit} onChange={(v) => set("takeProfit", v)} />
            </Field>
            <Field label="Manage at DTE" help="Roll out / manage defense when a position reaches this DTE (default: 21 DTE)." unit="DTE">
              <NInput value={form.manageDte} onChange={(v) => set("manageDte", v)} />
            </Field>
            <Field
              label="Tested delta threshold"
              help="Short-leg delta that triggers defense (roll untested side in). Must be higher than max short leg delta."
              unit="Δ"
              derived={`Defends when short leg reaches ${form.testedDelta}Δ (safety buffer: +${form.testedDelta - form.maxShortDelta}Δ above max entry)`}
            >
              <NInput value={form.testedDelta} onChange={(v) => set("testedDelta", v)} />
            </Field>
            <Field label="Use hard stop" help="Off by default — tastytrade mechanics prefer managing at 21 DTE rather than stopping out.">
              <Switch checked={form.hardStop} onCheckedChange={(v) => set("hardStop", v)} aria-label="Hard stop" />
            </Field>
            <Field label="Stop-loss multiple" help="Close at this multiple of credit received when hard stop is enabled (e.g. 2.0× credit)." unit="×">
              <NInput value={form.stopMult} onChange={(v) => set("stopMult", v)} step={0.5} />
            </Field>
          </CardContent>
        </Card>
      )}

      {/* 4. RISK TAB */}
      {tab === "risk" && (
        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <CardTitle><Shield className="size-4" /> Portfolio risk limits</CardTitle>
            <SaveBar />
          </CardHeader>
          <CardContent>
            <Field
              label="Max per-trade BP"
              help="Buying-power allocation cap for any single trade relative to working capital."
              unit="%"
              derived={`= ${fmtMoney0(Math.round((form.working_capital * form.bpPerTrade) / 100))} at ${fmtMoney0(form.working_capital)} capital`}
            >
              <NInput value={form.bpPerTrade} onChange={(v) => set("bpPerTrade", v)} />
            </Field>
            <Field
              label="Max total BP"
              help="Portfolio-wide buying-power usage cap relative to working capital."
              unit="%"
              derived={`= ${fmtMoney0(Math.round((form.working_capital * form.bpTotal) / 100))} total capacity`}
            >
              <NInput value={form.bpTotal} onChange={(v) => set("bpTotal", v)} />
            </Field>
            <Field label="Max positions" help="Hard cap on concurrent open trades across all tickers." unit="open">
              <NInput value={form.maxPos} onChange={(v) => set("maxPos", v)} />
            </Field>
            <Field label="Max positions / symbol" help="Concentration limit on open positions per individual underlying ticker." unit="per sym">
              <NInput value={form.maxPerSym} onChange={(v) => set("maxPerSym", v)} />
            </Field>
            <Field label="Daily-loss halt" help="Halt all new entries for the rest of the day if daily realized loss exceeds this." unit="%">
              <NInput value={form.dailyHalt} onChange={(v) => set("dailyHalt", v)} />
            </Field>
            <Field label="Consecutive loss halt" help="Halt all new entries if the agent experiences N consecutive losing trades (default: 5)." unit="trades">
              <NInput value={form.consecutiveLossHalt} onChange={(v) => set("consecutiveLossHalt", v)} />
            </Field>
          </CardContent>
        </Card>
      )}

      {/* 5. IBKR GATEWAY TAB */}
      {tab === "ibkr" && (
        <div className="space-y-4">
          <Card>
            <CardHeader className="flex-row items-center justify-between space-y-0">
              <CardTitle><Cpu className="size-4" /> IBKR Trading Gateway</CardTitle>
              <SaveBar />
            </CardHeader>
            <CardContent>
              <Field
                label="Gateway Host"
                help="Hostname or IP of the IBKR Gateway or TWS instance used for trading (IBKR_HOST)."
              >
                <SInput value={form.ibkrHost} onChange={(v) => set("ibkrHost", v)} placeholder="127.0.0.1" />
              </Field>
              <Field
                label="Gateway Port"
                help="API port for trading gateway (IBKR_PORT: 4002 for paper, 4001 for live)."
                unit="port"
              >
                <NInput value={form.ibkrPort} onChange={(v) => set("ibkrPort", v)} w={96} />
              </Field>
              <Field
                label="Trading Client ID"
                help="Dedicated client ID for trade order execution to avoid collisions with other bots (IBKR_CLIENT_ID)."
                unit="id"
              >
                <NInput value={form.ibkrClientId} onChange={(v) => set("ibkrClientId", v)} w={96} />
              </Field>
              <Field
                label="Live Account ID"
                help="Specific account ID (e.g. U24862056) for live trading. In sandbox mode, this is ignored and the connected paper gateway account is used automatically."
              >
                <SInput value={form.ibkrAccount} onChange={(v) => set("ibkrAccount", v)} placeholder="e.g. U1234567" />
              </Field>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex-row items-center justify-between space-y-0">
              <CardTitle><Cpu className="size-4" /> Real-time Market Data Gateway (Dual-Gateway)</CardTitle>
              <SaveBar />
            </CardHeader>
            <CardContent>
              <Field
                label="Market Data Host"
                help="Host for streaming live real-time market data (IBKR_DATA_HOST). Allows streaming real market data while paper trading."
              >
                <SInput value={form.ibkrDataHost} onChange={(v) => set("ibkrDataHost", v)} placeholder="127.0.0.1" />
              </Field>
              <Field
                label="Market Data Port"
                help="Port for market data gateway (IBKR_DATA_PORT: e.g. 4001 live data port)."
                unit="port"
              >
                <NInput value={form.ibkrDataPort} onChange={(v) => set("ibkrDataPort", v)} w={96} />
              </Field>
              <Field
                label="Market Data Client ID"
                help="Dedicated client ID for streaming market data (IBKR_DATA_CLIENT_ID)."
                unit="id"
              >
                <NInput value={form.ibkrDataClientId} onChange={(v) => set("ibkrDataClientId", v)} w={96} />
              </Field>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex-row items-center justify-between space-y-0">
              <CardTitle><Cpu className="size-4" /> Market Scanner & Execution Algorithm</CardTitle>
              <SaveBar />
            </CardHeader>
            <CardContent>
              <Field
                label="Market Scanner Code"
                help="Option scanner code used to discover active stock universe (IBKR_SCAN_CODE: OPT_VOLUME_MOST_ACTIVE, HIGH_OPT_VOLUME_PUT_CALL_RATIO, MOST_ACTIVE)."
              >
                <SInput value={form.ibkrScanCode} onChange={(v) => set("ibkrScanCode", v)} w={260} />
              </Field>
              <Field
                label="Scanner Rows"
                help="Number of candidate stock rows fetched from scanner each cycle (IBKR_SCAN_ROWS)."
                unit="rows"
              >
                <NInput value={form.ibkrScanRows} onChange={(v) => set("ibkrScanRows", v)} />
              </Field>
              <Field
                label="Walk-the-book Step"
                help="Price adjustment increment when walking limit orders from mid-price toward natural bid/ask (IBKR_WALK_STEP)."
                unit="$"
              >
                <NInput value={form.ibkrWalkStep} onChange={(v) => set("ibkrWalkStep", v)} step={0.01} />
              </Field>
              <Field
                label="Walk Interval"
                help="Seconds between walk-the-book limit price adjustments (IBKR_WALK_INTERVAL)."
                unit="sec"
              >
                <NInput value={form.ibkrWalkInterval} onChange={(v) => set("ibkrWalkInterval", v)} />
              </Field>
              <Field
                label="Pre-attach Take Profit"
                help="Automatically attach a 50% max-profit closing limit order upon opening BAG combos on IBKR (IBKR_ATTACH_TP)."
              >
                <Switch
                  checked={form.ibkrAttachTp}
                  onCheckedChange={(v) => set("ibkrAttachTp", v)}
                  aria-label="Pre-attach Take Profit"
                />
              </Field>
              <Field
                label="Pre-attached TP target"
                help="Target profit percentage for pre-attached bracket/closing limit orders (IBKR_TP_PCT)."
                unit="%"
              >
                <NInput value={form.ibkrTpPct} onChange={(v) => set("ibkrTpPct", v)} />
              </Field>
            </CardContent>
          </Card>
        </div>
      )}

      {/* 6. AI & MODEL TAB */}
      {tab === "llm" && (
        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <CardTitle><Sparkles className="size-4" /> OpenRouter Adaptive LLM Layer</CardTitle>
            <SaveBar />
          </CardHeader>
          <CardContent>
            <Field
              label="OpenRouter API Key"
              help="API key for LLM decision reasoning, risk analysis, and trade commentary. Stored securely in backend/.env. Masked keys will not be overwritten."
            >
              <div className="flex items-center gap-2">
                <Input
                  type={showApiKey ? "text" : "password"}
                  value={form.llmApiKey}
                  onChange={(e) => set("llmApiKey", e.target.value)}
                  placeholder="sk-or-v1-..."
                  className="h-9 font-mono text-xs w-[240px] sm:w-[320px]"
                />
                <Button
                  type="button"
                  variant="outline"
                  size="icon"
                  className="h-9 w-9 shrink-0"
                  onClick={() => setShowApiKey(!showApiKey)}
                  title={showApiKey ? "Hide key" : "Show key"}
                >
                  {showApiKey ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                </Button>
              </div>
            </Field>
            <Field
              label="Model Identifier"
              help="OpenRouter model identifier used for decision cycles (OPENROUTER_MODEL, e.g. deepseek/deepseek-v4.1-flash)."
            >
              <SInput value={form.llmModel} onChange={(v) => set("llmModel", v)} w={300} placeholder="deepseek/deepseek-v4.1-flash" />
            </Field>
            <Field
              label="API Base URL"
              help="OpenAI-compatible API base endpoint (OPENROUTER_BASE_URL, default: https://openrouter.ai/api/v1)."
            >
              <SInput value={form.llmBaseUrl} onChange={(v) => set("llmBaseUrl", v)} w={300} placeholder="https://openrouter.ai/api/v1" />
            </Field>
            <Field
              label="Site URL / Referer"
              help="HTTP-Referer header sent to OpenRouter for leaderboard inclusion (OPENROUTER_SITE_URL)."
            >
              <SInput value={form.llmSiteUrl} onChange={(v) => set("llmSiteUrl", v)} w={300} />
            </Field>
            <Field
              label="Application Title"
              help="X-Title header sent to OpenRouter for dashboard usage logs (OPENROUTER_APP_NAME)."
            >
              <SInput value={form.llmAppName} onChange={(v) => set("llmAppName", v)} w={220} />
            </Field>
          </CardContent>
        </Card>
      )}

      {/* 7. SYSTEM & NETWORK TAB */}
      {tab === "system" && (
        <div className="space-y-4">
          <Card>
            <CardHeader className="flex-row items-center justify-between space-y-0">
              <CardTitle><Server className="size-4" /> Backend Server (backend/.env)</CardTitle>
              <SaveBar />
            </CardHeader>
            <CardContent>
              <Field
                label="API Bind Host"
                help="Network interface host the FastAPI backend server binds to (TASTYAGENT_HOST, default: 0.0.0.0)."
              >
                <SInput value={form.apiHost} onChange={(v) => set("apiHost", v)} placeholder="0.0.0.0" w={160} />
              </Field>
              <Field
                label="API Server Port"
                help="Port the FastAPI backend service listens on (TASTYAGENT_PORT, default: 3060)."
                unit="port"
              >
                <NInput value={form.apiPort} onChange={(v) => set("apiPort", v)} w={96} />
              </Field>
              <Field
                label="Auto-start Scheduler"
                help="Automatically starts the trading scheduler daemon loop upon service launch (TASTYAGENT_AUTO_START_SCHEDULER)."
              >
                <Switch
                  checked={form.autoStartScheduler}
                  onCheckedChange={(v) => set("autoStartScheduler", v)}
                  aria-label="Auto-start scheduler"
                />
              </Field>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex-row items-center justify-between space-y-0">
              <CardTitle><Server className="size-4" /> Frontend Web App (frontend/.env.local)</CardTitle>
              <SaveBar />
            </CardHeader>
            <CardContent>
              <Field
                label="Frontend Web Port"
                help="Local port the Next.js web application binds to (PORT in frontend/.env.local, default: 3066)."
                unit="port"
              >
                <NInput value={form.frontendPort} onChange={(v) => set("frontendPort", v)} w={96} />
              </Field>
              <Field
                label="Frontend API Base URL"
                help="Endpoint used by the frontend dashboard to connect to the backend (NEXT_PUBLIC_API_BASE in frontend/.env.local)."
              >
                <SInput
                  value={form.frontendApiBase}
                  onChange={(v) => set("frontendApiBase", v)}
                  placeholder="http://localhost:3060"
                  w={300}
                />
              </Field>
            </CardContent>
          </Card>
        </div>
      )}

      <p className="mt-3 text-xs text-text-faint">
        Settings are synchronized to backend/.env and frontend/.env.local, and persisted across restarts.
      </p>

      <Dialog open={resetOpen} onOpenChange={setResetOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-loss">
              <AlertTriangle className="size-5 text-loss" /> Reset sandbox environment?
            </DialogTitle>
            <DialogDescription className="pt-2 text-sm text-muted-foreground">
              This action will permanently delete all sandbox positions, trades, orders, and execution history from the agent ledger. Any active sandbox Take-Profit or working limit orders on IBKR will also be cancelled.
              <br /><br />
              Are you sure you want to reset everything and start over from scratch?
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="mt-4 flex gap-2 justify-end">
            <Button
              variant="outline"
              onClick={() => setResetOpen(false)}
              disabled={resetting}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              className="bg-loss hover:bg-loss/90 text-white font-medium"
              onClick={handleResetSandbox}
              disabled={resetting}
            >
              {resetting ? "Resetting…" : "Yes, reset sandbox"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
