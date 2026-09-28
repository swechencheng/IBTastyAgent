"use client";

import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight, Loader2, TriangleAlert } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { cn, formatLocalDateTime } from "@/lib/utils";
import { fmtMoney0, Trade, TradeEvent } from "@/lib/api";

/* ---- Number helpers (presentation) --------------------------------------- */
export const pop = (p: number | null | undefined) => `${Math.round((p ?? 0) * 100)}%`;
export const signClass = (n: number) => (n > 0 ? "text-gain" : n < 0 ? "text-loss" : "text-foreground");
export const num = "font-mono tabular-nums";

/* ---- KPI card ------------------------------------------------------------- */
export function Kpi({
  label,
  value,
  delta,
  deltaTone = "flat",
  sub,
}: {
  label: string;
  value: string;
  delta?: string;
  deltaTone?: "up" | "down" | "flat";
  sub?: string;
}) {
  const valTone = value && value[0] === "+" ? "text-gain" : value && value[0] === "−" ? "text-loss" : "";
  return (
    <Card className="p-3.5 md:p-4">
      <div className="text-[11px] uppercase tracking-[0.06em] text-muted-foreground truncate">{label}</div>
      <div className={cn("mt-1.5 md:mt-2 font-mono text-[20px] md:text-[26px] font-semibold leading-none tabular-nums", valTone)}>
        {value}
      </div>
      {(delta != null || sub) && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {delta != null && (
            <span
              className={cn(
                "inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[10px] md:text-[11px]",
                deltaTone === "up" && "bg-gain-soft text-gain",
                deltaTone === "down" && "bg-loss-soft text-loss",
                deltaTone === "flat" && "bg-surface-2 text-muted-foreground"
              )}
            >
              {deltaTone === "up" ? "▲" : deltaTone === "down" ? "▼" : "•"} {delta}
            </span>
          )}
          {sub && (
            <span className="inline-flex items-center rounded-full bg-surface-2 px-2 py-0.5 font-mono text-[10px] md:text-[11px] text-muted-foreground">
              {sub}
            </span>
          )}
        </div>
      )}
    </Card>
  );
}

/* ---- Buying power card ---------------------------------------------------- */
export function BuyingPowerCard({
  total,
  used,
  remaining,
  pctUsed,
}: {
  total: number;
  used: number;
  remaining: number;
  pctUsed: number;
}) {
  void used;
  const barColor = pctUsed > 80 ? "bg-loss" : pctUsed > 60 ? "bg-warn" : "bg-gain";
  return (
    <Card className="p-3.5 md:p-4">
      <div className="text-[11px] uppercase tracking-[0.06em] text-muted-foreground truncate">Buying power</div>
      <div className="mt-1.5 md:mt-2 font-mono text-[20px] md:text-[26px] font-semibold leading-none tabular-nums">
        {fmtMoney0(remaining)}
      </div>
      <div className="mt-2.5 h-1 overflow-hidden rounded-full bg-surface-2">
        <div className={cn("h-full rounded-full transition-[width]", barColor)} style={{ width: `${Math.min(pctUsed, 100)}%` }} />
      </div>
      <div className="mt-1.5 flex justify-between font-mono text-[10px] md:text-[11px]">
        <span className="text-muted-foreground">{pctUsed.toFixed(0)}% used</span>
        <span className="text-text-faint">{fmtMoney0(total)} cap</span>
      </div>
    </Card>
  );
}

/* ---- IV-rank heat bar ----------------------------------------------------- */
export function IvrHeat({ ivr }: { ivr: number | null }) {
  const style =
    ivr == null
      ? { background: "hsl(var(--surface-2))", color: "hsl(var(--text-faint))" }
      : { background: `hsl(var(--brand) / ${Math.max(0.12, ivr / 100).toFixed(2)})`, color: ivr > 45 ? "#fff" : "hsl(var(--muted-foreground))" };
  return (
    <div className="flex items-center gap-2.5">
      <span
        className="min-w-[30px] rounded-md px-2 py-0.5 text-center font-mono text-xs font-semibold"
        style={style}
      >
        {ivr == null ? "—" : Math.round(ivr)}
      </span>
      <span className="h-1.5 w-[70px] overflow-hidden rounded-full bg-surface-2">
        <span className="block h-full rounded-full bg-brand" style={{ width: `${ivr ?? 0}%` }} />
      </span>
    </div>
  );
}

/* ---- Profit-taken bar (closed positions) ---------------------------------- */
export function ProfitBar({ pct }: { pct: number }) {
  const abs = Math.abs(pct);
  const isPos = pct >= 0;
  return (
    <div className="flex items-center gap-2">
      <span className={cn("min-w-[42px] font-mono text-xs font-semibold", isPos ? "text-gain" : "text-loss")}>
        {pct > 0 ? "+" : pct < 0 ? "−" : ""}
        {abs.toFixed(0)}%
      </span>
      <div className="h-[5px] w-[60px] overflow-hidden rounded-full bg-surface-2">
        <div className={cn("h-full rounded-full", isPos ? "bg-gain" : "bg-loss")} style={{ width: `${Math.min(abs, 100)}%` }} />
      </div>
    </div>
  );
}

