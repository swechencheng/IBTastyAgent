"use client";

import { useEffect, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { AlertTriangle, Bot, Check, HandCoins, RotateCcw, Shield, SlidersHorizontal } from "lucide-react";
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
  useCustomCapital: boolean;
  working_capital: number;
  intervalMin: number;
  marketHours: boolean;
  minIvr: number;
  dteMin: number;
  dteTarget: number;
  dteMax: number;
  shortDelta: number;
  maxShortDelta: number;
  testedDelta: number;
  topN: number;
  takeProfit: number;
  manageDte: number;
  hardStop: boolean;
  stopMult: number;
  bpPerTrade: number;
  bpTotal: number;
  maxPos: number;
  maxPerSym: number;
  dailyHalt: number;
};

const n = (v: unknown, d = 0) => (typeof v === "number" ? v : d);

function fromSettings(s: SettingsT): Form {
  const st = s.strategy;
  const rk = s.risk;
  return {
    useCustomCapital: s.use_custom_working_capital ?? true,
    working_capital: s.working_capital,
    intervalMin: Math.round(s.scheduler.interval_seconds / 60),
    marketHours: !!s.scheduler.market_hours_only,
    minIvr: Math.round(n(st.min_iv_rank, 0.3) * 100),
    dteMin: n(st.min_dte, 30),
    dteTarget: n(st.target_dte, 45),
    dteMax: n(st.max_dte, 55),
    shortDelta: Math.round(n(st.target_short_delta, 0.24) * 100),
    maxShortDelta: Math.round(n(st.max_short_leg_delta, 0.25) * 100),
    testedDelta: Math.round(n(st.tested_delta_threshold, 0.45) * 100),
    topN: n(st.universe_top_n, 15),
    takeProfit: Math.round(n(st.take_profit_pct, 0.5) * 100),
    manageDte: n(st.manage_dte, 21),
    hardStop: !!st.use_hard_stop,
    stopMult: n(st.stop_loss_multiple, 2),
    bpPerTrade: Math.round(n(rk.max_trade_bp_pct, 0.05) * 100),
    bpTotal: Math.round(n(rk.max_total_bp_pct, 0.4) * 100),
    maxPos: n(rk.max_positions, 15),
    maxPerSym: n(rk.max_positions_per_symbol, 2),
    dailyHalt: Math.round(n(rk.max_daily_loss_pct, 0.03) * 100),
  };
}

function toPayload(f: Form): SettingsUpdate {
  return {
    use_custom_working_capital: f.useCustomCapital,
    working_capital: f.working_capital,
    scheduler_interval_seconds: f.intervalMin * 60,
    scheduler_market_hours_only: f.marketHours,
    strategy: {
      min_iv_rank: f.minIvr / 100,
      min_dte: f.dteMin,
      target_dte: f.dteTarget,
      max_dte: f.dteMax,
      target_short_delta: f.shortDelta / 100,
      max_short_leg_delta: f.maxShortDelta / 100,
      tested_delta_threshold: f.testedDelta / 100,
      universe_top_n: f.topN,
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
    },
  };
}

