"use client";

import { useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import {
  Activity as ActivityIcon,
  Bot,
  ChevronDown,
  Clock,
  Layers,
  LayoutDashboard,
  ListChecks,
  Loader2,
  Menu,
  OctagonX,
  Play,
  Settings as SettingsIcon,
  X,
} from "lucide-react";
import { toast } from "sonner";

import {
  fetcher,
  Pnl,
  runCycle,
  setKillSwitch,
  setMode,
  startScheduler,
  Settings as SettingsT,
  Status,
  ConnectionStatus,
  stopScheduler,
  Trade,
  fmtMoneySigned,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Toaster } from "@/components/ui/sonner";
import ConfirmDialog, { ConfirmSpec } from "@/components/ConfirmDialog";
import NotificationBell from "@/components/NotificationBell";
import IbkrLogo from "@/components/IbkrLogo";
import { cn } from "@/lib/utils";

import Overview from "@/components/screens/Overview";
import Positions from "@/components/screens/Positions";
import Watchlist from "@/components/screens/Watchlist";
import PendingTrades from "@/components/screens/PendingTrades";
import Activity from "@/components/screens/Activity";
import Settings from "@/components/screens/Settings";

const POLL = { refreshInterval: 8000 };
const MODE_LABELS: Record<string, string> = {
  sandbox: "Sandbox",
  live_approval: "Live-approval",
  live_auto: "Live-auto",
};
const NAV = [
  { id: "overview", label: "Overview", icon: LayoutDashboard },
  { id: "positions", label: "Positions", icon: Layers },
  { id: "watchlist", label: "Watchlist", icon: ListChecks },
  { id: "pending", label: "Pending Trades", icon: Clock },
  { id: "activity", label: "Activity", icon: ActivityIcon },
  { id: "settings", label: "Settings", icon: SettingsIcon },
];

function ConnectionBadge({ conn, className }: { conn?: ConnectionStatus | null; className?: string }) {
  const status = conn?.status ?? "disconnected";
  const detail = conn?.detail ?? (status === "connected" ? "Connected to IBKR" : "Disconnected from IBKR");

  if (status === "connected") {
    return (
      <div
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full border border-gain/30 bg-gain-soft/30 px-2.5 py-0.5 text-[11px] font-medium text-gain transition-all cursor-default select-none",
          className
        )}
        title={detail}
      >
        <span className="live-dot size-2 rounded-full bg-gain" />
        <span>Live</span>
      </div>
    );
  }

  if (status === "warning") {
    return (
      <div
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full border border-amber-500/40 bg-amber-500/10 px-2.5 py-0.5 text-[11px] font-medium text-amber-400 transition-all cursor-default select-none",
          className
        )}
        title={detail}
      >
        <span className="size-2 rounded-full bg-amber-400 animate-pulse" />
        <span>Paper Offline</span>
      </div>
    );
  }

  return (
    <div
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border border-loss/40 bg-loss/10 px-2.5 py-0.5 text-[11px] font-medium text-loss transition-all cursor-default select-none",
        className
      )}
      title={detail}
    >
      <span className="size-2 rounded-full bg-loss" />
      <span>Disconnected</span>
    </div>
  );
}

