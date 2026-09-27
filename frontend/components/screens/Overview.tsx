"use client";

import useSWR, { useSWRConfig } from "swr";
import { Bell, TrendingUp, ArrowRight } from "lucide-react";
import { toast } from "sonner";

import {
  approveTrade,
  Benchmark,
  fetcher,
  Pnl,
  rejectTrade,
  Settings,
  Trade,
  ActivityItem,
  fmtMoney0,
  fmtMoneySigned,
  fmtPctSigned,
} from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import EquityChart from "@/components/EquityChart";
import { BuyingPowerCard, Empty, ErrorNote, Kpi, Loading, PageHeader, num, pop, signClass } from "@/components/common";
import { cn } from "@/lib/utils";

const POLL = { refreshInterval: 8000 };

function formatActivityTime(iso: string) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return { date: iso, time: "" };
  return {
    date: d.toLocaleDateString(undefined, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }),
    time: d.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }),
  };
}

export default function Overview({ goTo }: { goTo: (r: string) => void }) {
  const { mutate } = useSWRConfig();
  const pnl = useSWR<Pnl>("/api/pnl", fetcher, POLL);
  const benchmark = useSWR<Benchmark>("/api/benchmark", fetcher, POLL);
  const approvals = useSWR<Trade[]>("/api/approvals", fetcher, POLL);
  const positions = useSWR<Trade[]>("/api/positions", fetcher, POLL);
  const activity = useSWR<ActivityItem[]>("/api/activity", fetcher, POLL);
  const settings = useSWR<Settings>("/api/settings", fetcher, POLL);

  const refresh = () =>
    ["/api/pnl", "/api/approvals", "/api/positions", "/api/activity", "/api/status"].forEach((k) => mutate(k));

  if (pnl.error) return <ErrorNote msg="Could not reach the API. Is the backend running on :8000?" />;
  if (!pnl.data) return <Loading />;

  const p = pnl.data;
  const aps = approvals.data || [];
  const pos = positions.data || [];

  const cap =
    settings.data?.use_custom_working_capital === false && settings.data?.account_cash_usd != null
      ? settings.data.account_cash_usd
      : (settings.data?.working_capital ?? p.starting_capital);
  const totalBpPct = Number(settings.data?.risk?.max_total_bp_pct ?? 0.4);
  const total = cap * totalBpPct;
  const used = pos.reduce((s, t) => s + (t.buying_power || 0), 0);

  const onApprove = async (t: Trade) => {
    try {
      await approveTrade(t.id);
      toast.success(`${t.symbol} ${t.strategy} approved — sending to broker`);
      refresh();
    } catch {
      toast.error(`Could not approve ${t.symbol}`);
    }
  };
  const onReject = async (t: Trade) => {
    try {
      await rejectTrade(t.id);
      toast.error(`${t.symbol} rejected`);
      refresh();
    } catch {
      toast.error(`Could not reject ${t.symbol}`);
    }
  };

  return (
    <div className="max-w-[1080px]">
      <PageHeader title="Overview" />

      <div className="mb-[18px] grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-6 sm:gap-3.5">
        <Kpi label="Total P/L" value={fmtMoneySigned(p.total_pnl)} delta={fmtPctSigned(p.profit_pct)} deltaTone={p.total_pnl >= 0 ? "up" : "down"} />
        <Kpi label="Realized" value={fmtMoneySigned(p.realized_pnl)} delta="closed" deltaTone={p.realized_pnl >= 0 ? "up" : "down"} />
        <Kpi label="Unrealized" value={fmtMoneySigned(p.unrealized_pnl)} delta="open" deltaTone={p.unrealized_pnl >= 0 ? "up" : "down"} />
        <Kpi label="Win rate" value={p.win_rate == null ? "—" : `${Math.round(p.win_rate * 100)}%`} sub={`${p.wins}W / ${p.losses}L`} />
        <Kpi label="Open / Closed" value={`${p.open_count} / ${p.closed_count}`} sub={`cap ${fmtMoney0(cap)}`} />
        <BuyingPowerCard total={total} used={used} remaining={Math.max(0, total - used)} pctUsed={total > 0 ? (used / total) * 100 : 0} />
      </div>

      {aps.length > 0 && (
        <Card className="mb-[18px] border-warn/40">
          <CardHeader>
            <CardTitle>
              <Bell className="size-4" /> Needs attention · {aps.length} pending approval
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {aps.map((t) => (
              <div key={t.id} className="rounded-[10px] border border-border border-l-[3px] border-l-warn bg-surface-2 px-4 py-3.5">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="text-[15px] font-semibold">
                      {t.symbol} · {t.strategy}
                    </div>
                    <div className={cn("mt-0.5 text-xs text-muted-foreground", num)}>
                      {t.contracts} contracts · credit {fmtMoney0(t.entry_credit)}
                    </div>
                  </div>
                  <Badge variant="gain">PoP {pop(t.probability_of_profit)}</Badge>
                </div>
                <div className="my-3 text-[13px] leading-relaxed text-muted-foreground">{t.rationale}</div>
                <div className="flex gap-2.5">
                  <Button variant="approve" onClick={() => onApprove(t)}>Approve</Button>
                  <Button variant="reject" onClick={() => onReject(t)}>Reject</Button>
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      <Card className="mb-[18px]">
        <CardHeader>
          <CardTitle>
            <TrendingUp className="size-4" /> Equity vs. S&amp;P 500
          </CardTitle>
        </CardHeader>
        <CardContent>
          {benchmark.data ? <EquityChart data={benchmark.data} /> : <Empty>Equity curve builds as the agent trades.</Empty>}
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 gap-[18px] lg:grid-cols-2">
        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <CardTitle>Open positions</CardTitle>
            <button className="inline-flex items-center gap-1 text-[13px] text-muted-foreground hover:text-foreground" onClick={() => goTo("positions")}>
              View all <ArrowRight className="size-3.5" />
            </button>
          </CardHeader>
          <CardContent>
            {pos.length > 0 ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Symbol</TableHead>
                    <TableHead>Strategy</TableHead>
                    <TableHead className="text-right">PoP</TableHead>
                    <TableHead className="text-right">Unreal.</TableHead>
                    <TableHead className="text-right">DTE</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {pos.slice(0, 5).map((t) => (
                    <TableRow key={t.id}>
                      <TableCell className="font-semibold">{t.symbol}</TableCell>
                      <TableCell className="text-muted-foreground">{t.strategy}</TableCell>
                      <TableCell className={cn("text-right", num)}>{pop(t.probability_of_profit)}</TableCell>
                      <TableCell className={cn("text-right", num, signClass(t.unrealized_pnl))}>{fmtMoneySigned(t.unrealized_pnl)}</TableCell>
                      <TableCell className={cn("text-right", num)}>{t.dte_at_entry}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <Empty>No open positions.</Empty>
            )}
          </CardContent>
        </Card>


        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <CardTitle>Recent activity</CardTitle>
            <button className="inline-flex items-center gap-1 text-[13px] text-muted-foreground hover:text-foreground" onClick={() => goTo("activity")}>
              Log <ArrowRight className="size-3.5" />
            </button>
          </CardHeader>
          <CardContent>
            {(activity.data || []).length > 0 ? (
              <div className="flex flex-col gap-4">
                {(activity.data || []).slice(0, 3).map((a) => {
                  const { date, time } = formatActivityTime(a.created_at);
                  return (
                    <div
                      key={a.id}
                      className="flex items-start gap-3.5 border-b border-border/40 pb-3.5 last:border-b-0 last:pb-0"
                    >
                      <div className="w-[84px] shrink-0 pt-0.5 font-mono tabular-nums leading-tight">
                        <div className="text-xs font-medium text-foreground/80">{date}</div>
                        <div className="text-[11px] text-muted-foreground/70">{time}</div>
                      </div>
                      <div className="min-w-0 flex-1 border-l border-border/50 pl-3.5">
                        <div className="flex flex-wrap items-center gap-1.5 text-[13px]">
                          <span className={cn("font-medium", a.placed > 0 ? "font-semibold text-gain" : "text-foreground")}>
                            +{a.placed} placed
                          </span>
                          <span className="text-muted-foreground/50">·</span>
                          <span className="text-foreground">{a.considered} considered</span>
                          <span className="text-muted-foreground/50">·</span>
                          <span className={cn(a.rejected > 0 ? "font-semibold text-loss" : "text-muted-foreground")}>
                            {a.rejected} rejected
                          </span>
                        </div>
                        {a.commentary && (
                          <div className="mt-1 text-xs leading-relaxed text-muted-foreground">{a.commentary}</div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <Empty>No decision cycles yet. Run one from the sidebar.</Empty>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