/* ---- Position lifecycle timeline ----------------------------------------- */
const EVENT_DOT: Record<string, string> = {
  planned: "bg-text-faint",
  working: "bg-info",
  open: "bg-gain",
  managed: "bg-warn",
  rolled: "bg-warn",
  closed: "bg-muted-foreground",
  canceled: "bg-loss",
  rejected: "bg-loss",
};
const EVENT_TITLE: Record<string, string> = {
  planned: "Planned",
  working: "Working",
  open: "Filled",
  managed: "Managed",
  rolled: "Rolled",
  closed: "Closed",
  canceled: "Canceled",
  rejected: "Rejected",
};

/** Build a timeline from the trade's events; fall back to its timestamps for
 *  trades created before the event log existed. */
function deriveEvents(t: Trade): TradeEvent[] {
  if (t.events && t.events.length) return t.events;
  const out: TradeEvent[] = [];
  if (t.created_at) out.push({ ts: t.created_at, kind: "working", detail: "Order working" });
  if (t.opened_at) out.push({ ts: t.opened_at, kind: "open", detail: "Filled — position open" });
  if (t.closed_at) out.push({ ts: t.closed_at, kind: "closed", detail: t.exit_reason || "Closed" });
  return out;
}

export function PositionTimeline({ trade }: { trade: Trade }) {
  const events = deriveEvents(trade);
  if (!events.length) return <Empty>No lifecycle events yet.</Empty>;
  return (
    <div className="flex flex-col">
      {events.map((e, i) => (
        <div key={i} className="flex gap-3.5">
          <div className="flex w-3 flex-col items-center">
            <span className={cn("size-2.5 shrink-0 rounded-full border-2 border-background", EVENT_DOT[e.kind] || "bg-text-faint")} />
            {i < events.length - 1 && <span className="my-0.5 min-h-3 w-0.5 flex-1 bg-border" />}
          </div>
          <div className="pb-3.5">
            <div className={cn("text-xs text-text-faint", num)}>{formatLocalDateTime(e.ts)}</div>
            <div className="text-[13px]">
              <span className="font-medium">
                {trade.symbol} — {EVENT_TITLE[e.kind] || e.kind}
              </span>
              <span className="text-muted-foreground"> — Status {e.kind}</span>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

/* ---- Page header ---------------------------------------------------------- */
export function PageHeader({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="mb-5">
      <h1 className="text-[24px] font-semibold tracking-[-0.01em]">{title}</h1>
      {children && <p className="mt-1 max-w-[720px] text-sm leading-relaxed text-muted-foreground">{children}</p>}
    </div>
  );
}

/* ---- States --------------------------------------------------------------- */
export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2.5 py-16 text-sm text-muted-foreground">
      <Loader2 className="size-4 animate-spin" />
      {label}
    </div>
  );
}

export function ErrorNote({ msg }: { msg: string }) {
  return (
    <div className="mb-4 flex items-center gap-2.5 rounded-[10px] border border-loss/40 bg-loss-soft px-4 py-3 text-[13px] text-loss">
      <TriangleAlert className="size-4" />
      {msg}
    </div>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="py-8 text-center text-[13px] text-text-faint">{children}</div>;
}

/* ---- Pagination ----------------------------------------------------------- */
export function usePagination<T>(items: T[], pageSize = 8) {
  const [page, setPage] = useState(0);
  useEffect(() => setPage(0), [items.length]);
  const pageCount = Math.max(1, Math.ceil(items.length / pageSize));
  const safe = Math.min(page, pageCount - 1);
  const pageItems = items.slice(safe * pageSize, (safe + 1) * pageSize);
  return { page: safe, setPage, pageItems, pageCount };
}

export function Pagination({
  page,
  pageCount,
  onPage,
}: {
  page: number;
  pageCount: number;
  onPage: (p: number) => void;
}) {
  if (pageCount <= 1) return null;
  return (
    <div className="mt-1 flex items-center justify-center gap-3 border-t border-border pt-3.5">
      <Button variant="outline" size="sm" onClick={() => onPage(page - 1)} disabled={page === 0}>
        <ChevronLeft className="size-4" /> Prev
      </Button>
      <span className="font-mono text-xs text-text-faint">
        {page + 1} of {pageCount}
      </span>
      <Button variant="outline" size="sm" onClick={() => onPage(page + 1)} disabled={page === pageCount - 1}>
        Next <ChevronRight className="size-4" />
      </Button>
    </div>
  );
}