export default function App() {
  const { mutate } = useSWRConfig();
  const [route, setRoute] = useState("overview");
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null);
  const [running, setRunning] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  const status = useSWR<Status>("/api/status", fetcher, POLL);
  const pnl = useSWR<Pnl>("/api/pnl", fetcher, POLL);
  const approvals = useSWR<Trade[]>("/api/approvals", fetcher, POLL);
  const settings = useSWR<SettingsT>("/api/settings", fetcher, POLL);

  const s = status.data;
  const total = pnl.data?.total_pnl ?? 0;
  const pendingCount = approvals.data?.length ?? 0;
  const refreshAll = () =>
    ["/api/status", "/api/pnl", "/api/positions", "/api/approvals", "/api/activity", "/api/benchmark"].forEach((k) =>
      mutate(k)
    );

  const changeMode = (m: string) => {
    if (!s || m === s.mode) return;
    if (m.startsWith("live")) {
      setConfirm({
        title: "Switch to a live trading mode?",
        body: `You are switching to ${m === "live_auto" ? "Live-auto (fully autonomous)" : "Live-approval"}. This trades a real account. Type LIVE to confirm.`,
        confirmLabel: "Switch to live",
        danger: true,
        requireType: "LIVE",
        onConfirm: async () => {
          setConfirm(null);
          try {
            await setMode(m);
            toast.success(`Mode set to ${MODE_LABELS[m] ?? m}`);
            refreshAll();
          } catch {
            toast.error("Could not change mode");
          }
        },
      });
    } else {
      setMode(m)
        .then(() => {
          toast.success(`Mode set to ${MODE_LABELS[m] ?? m}`);
          refreshAll();
        })
        .catch(() => toast.error("Could not change mode"));
    }
  };

  const toggleAuto = async () => {
    if (!s) return;
    try {
      if (s.scheduler_running) {
        await stopScheduler();
        toast.success("Scheduler stopped");
      } else {
        const iv = settings.data?.scheduler.interval_seconds ?? 300;
        const mh = settings.data?.scheduler.market_hours_only ?? true;
        await startScheduler(iv, mh);
        toast.success("Scheduler started");
      }
      mutate("/api/status");
    } catch {
      toast.error("Could not toggle the scheduler");
    }
  };

  const askKill = () => {
    if (!s) return;
    setConfirm({
      title: s.kill_switch ? "Disengage kill switch?" : "Engage kill switch?",
      body: s.kill_switch
        ? "The agent will resume opening new positions on the next cycle."
        : "Halts ALL new entries instantly. Open positions are unaffected. You can disengage at any time.",
      confirmLabel: s.kill_switch ? "Disengage" : "Engage kill switch",
      danger: !s.kill_switch,
      onConfirm: async () => {
        const was = s.kill_switch;
        setConfirm(null);
        try {
          await setKillSwitch(!was);
          toast[was ? "success" : "error"](was ? "Kill switch disengaged" : "Kill switch ENGAGED — all new entries halted");
          mutate("/api/status");
        } catch {
          toast.error("Could not toggle the kill switch");
        }
      },
    });
  };

  const onRun = async () => {
    setRunning(true);
    try {
      await runCycle();
      toast.success("Cycle complete");
      refreshAll();
    } catch {
      toast.error("Cycle failed");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="flex min-h-screen flex-col">
      {/* ---- Desktop Status bar ---- */}
      <header className="hidden md:flex h-14 flex-shrink-0 items-center gap-4 border-b border-border bg-black px-5">
        <div className="flex items-center gap-2.5">
          <IbkrLogo className="h-6 w-auto" />
          <div className="text-[18px] font-bold tracking-[-0.02em]">
            Tasty<span className="text-brand">Agent</span>
          </div>
        </div>

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-surface-2 px-3 py-1.5 text-[13px] hover:border-border-strong">
              <Bot className="size-[15px]" />
              {s ? MODE_LABELS[s.mode] ?? s.mode : "—"}
              <ChevronDown className="size-3.5" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            {Object.entries(MODE_LABELS).map(([v, l]) => (
              <DropdownMenuItem key={v} active={s?.mode === v} onSelect={() => changeMode(v)}>
                {l}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>

        {s && (
          <Badge variant={s.market_open ? "open" : "warn"} dot={s.market_open}>
            Market {s.market_open ? "Open" : "Closed"}
          </Badge>
        )}

        <button
          onClick={toggleAuto}
          className={cn(
            "inline-flex items-center gap-2 rounded-lg border px-4 py-1.5 text-sm font-semibold transition-colors",
            s?.scheduler_running
              ? "border-gain/50 bg-gain-soft text-gain"
              : "border-border bg-transparent text-muted-foreground hover:border-border-strong"
          )}
        >
          <span className={cn("size-2 rounded-full", s?.scheduler_running ? "bg-gain" : "bg-text-faint")} />
          Auto: {s?.scheduler_running ? "ON" : "OFF"}
        </button>

        <button
          onClick={onRun}
          disabled={running}
          className={cn(
            "inline-flex items-center gap-2 rounded-lg border border-border bg-transparent px-4 py-1.5 text-sm font-semibold text-muted-foreground transition-colors hover:border-border-strong hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
          )}
        >
          {running ? <Loader2 className="size-4 animate-spin text-brand" /> : <Play className="size-4 text-brand" />}
          {running ? "Running cycle…" : "Run cycle"}
        </button>

        <div className="ml-auto flex items-baseline gap-2">
          <span className="text-[11px] uppercase tracking-[0.06em] text-muted-foreground">P/L</span>
          <span className={cn("font-mono text-base font-semibold tabular-nums", total > 0 ? "text-gain" : total < 0 ? "text-loss" : "")}>
            {fmtMoneySigned(total)}
          </span>
        </div>

        <ConnectionBadge conn={s?.connection} className="hidden md:inline-flex" />

        <NotificationBell />

        <button
          onClick={askKill}
          className={cn(
            "inline-flex items-center gap-2 rounded-lg border border-brand px-4 py-1.5 text-sm font-semibold transition-colors",
            s?.kill_switch ? "bg-brand text-white" : "bg-transparent text-brand hover:bg-brand-soft"
          )}
        >
          <OctagonX className="size-4" />
          {s?.kill_switch ? "Kill switch ON" : "Kill switch"}
        </button>
      </header>

      {/* ---- Mobile Status bar ---- */}
      <header className="flex md:hidden flex-col border-b border-border bg-black">
        {/* Row 1: Nav Menu Button + Logo + Market Badge + P/L + NotificationBell */}
        <div className="flex h-12 items-center justify-between px-3 gap-2 border-b border-border/40">
          <div className="flex items-center gap-2 min-w-0">
            <button
              onClick={() => setMobileNavOpen(true)}
              className="inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-muted-foreground hover:bg-surface hover:text-foreground"
              aria-label="Open Navigation"
            >
              <Menu className="size-5" />
            </button>
            <div className="flex items-center gap-2 shrink-0">
              <IbkrLogo className="h-5 w-auto" />
              <div className="text-[17px] font-bold tracking-tight">
                Tasty<span className="text-brand">Agent</span>
              </div>
            </div>
            {s && (
              <span
                className={cn(
                  "inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium",
                  s.market_open ? "bg-gain-soft text-gain" : "bg-surface-2 text-muted-foreground"
                )}
              >
                <span className={cn("size-1.5 rounded-full", s.market_open ? "bg-gain" : "bg-text-faint")} />
                {s.market_open ? "Open" : "Closed"}
              </span>
            )}
          </div>

          <div className="flex items-center gap-2 shrink-0">
            <div className="flex items-baseline gap-1">
              <span className="text-[10px] uppercase text-text-faint">P/L</span>
              <span className={cn("font-mono text-sm font-semibold tabular-nums", total > 0 ? "text-gain" : total < 0 ? "text-loss" : "")}>
                {fmtMoneySigned(total)}
              </span>
            </div>
            <NotificationBell />
          </div>
        </div>

        {/* Row 2: Action bar (scrollable pills, no wrap, no overflow) */}
        <div className="flex items-center gap-2 px-3 py-1.5 overflow-x-auto no-scrollbar bg-surface/30">
          <ConnectionBadge conn={s?.connection} className="shrink-0" />
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border bg-surface-2 px-2.5 py-1 text-xs text-foreground hover:border-border-strong">
                <Bot className="size-3.5 text-brand" />
                {s ? MODE_LABELS[s.mode] ?? s.mode : "—"}
                <ChevronDown className="size-3 text-muted-foreground" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              {Object.entries(MODE_LABELS).map(([v, l]) => (
                <DropdownMenuItem key={v} active={s?.mode === v} onSelect={() => changeMode(v)}>
                  {l}
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>

          <button
            onClick={toggleAuto}
            className={cn(
              "inline-flex shrink-0 items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium transition-colors",
              s?.scheduler_running
                ? "border-gain/50 bg-gain-soft text-gain"
                : "border-border bg-transparent text-muted-foreground hover:border-border-strong"
            )}
          >
            <span className={cn("size-1.5 rounded-full", s?.scheduler_running ? "bg-gain" : "bg-text-faint")} />
            Auto: {s?.scheduler_running ? "ON" : "OFF"}
          </button>

          <button
            onClick={onRun}
            disabled={running}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-border bg-transparent px-2.5 py-1 text-xs font-medium text-muted-foreground hover:border-border-strong hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
          >
            {running ? <Loader2 className="size-3.5 animate-spin text-brand" /> : <Play className="size-3.5 text-brand" />}
            {running ? "Running…" : "Run cycle"}
          </button>

          <button
            onClick={askKill}
            className={cn(
              "inline-flex shrink-0 items-center gap-1 rounded-md border px-2.5 py-1 text-xs font-medium transition-colors",
              s?.kill_switch
                ? "border-brand bg-brand text-white"
                : "border-brand/70 bg-transparent text-brand hover:bg-brand-soft"
            )}
          >
            <OctagonX className="size-3.5" />
            {s?.kill_switch ? "Kill ON" : "Kill"}
          </button>
        </div>
      </header>

      {/* ---- Mobile Navigation Drawer ---- */}
      {mobileNavOpen && (
        <div className="fixed inset-0 z-50 md:hidden">
          {/* Backdrop */}
          <div
            className="fixed inset-0 bg-black/80 backdrop-blur-sm transition-opacity"
            onClick={() => setMobileNavOpen(false)}
          />
          {/* Drawer Panel */}
          <div className="fixed inset-y-0 left-0 w-[280px] max-w-[85vw] bg-black border-r border-border p-4 flex flex-col justify-between shadow-2xl z-10 animate-in slide-in-from-left duration-200">
            <div className="flex flex-col gap-4">
              {/* Drawer Header */}
              <div className="flex items-center justify-between border-b border-border pb-3">
                <div className="flex items-center gap-2">
                  <IbkrLogo className="h-6 w-auto" />
                  <div className="text-lg font-bold tracking-tight">
                    Tasty<span className="text-brand">Agent</span>
                  </div>
                </div>
                <button
                  onClick={() => setMobileNavOpen(false)}
                  className="rounded-lg p-1.5 text-muted-foreground hover:bg-surface-2 hover:text-white"
                  aria-label="Close menu"
                >
                  <X className="size-5" />
                </button>
              </div>

              {/* Mode & Status quick info */}
              <div className="flex items-center justify-between rounded-lg border border-border bg-surface-2 p-2.5 text-xs">
                <div className="flex items-center gap-2">
                  <Bot className="size-4 text-brand" />
                  <span className="font-medium text-foreground">{s ? MODE_LABELS[s.mode] ?? s.mode : "—"}</span>
                </div>
                <span className={cn("text-[11px] font-medium", s?.market_open ? "text-gain" : "text-muted-foreground")}>
                  Market {s?.market_open ? "Open" : "Closed"}
                </span>
              </div>

              {/* Navigation list */}
              <div className="flex flex-col gap-1">
                <div className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wider text-text-faint">
                  Navigation
                </div>
                {NAV.map((nv) => {
                  const active = route === nv.id;
                  return (
                    <button
                      key={nv.id}
                      onClick={() => {
                        setRoute(nv.id);
                        setMobileNavOpen(false);
                      }}
                      className={cn(
                        "relative flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors",
                        active
                          ? "bg-brand-soft text-white [&_svg]:text-brand font-semibold"
                          : "text-muted-foreground hover:bg-surface hover:text-foreground"
                      )}
                    >
                      <nv.icon className="size-5" />
                      <span>{nv.label}</span>
                      {nv.id === "pending" && pendingCount > 0 && (
                        <span className="ml-auto inline-flex h-5 min-w-[20px] items-center justify-center rounded-full bg-brand px-1.5 text-[11px] font-bold text-white">
                          {pendingCount}
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
            </div>

            {/* Drawer Footer Actions */}
            <div className="flex flex-col gap-2 border-t border-border pt-3">
              <div className="flex items-center justify-between text-xs text-muted-foreground px-1">
                <span>Scheduler</span>
                <span className={s?.scheduler_running ? "text-gain font-medium" : "text-muted-foreground"}>
                  {s?.scheduler_running ? "Running (Live)" : "Stopped"}
                </span>
              </div>
              <div className="flex items-center justify-between text-xs text-muted-foreground px-1">
                <span>Total P/L</span>
                <span className={cn("font-mono font-semibold", total > 0 ? "text-gain" : total < 0 ? "text-loss" : "")}>
                  {fmtMoneySigned(total)}
                </span>
              </div>
              <button
                onClick={() => {
                  setMobileNavOpen(false);
                  askKill();
                }}
                className={cn(
                  "mt-1 flex w-full items-center justify-center gap-2 rounded-lg border py-2 text-xs font-semibold transition-colors",
                  s?.kill_switch
                    ? "border-brand bg-brand text-white"
                    : "border-brand/60 bg-transparent text-brand hover:bg-brand-soft"
                )}
              >
                <OctagonX className="size-4" />
                {s?.kill_switch ? "Kill Switch ENGAGED" : "Kill Switch"}
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        {/* ---- Desktop Sidebar ---- */}
        <nav className="hidden md:flex w-[220px] flex-shrink-0 flex-col gap-2 border-r border-border bg-black p-3">
          <div className="flex flex-1 flex-col gap-0.5">
            {NAV.map((nv) => {
              const active = route === nv.id;
              return (
                <button
                  key={nv.id}
                  onClick={() => setRoute(nv.id)}
                  className={cn(
                    "relative flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors [&_svg]:size-5",
                    active ? "bg-brand-soft text-white [&_svg]:text-brand" : "text-muted-foreground hover:bg-surface hover:text-foreground"
                  )}
                >
                  <nv.icon />
                  <span>{nv.label}</span>
                  {nv.id === "pending" && pendingCount > 0 && (
                    <span className="ml-auto inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-brand px-1 text-[10px] font-bold leading-none text-white">
                      {pendingCount}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        </nav>

        {/* ---- Content ---- */}
        <main className="flex-1 overflow-y-auto px-4 md:px-8 pt-4 md:pt-7 pb-24 md:pb-16">
          {status.error ? (
            <div className="mx-auto mt-20 max-w-md rounded-xl border border-loss/40 bg-loss-soft p-6 text-center text-sm text-loss">
              Could not reach the API at <code>:3060</code>. Start the backend, then this dashboard will populate.
            </div>
          ) : (
            <>
              {s?.kill_switch && (
                <div className="mb-5 flex items-center gap-2.5 rounded-[10px] border border-brand/50 bg-brand-soft px-4 py-3 text-[13px] font-medium text-brand">
                  <OctagonX className="size-4" />
                  Kill switch engaged — the agent will not open new positions.
                </div>
              )}
              {route === "overview" && <Overview goTo={setRoute} />}
              {route === "positions" && <Positions />}
              {route === "watchlist" && <Watchlist />}
              {route === "pending" && <PendingTrades />}
              {route === "activity" && <Activity />}
              {route === "settings" && <Settings mode={s?.mode ?? "sandbox"} onModeChange={changeMode} />}
            </>
          )}
        </main>
      </div>

      {/* ---- Mobile Bottom Tab Bar ---- */}
      <nav className="md:hidden fixed bottom-0 inset-x-0 z-40 bg-black/95 backdrop-blur-md border-t border-border flex items-center justify-around px-1 py-1 pb-[max(0.35rem,env(safe-area-inset-bottom))]">
        {NAV.map((nv) => {
          const active = route === nv.id;
          return (
            <button
              key={nv.id}
              onClick={() => setRoute(nv.id)}
              className={cn(
                "relative flex flex-1 flex-col items-center justify-center py-1 text-center transition-colors",
                active ? "text-brand" : "text-muted-foreground hover:text-foreground"
              )}
            >
              <div className="relative">
                <nv.icon className="size-5" />
                {nv.id === "pending" && pendingCount > 0 && (
                  <span className="absolute -top-1 -right-2 inline-flex h-4 min-w-[16px] items-center justify-center rounded-full bg-brand px-1 text-[9px] font-bold text-white">
                    {pendingCount}
                  </span>
                )}
              </div>
              <span className={cn("text-[10px] tracking-tight mt-0.5", active ? "font-semibold text-brand" : "font-normal")}>
                {nv.label === "Pending Trades" ? "Pending" : nv.label}
              </span>
            </button>
          );
        })}
      </nav>

      <ConfirmDialog spec={confirm} onClose={() => setConfirm(null)} />
      <Toaster />
    </div>
  );
}