const TABS = [
  { id: "agent", label: "Agent", icon: Bot },
  { id: "strategy", label: "Strategy", icon: SlidersHorizontal },
  { id: "management", label: "Management", icon: HandCoins },
  { id: "risk", label: "Risk limits", icon: Shield },
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
      <div className="flex items-center justify-between gap-6">
        <div>
          <div className="text-sm font-medium">{label}</div>
          {help && <div className="mt-0.5 max-w-[520px] text-xs leading-snug text-muted-foreground">{help}</div>}
        </div>
        <div className="flex shrink-0 items-center gap-2">
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
  w = 88,
}: {
  value: number;
  onChange: (v: number) => void;
  disabled?: boolean;
  w?: number;
}) {
  return (
    <Input
      type="number"
      value={Number.isFinite(value) ? value : 0}
      onChange={(e) => onChange(Number(e.target.value))}
      disabled={disabled}
      className={cn("h-9 font-mono", disabled && "opacity-60 cursor-not-allowed")}
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
      toast.success("Settings saved");
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
          <span className="text-xs text-loss font-medium">{validationError}</span>
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
        Manage the agent end-to-end. Switching to a live mode requires a typed confirm; changes apply on the next cycle.
      </PageHeader>

      <Tabs value={tab} onValueChange={setTab} className="mb-[18px]">
        <TabsList className="flex-wrap">
          {TABS.map((t) => (
            <TabsTrigger key={t.id} value={t.id}>
              <t.icon className="size-4" /> {t.label}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      {tab === "agent" && (
        <div className="space-y-4">
          <Card>
            <CardHeader className="flex-row items-center justify-between space-y-0">
              <CardTitle><Bot className="size-4" /> Agent</CardTitle>
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
              <Field label="Cycle interval" help="How often the loop ticks while Auto is on (min 30s)." unit="min">
                <NInput value={form.intervalMin} onChange={(v) => set("intervalMin", v)} />
              </Field>
              <Field label="Market hours only" help="When on, the scheduler won't fire outside regular hours.">
                <Switch checked={form.marketHours} onCheckedChange={(v) => set("marketHours", v)} aria-label="Market hours only" />
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

      {tab === "strategy" && (
        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <CardTitle><SlidersHorizontal className="size-4" /> Strategy params</CardTitle>
            <SaveBar />
          </CardHeader>
          <CardContent>
            <Field label="Min IV rank" help="Only sell premium when IV rank is at least this high." unit="IVR">
              <NInput value={form.minIvr} onChange={(v) => set("minIvr", v)} />
            </Field>
            <Field label="DTE window (min / target / max)" help="Days-to-expiration range for new positions.">
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
              label="Tested delta threshold"
              help="Short-leg delta that triggers defense (roll untested side in). Must be higher than max short leg delta."
              unit="Δ"
              derived={`Defends when short leg reaches ${form.testedDelta}Δ (safety buffer: +${form.testedDelta - form.maxShortDelta}Δ above max entry)`}
            >
              <NInput value={form.testedDelta} onChange={(v) => set("testedDelta", v)} />
            </Field>
            <Field label="Universe top-N" help="How many highest-IVR names get chain work each cycle." unit="names">
              <NInput value={form.topN} onChange={(v) => set("topN", v)} />
            </Field>
          </CardContent>
        </Card>
      )}

      {tab === "management" && (
        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <CardTitle><HandCoins className="size-4" /> Management / Exits</CardTitle>
            <SaveBar />
          </CardHeader>
          <CardContent>
            <Field label="Take-profit" help="Close a winner at this % of max profit." unit="%">
              <NInput value={form.takeProfit} onChange={(v) => set("takeProfit", v)} />
            </Field>
            <Field label="Manage at DTE" help="Roll out / defend when a position reaches this DTE." unit="DTE">
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
            <Field label="Use hard stop" help="Off by default — tastytrade manages rather than stops out.">
              <Switch checked={form.hardStop} onCheckedChange={(v) => set("hardStop", v)} aria-label="Hard stop" />
            </Field>
            <Field label="Stop-loss multiple" help="Stop at this multiple of credit received (when hard stop is on)." unit="×">
              <NInput value={form.stopMult} onChange={(v) => set("stopMult", v)} />
            </Field>
          </CardContent>
        </Card>
      )}

      {tab === "risk" && (
        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <CardTitle><Shield className="size-4" /> Risk limits</CardTitle>
            <SaveBar />
          </CardHeader>
          <CardContent>
            <Field
              label="Max per-trade BP"
              help="Buying-power cap for any single trade."
              unit="%"
              derived={`= ${fmtMoney0(Math.round((form.working_capital * form.bpPerTrade) / 100))} at ${fmtMoney0(form.working_capital)} capital`}
            >
              <NInput value={form.bpPerTrade} onChange={(v) => set("bpPerTrade", v)} />
            </Field>
            <Field label="Max total BP" help="Portfolio-wide buying-power cap." unit="%">
              <NInput value={form.bpTotal} onChange={(v) => set("bpTotal", v)} />
            </Field>
            <Field label="Max positions" help="Hard cap on concurrent open positions." unit="open">
              <NInput value={form.maxPos} onChange={(v) => set("maxPos", v)} />
            </Field>
            <Field label="Max positions / symbol" help="Concentration cap per underlying." unit="per sym">
              <NInput value={form.maxPerSym} onChange={(v) => set("maxPerSym", v)} />
            </Field>
            <Field label="Daily-loss halt" help="Halt all new entries if daily loss exceeds this." unit="%">
              <NInput value={form.dailyHalt} onChange={(v) => set("dailyHalt", v)} />
            </Field>
          </CardContent>
        </Card>
      )}
      <p className={cn("mt-3 text-xs text-text-faint")}>
        Settings are synchronized to backend/.env and persisted across restarts.
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

