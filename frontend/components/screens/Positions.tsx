"use client";

import { Fragment, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { ChevronDown, ChevronUp, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import {
  fetcher,
  Trade,
  fmtMoney0,
  fmtMoneySigned,
  fmtPrice,
  fmtPctSigned,
  fmtSignedDelta,
  syncPnl,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  Empty,
  ErrorNote,
  Loading,
  PageHeader,
  Pagination,
  PositionTimeline,
  ProfitBar,
  num,
  pop,
  signClass,
  usePagination,
} from "@/components/common";
import { cn } from "@/lib/utils";

const POLL = { refreshInterval: 8000 };

export default function Positions() {
  const { mutate } = useSWRConfig();
  const [tab, setTab] = useState("open");
  const [expanded, setExpanded] = useState<number | null>(null);
  const [syncing, setSyncing] = useState(false);

  const open = useSWR<Trade[]>("/api/positions", fetcher, POLL);
  const closed = useSWR<Trade[]>("/api/trades/closed", fetcher, POLL);

  const openList = open.data || [];
  const closedList = closed.data || [];
  const openPg = usePagination(openList, 8);
  const closedPg = usePagination(closedList, 8);
  const toggle = (id: number) => setExpanded(expanded === id ? null : id);

  const handleSync = async () => {
    setSyncing(true);
    try {
      const res = await syncPnl();
      mutate("/api/positions");
      mutate("/api/pnl");
      mutate("/api/trades");
      toast.success("Marks synced from IBKR", {
        description: `${res.marks_updated} position(s) updated · Unrealized: ${fmtMoneySigned(res.unrealized_pnl)}`,
      });
    } catch {
      toast.error("Could not sync marks with IBKR");
    } finally {
      setSyncing(false);
    }
  };

  if (open.error) return <ErrorNote msg="Could not load positions." />;
  if (!open.data || !closed.data) return <Loading />;

  return (
    <div className="max-w-[1080px]">
      <PageHeader title="Positions">
        <Button
          variant="outline"
          size="sm"
          onClick={handleSync}
          disabled={syncing}
          className="gap-1.5 text-xs font-medium"
        >
          <RefreshCw className={cn("size-3.5", syncing && "animate-spin")} />
          {syncing ? "Syncing…" : "Sync Marks"}
        </Button>
      </PageHeader>
      <Tabs value={tab} onValueChange={(v) => { setTab(v); setExpanded(null); }} className="mb-[18px]">
        <TabsList>
          <TabsTrigger value="open">Open · {openList.length}</TabsTrigger>
          <TabsTrigger value="closed">Closed · {closedList.length}</TabsTrigger>
        </TabsList>
      </Tabs>

      {tab === "open" ? (
        <Card>
          <CardContent className="p-[18px]">
            {openList.length === 0 ? (
              <Empty>No open positions.</Empty>
            ) : (
              <>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Symbol</TableHead>
                      <TableHead>Strategy</TableHead>
                      <TableHead className="text-right">Qty</TableHead>
                      <TableHead className="text-right">Avg Price</TableHead>
                      <TableHead className="text-right">Mkt Price</TableHead>
                      <TableHead className="text-right">Change %</TableHead>
                      <TableHead className="text-right">P&L %</TableHead>
                      <TableHead className="text-right">Unreal. P&L</TableHead>
                      <TableHead className="text-right">Delta</TableHead>
                      <TableHead className="text-right">DTE</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead className="w-8" />
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {openPg.pageItems.map((t) => (
                      <Fragment key={t.id}>
                        <TableRow className="cursor-pointer hover:bg-surface-2/50 transition-colors" onClick={() => toggle(t.id)}>
                          <TableCell className="font-semibold text-foreground">{t.symbol}</TableCell>
                          <TableCell className="text-muted-foreground">{t.strategy}</TableCell>
                          <TableCell className={cn("text-right font-medium", num)}>{t.contracts}</TableCell>
                          <TableCell className={cn("text-right", num)}>{fmtPrice(t.average_price)}</TableCell>
                          <TableCell className={cn("text-right", num)}>{fmtPrice(t.market_price)}</TableCell>
                          <TableCell className={cn("text-right", num, signClass(t.change_pct ?? 0))}>
                            {fmtPctSigned(t.change_pct, true)}
                          </TableCell>
                          <TableCell className={cn("text-right font-medium", num, signClass(t.pnl_pct ?? 0))}>
                            {fmtPctSigned(t.pnl_pct, true)}
                          </TableCell>
                          <TableCell className={cn("text-right font-semibold", num, signClass(t.unrealized_pnl))}>
                            {fmtMoneySigned(t.unrealized_pnl)}
                          </TableCell>
                          <TableCell className={cn("text-right", num, "text-muted-foreground")}>
                            {fmtSignedDelta(t.net_delta)}
                          </TableCell>
                          <TableCell className={cn("text-right", num, t.dte_at_entry <= 22 && "text-warn")}>
                            {t.dte_at_entry}
                          </TableCell>
                          <TableCell>
                            <Badge variant={t.status === "open" ? "open" : "warn"}>{t.status}</Badge>
                          </TableCell>
                          <TableCell className="text-right pr-2">
                            {expanded === t.id ? (
                              <ChevronUp className="size-4 text-muted-foreground inline" />
                            ) : (
                              <ChevronDown className="size-4 text-muted-foreground inline" />
                            )}
                          </TableCell>
                        </TableRow>
                        {expanded === t.id && (
                          <TableRow className="hover:bg-transparent">
                            <TableCell colSpan={12} className="bg-surface-2/40 p-4 border-t border-b border-border/40">
                              <div className="space-y-4">
                                {/* Option Legs Breakdown Card */}
                                <div className="rounded-lg border border-border/70 bg-card p-4 shadow-sm">
                                  <div className="mb-3 flex flex-wrap items-center justify-between gap-2 border-b border-border/40 pb-2.5">
                                    <div className="flex items-center gap-2">
                                      <span className="text-[12px] font-semibold uppercase tracking-[0.06em] text-foreground">
                                        Option Legs · {t.symbol}
                                      </span>
                                      <span className="rounded bg-surface-2 px-1.5 py-0.5 text-[10px] font-mono text-muted-foreground">
                                        {t.legs?.length || 0} {t.legs?.length === 1 ? "leg" : "legs"}
                                      </span>
                                    </div>
                                    <div className="flex items-center gap-4 text-xs">
                                      <span className="text-muted-foreground">
                                        Combo Avg: <strong className={cn(num, "text-foreground")}>{fmtPrice(t.average_price)}</strong>
                                      </span>
                                      <span className="text-muted-foreground">
                                        Combo Mkt: <strong className={cn(num, "text-foreground")}>{fmtPrice(t.market_price)}</strong>
                                      </span>
                                      <span className="text-muted-foreground">
                                        Net Delta: <strong className={cn(num, "text-foreground")}>{fmtSignedDelta(t.net_delta)}</strong>
                                      </span>
                                      <span className="text-muted-foreground">
                                        Unrealized: <strong className={cn(num, signClass(t.unrealized_pnl))}>{fmtMoneySigned(t.unrealized_pnl)}</strong>
                                      </span>
                                    </div>
                                  </div>

                                  <div className="overflow-x-auto">
                                    <Table>
                                      <TableHeader className="bg-surface-2/50 text-[11px]">
                                        <TableRow className="border-border/40 hover:bg-transparent">
                                          <TableHead className="py-2 font-medium">Leg (OCC Symbol)</TableHead>
                                          <TableHead className="py-2 text-right font-medium">Pos Size</TableHead>
                                          <TableHead className="py-2 text-right font-medium">Avg Price</TableHead>
                                          <TableHead className="py-2 text-right font-medium">Mkt Price</TableHead>
                                          <TableHead className="py-2 text-right font-medium">Change %</TableHead>
                                          <TableHead className="py-2 text-right font-medium">P&L %</TableHead>
                                          <TableHead className="py-2 text-right font-medium">Unreal. P&L</TableHead>
                                          <TableHead className="py-2 text-right font-medium">Delta</TableHead>
                                        </TableRow>
                                      </TableHeader>
                                      <TableBody>
                                        {t.legs && t.legs.length > 0 ? (
                                          t.legs.map((leg, idx) => {
                                            const isShort =
                                              (leg.position_size ?? 0) < 0 ||
                                              leg.action.toLowerCase().includes("sell");
                                            return (
                                              <TableRow
                                                key={idx}
                                                className="border-border/30 hover:bg-surface-2/30 text-xs"
                                              >
                                                <TableCell className="py-2.5 font-mono font-medium text-foreground">
                                                  {leg.occ_symbol ||
                                                    `${t.symbol} ${leg.strike}${leg.option_type[0].toUpperCase()}`}
                                                </TableCell>
                                                <TableCell
                                                  className={cn(
                                                    "py-2.5 text-right font-mono font-semibold",
                                                    num,
                                                    isShort ? "text-loss" : "text-gain"
                                                  )}
                                                >
                                                  {leg.position_size != null
                                                    ? leg.position_size > 0
                                                      ? `+${leg.position_size}`
                                                      : leg.position_size
                                                    : `${isShort ? "-" : "+"}${leg.quantity * t.contracts}`}
                                                </TableCell>
                                                <TableCell className={cn("py-2.5 text-right", num)}>
                                                  {fmtPrice(leg.average_price)}
                                                </TableCell>
                                                <TableCell className={cn("py-2.5 text-right", num)}>
                                                  {fmtPrice(leg.market_price)}
                                                </TableCell>
                                                <TableCell
                                                  className={cn(
                                                    "py-2.5 text-right",
                                                    num,
                                                    signClass(leg.change_pct ?? 0)
                                                  )}
                                                >
                                                  {fmtPctSigned(leg.change_pct, true)}
                                                </TableCell>
                                                <TableCell
                                                  className={cn(
                                                    "py-2.5 text-right font-medium",
                                                    num,
                                                    signClass(leg.pnl_pct ?? 0)
                                                  )}
                                                >
                                                  {fmtPctSigned(leg.pnl_pct, true)}
                                                </TableCell>
                                                <TableCell
                                                  className={cn(
                                                    "py-2.5 text-right",
                                                    num,
                                                    signClass(leg.unrealized_pnl ?? 0)
                                                  )}
                                                >
                                                  {fmtMoneySigned(leg.unrealized_pnl)}
                                                </TableCell>
                                                <TableCell
                                                  className={cn(
                                                    "py-2.5 text-right",
                                                    num,
                                                    "text-muted-foreground"
                                                  )}
                                                >
                                                  {fmtSignedDelta(leg.delta)}
                                                </TableCell>
                                              </TableRow>
                                            );
                                          })
                                        ) : (
                                          <TableRow>
                                            <TableCell
                                              colSpan={8}
                                              className="py-3 text-center text-xs text-muted-foreground"
                                            >
                                              No leg details available.
                                            </TableCell>
                                          </TableRow>
                                        )}
                                      </TableBody>
                                    </Table>
                                  </div>
                                </div>

                                {/* Rationale & Timeline Section */}
                                <div className="rounded-lg border border-border/40 bg-card/60 p-3.5 space-y-3">
                                  {t.rationale && (
                                    <div className="text-[13px] leading-relaxed text-muted-foreground">
                                      <span className="text-xs font-semibold uppercase tracking-wider text-foreground">
                                        Rationale ·{" "}
                                      </span>
                                      {t.rationale}
                                    </div>
                                  )}
                                  <div>
                                    <div className="mb-2 flex items-center gap-2 text-[11px] uppercase tracking-[0.05em] text-text-faint">
                                      <span className="size-1.5 animate-pulse rounded-full bg-gain" />
                                      Live position timeline
                                    </div>
                                    <PositionTimeline trade={t} />
                                  </div>
                                </div>
                              </div>
                            </TableCell>
                          </TableRow>
                        )}
                      </Fragment>
                    ))}
                  </TableBody>
                </Table>
                <Pagination page={openPg.page} pageCount={openPg.pageCount} onPage={openPg.setPage} />
              </>
            )}
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="p-[18px]">
            {closedList.length === 0 ? (
              <Empty>No closed trades yet.</Empty>
            ) : (
              <>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Symbol</TableHead>
                      <TableHead>Strategy</TableHead>
                      <TableHead className="text-right">PoP</TableHead>
                      <TableHead className="text-right">Realized</TableHead>
                      <TableHead>% Taken</TableHead>
                      <TableHead>Result</TableHead>
                      <TableHead>Exit reason</TableHead>
                      <TableHead />
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {closedPg.pageItems.map((t) => {
                      const taken = t.entry_credit > 0 ? ((t.realized_pnl ?? 0) / t.entry_credit) * 100 : 0;
                      return (
                        <Fragment key={t.id}>
                          <TableRow className="cursor-pointer" onClick={() => toggle(t.id)}>
                            <TableCell className="font-semibold">{t.symbol}</TableCell>
                            <TableCell className="text-muted-foreground">{t.strategy}</TableCell>
                            <TableCell className={cn("text-right", num)}>{pop(t.probability_of_profit)}</TableCell>
                            <TableCell className={cn("text-right", num, t.is_win ? "text-gain" : "text-loss")}>{fmtMoneySigned(t.realized_pnl ?? 0)}</TableCell>
                            <TableCell><ProfitBar pct={taken} /></TableCell>
                            <TableCell><Badge variant={t.is_win ? "gain" : "loss"}>{t.is_win ? "WIN" : "LOSS"}</Badge></TableCell>
                            <TableCell className="text-xs text-muted-foreground">{t.exit_reason || "—"}</TableCell>
                            <TableCell>{expanded === t.id ? <ChevronUp className="size-4" /> : <ChevronDown className="size-4" />}</TableCell>
                          </TableRow>
                          {expanded === t.id && (
                            <TableRow className="hover:bg-surface-2">
                              <TableCell colSpan={8} className="bg-surface-2 p-4">
                                <div className="mb-3 text-[13px] leading-relaxed">
                                  <span className="text-xs text-muted-foreground">Rationale · </span>
                                  {t.rationale}
                                </div>
                                <div className="mb-2 text-[11px] uppercase tracking-[0.05em] text-text-faint">Trade timeline</div>
                                <PositionTimeline trade={t} />
                              </TableCell>
                            </TableRow>
                          )}
                        </Fragment>
                      );
                    })}
                  </TableBody>
                </Table>
                <Pagination page={closedPg.page} pageCount={closedPg.pageCount} onPage={closedPg.setPage} />
              </>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
