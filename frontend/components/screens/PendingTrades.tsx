"use client";

import useSWR, { useSWRConfig } from "swr";
import { Inbox } from "lucide-react";
import { toast } from "sonner";

import { approveTrade, fetcher, Pnl, rejectTrade, Settings, Trade, fmtMoney0 } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { BuyingPowerCard, ErrorNote, Kpi, Loading, PageHeader, Pagination, num, pop, usePagination } from "@/components/common";
import { cn } from "@/lib/utils";

const POLL = { refreshInterval: 6000 };

export default function PendingTrades() {
  const { mutate } = useSWRConfig();
  const approvals = useSWR<Trade[]>("/api/approvals", fetcher, POLL);
  const positions = useSWR<Trade[]>("/api/positions", fetcher, POLL);
  const pnl = useSWR<Pnl>("/api/pnl", fetcher, POLL);
  const settings = useSWR<Settings>("/api/settings", fetcher, POLL);

  const aps = approvals.data || [];
  const pg = usePagination(aps, 8);

  const refresh = () => ["/api/approvals", "/api/positions", "/api/pnl", "/api/status"].forEach((k) => mutate(k));

  if (approvals.error) return <ErrorNote msg="Could not load the approval queue." />;
  if (!approvals.data) return <Loading />;

  const cap =
    settings.data?.use_custom_working_capital === false && settings.data?.account_cash_usd != null
      ? settings.data.account_cash_usd
      : (settings.data?.working_capital ?? pnl.data?.starting_capital ?? 10000);
  const totalBpPct = Number(settings.data?.risk?.max_total_bp_pct ?? 0.4);
  const total = cap * totalBpPct;
  const used = (positions.data || []).reduce((s, t) => s + (t.buying_power || 0), 0);
  const pendingBp = aps.reduce((s, t) => s + (t.buying_power || 0), 0);

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
      <PageHeader title="Pending Trades">
        {aps.length > 0
          ? `${aps.length} trade${aps.length > 1 ? "s" : ""} waiting for your approval.`
          : "Trades appear here when the agent runs in live-approval mode."}
      </PageHeader>

      <div className="mb-[18px] flex flex-wrap gap-3.5 [&>*]:max-w-[260px] [&>*]:flex-1 [&>*]:basis-[180px]">
        <BuyingPowerCard total={total} used={used} remaining={Math.max(0, total - used)} pctUsed={total > 0 ? (used / total) * 100 : 0} />
        <Kpi label="BP if all approved" value={fmtMoney0(Math.max(0, total - used - pendingBp))} delta="estimated impact" deltaTone="down" />
        <Kpi label="Pending" value={String(aps.length)} delta="awaiting you" deltaTone="flat" />
      </div>

      {aps.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-center text-muted-foreground">
          <Inbox className="mb-3 size-10 text-text-faint" />
          <div className="text-[15px]">No pending trades.</div>
          <div className="mt-1.5 text-[13px] text-text-faint">Switch to live-approval mode to review trades here.</div>
        </div>
      ) : (
        <>
          <div className="space-y-3">
            {pg.pageItems.map((t) => (
              <Card key={t.id} className="border-l-[3px] border-l-warn bg-surface-2 px-4 py-3.5">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="text-[15px] font-semibold">{t.symbol} · {t.strategy}</div>
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
              </Card>
            ))}
          </div>
          <Pagination page={pg.page} pageCount={pg.pageCount} onPage={pg.setPage} />
        </>
      )}
    </div>
  );
}
