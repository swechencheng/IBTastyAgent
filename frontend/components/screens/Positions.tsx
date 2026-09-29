"use client";

import { Fragment, useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { ChevronDown, ChevronUp, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { fetcher, Trade, fmtMoney0, fmtMoneySigned, syncPnl } from "@/lib/api";
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
                      <TableHead className="text-right">Credit</TableHead>
                      <TableHead className="text-right">PoP</TableHead>
                      <TableHead className="text-right">Unreal.</TableHead>
                      <TableHead className="text-right">DTE</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead />
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {openPg.pageItems.map((t) => (
                      <Fragment key={t.id}>
                        <TableRow className="cursor-pointer" onClick={() => toggle(t.id)}>
                          <TableCell className="font-semibold">{t.symbol}</TableCell>
                          <TableCell className="text-muted-foreground">{t.strategy}</TableCell>
                          <TableCell className={cn("text-right", num)}>{t.contracts}</TableCell>
                          <TableCell className={cn("text-right", num)}>{fmtMoney0(t.entry_credit)}</TableCell>
                          <TableCell className={cn("text-right", num)}>{pop(t.probability_of_profit)}</TableCell>
                          <TableCell className={cn("text-right", num, signClass(t.unrealized_pnl))}>{fmtMoneySigned(t.unrealized_pnl)}</TableCell>
                          <TableCell className={cn("text-right", num, t.dte_at_entry <= 22 && "text-warn")}>{t.dte_at_entry}</TableCell>
                          <TableCell>
                            <Badge variant={t.status === "open" ? "open" : "warn"}>{t.status}</Badge>
                          </TableCell>
                          <TableCell>{expanded === t.id ? <ChevronUp className="size-4" /> : <ChevronDown className="size-4" />}</TableCell>
                        </TableRow>
                        {expanded === t.id && (
                          <TableRow className="hover:bg-surface-2">
                            <TableCell colSpan={9} className="bg-surface-2 p-4">
                              <div className="mb-3 text-[13px] leading-relaxed">
                                <span className="text-xs text-muted-foreground">Rationale · </span>
                                {t.rationale}
                              </div>
                              <div className="mb-2 flex items-center gap-2 text-[11px] uppercase tracking-[0.05em] text-text-faint">
                                <span className="size-1.5 animate-pulse rounded-full bg-gain" />
                                Live timeline
                              </div>
                              <PositionTimeline trade={t} />
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
