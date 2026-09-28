"use client";

import { useState } from "react";
import useSWR from "swr";
import { ChevronDown, ChevronUp } from "lucide-react";

import { ActivityItem, ActivityTrade, fetcher, fmtMoney0, fmtMoneySigned } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Empty, ErrorNote, Loading, PageHeader, num, pop } from "@/components/common";
import { cn, formatLocalDateTime } from "@/lib/utils";

const POLL = { refreshInterval: 8000 };

const toneDot: Record<string, string> = {
  placed: "bg-gain",
  rejected: "bg-loss",
  managed: "bg-warn",
};
const toneText: Record<string, string> = {
  placed: "text-gain",
  rejected: "text-loss",
  managed: "text-warn",
};

function TradeTable({ kind, trades }: { kind: string; trades: ActivityTrade[] }) {
  if (!trades || trades.length === 0) {
    const empty: Record<string, string> = {
      planned: "Nothing planned this cycle.",
      placed: "No trades placed.",
      rejected: "No rejections.",
      managed: "No management actions.",
    };
    return <div className="py-2.5 text-[13px] text-text-faint">{empty[kind]}</div>;
  }
  const showReason = kind === "rejected";
  const showManaged = kind === "managed";
  return (
    <Table className="mt-0.5">
      <TableHeader>
        <TableRow>
          <TableHead>Symbol</TableHead>
          <TableHead>Strategy</TableHead>
          {showReason ? (
            <TableHead>Reason</TableHead>
          ) : showManaged ? (
            <>
              <TableHead>Action</TableHead>
              <TableHead className="text-right">Realized</TableHead>
            </>
          ) : (
            <>
              <TableHead className="text-right">Qty</TableHead>
              <TableHead className="text-right">Credit</TableHead>
              <TableHead className="text-right">PoP</TableHead>
            </>
          )}
        </TableRow>
      </TableHeader>
      <TableBody>
        {trades.map((t, i) => (
          <TableRow key={i}>
            <TableCell className="font-semibold">{t.symbol}</TableCell>
            <TableCell className="text-muted-foreground">{t.strategy}</TableCell>
            {showReason ? (
              <TableCell className="text-xs text-muted-foreground">{t.detail || "—"}</TableCell>
            ) : showManaged ? (
              <>
                <TableCell className="text-xs text-muted-foreground">{t.detail || "—"}</TableCell>
                <TableCell className={cn("text-right", num, (t.realized_pnl ?? 0) >= 0 ? "text-gain" : "text-loss")}>
                  {t.realized_pnl == null ? "—" : fmtMoneySigned(t.realized_pnl)}
                </TableCell>
              </>
            ) : (
              <>
                <TableCell className={cn("text-right", num)}>{t.contracts}</TableCell>
                <TableCell className={cn("text-right", num)}>{fmtMoney0(t.credit)}</TableCell>
                <TableCell className={cn("text-right", num)}>{pop(t.pop)}</TableCell>
              </>
            )}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

function Cycle({ a, defaultOpen }: { a: ActivityItem; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const [tab, setTab] = useState("placed");

  const tabs = [
    { id: "planned", label: `Planned · ${a.planned_trades.length}`, trades: a.planned_trades },
    { id: "placed", label: `Placed · ${a.placed_trades.length}`, trades: a.placed_trades },
    { id: "rejected", label: `Rejected · ${a.rejected_trades.length}`, trades: a.rejected_trades },
    { id: "managed", label: `Managed · ${a.managed_trades.length}`, trades: a.managed_trades },
  ];

  return (
    <div className="flex gap-4">
      <div className="flex flex-col items-center pt-1.5">
        <span className="z-[1] size-[11px] rounded-full border-2 border-background bg-brand" />
        <span className="mt-1 w-0.5 flex-1 bg-border" />
      </div>
      <div className="flex-1 pb-6">
        <div className="flex cursor-pointer flex-wrap items-center justify-between gap-4" onClick={() => setOpen(!open)}>
          <div className={cn("flex items-center gap-2.5 text-[13px] font-semibold", num)}>
            {formatLocalDateTime(a.created_at)}
            <Badge variant="default" className="text-[11px]">{a.mode}</Badge>
          </div>
          <div className="flex items-center gap-3.5 text-xs text-muted-foreground">
            <span><b className={cn("text-foreground", num)}>{a.considered}</b> considered</span>
            <span className="text-gain"><b className={num}>{a.placed}</b> placed</span>
            <span className="text-loss"><b className={num}>{a.rejected}</b> rejected</span>
            <span className="text-warn"><b className={num}>{a.managed}</b> managed</span>
            {open ? <ChevronUp className="size-4" /> : <ChevronDown className="size-4" />}
          </div>
        </div>

        {a.commentary && <div className="mt-2 max-w-[760px] text-[13px] leading-relaxed text-muted-foreground">{a.commentary}</div>}

        {open && (
          <div className="mt-3.5">
            {/* Per-ticker reasoning bullets */}
            {a.reasoning.length > 0 && (
              <div className="mb-4">
                <div className="mb-2 text-[11px] uppercase tracking-[0.05em] text-text-faint">Reasoning by symbol</div>
                <ul className="flex flex-col gap-1.5">
                  {a.reasoning.map((r, i) => (
                    <li key={i} className="flex gap-2.5 text-[13px] leading-relaxed">
                      <span className={cn("mt-[7px] size-1.5 shrink-0 rounded-full", toneDot[r.tone])} />
                      <span>
                        <span className={cn("font-semibold", num, toneText[r.tone])}>{r.symbol}</span>
                        <span className="text-muted-foreground"> — {r.text}</span>
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {/* Universe / symbols placed */}
            {a.symbols.length > 0 && (
              <div className="mb-4">
                <div className="mb-2 text-[11px] uppercase tracking-[0.05em] text-text-faint">Symbols placed</div>
                <div className="flex flex-wrap gap-1.5">
                  {a.symbols.map((sym) => (
                    <span key={sym} className={cn("rounded-md border border-border bg-surface-2 px-2.5 py-0.5 text-xs", num)}>{sym}</span>
                  ))}
                </div>
              </div>
            )}

            {/* Trade breakdown tabs */}
            <Tabs value={tab} onValueChange={setTab}>
              <TabsList className="mb-2 flex-wrap">
                {tabs.map((t) => (
                  <TabsTrigger key={t.id} value={t.id}>{t.label}</TabsTrigger>
                ))}
              </TabsList>
              {tabs.map((t) => (tab === t.id ? <TradeTable key={t.id} kind={t.id} trades={t.trades} /> : null))}
            </Tabs>
          </div>
        )}
      </div>
    </div>
  );
}

export default function Activity() {
  const { data, error } = useSWR<ActivityItem[]>("/api/activity", fetcher, POLL);

  if (error) return <ErrorNote msg="Could not load the activity log." />;
  if (!data) return <Loading />;

  return (
    <div className="max-w-[1080px]">
      <PageHeader title="Activity">
        Every decision cycle — LLM reasoning by ticker and the exact trades planned, placed, rejected, and managed.
      </PageHeader>

      {data.length === 0 ? (
        <Empty>No decision cycles yet. Run one from the sidebar.</Empty>
      ) : (
        <div className="flex flex-col">
          {data.map((a, i) => (
            <Cycle key={a.id} a={a} defaultOpen={i === 0} />
          ))}
        </div>
      )}
    </div>
  );
}
