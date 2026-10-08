"use client";

import type { Benchmark } from "@/lib/api";
import { fmtMoney0 } from "@/lib/api";
import { cn } from "@/lib/utils";

/** Clean SVG equity curve — brand-red strategy vs. optional S&P 500 benchmark when custom capital is active. */
export default function EquityChart({
  data,
  useCustomCapital = true,
}: {
  data: Benchmark;
  useCustomCapital?: boolean;
}) {
  const isBenchmarkEnabled = useCustomCapital && data.use_custom_working_capital !== false;
  const rawStrategy = data.strategy_curve || [];
  const rawSp500 = isBenchmarkEnabled ? (data.sp500_curve || []) : [];

  if (!rawStrategy || rawStrategy.length === 0) {
    return <div className="py-8 text-center text-[13px] text-text-faint">Equity curve builds as the agent trades.</div>;
  }

  // If there is only 1 point (e.g. right after reset), synthesize a flat baseline so the starting point can render
  const strategy =
    rawStrategy.length === 1
      ? [rawStrategy[0], { date: "Today", value: rawStrategy[0].value }]
      : rawStrategy;
  const sp500 =
    isBenchmarkEnabled && rawSp500.length === 1
      ? [rawSp500[0], { date: "Today", value: rawSp500[0].value }]
      : isBenchmarkEnabled
      ? rawSp500
      : [];

  const W = 760;
  const H = 240;
  const pad = { t: 12, r: 16, b: 28, l: 52 };

  const all = [...strategy, ...(isBenchmarkEnabled ? sp500 : [])];
  const minVal = Math.min(...all.map((d) => d.value));
  const maxVal = Math.max(...all.map((d) => d.value));
  const diff = maxVal - minVal;
  // If baseline is completely flat, give it a tiny 2% margin so line is centered
  const min = diff === 0 ? minVal * 0.98 : minVal;
  const max = diff === 0 ? maxVal * 1.02 : maxVal;
  const n = strategy.length;

  // Compute X coordinate proportionally based on array length so both curves span full width
  const xFor = (len: number, i: number) =>
    len > 1 ? pad.l + (i / (len - 1)) * (W - pad.l - pad.r) : (pad.l + W - pad.r) / 2;

  const y = (v: number) => {
    const range = max - min || 1;
    return pad.t + (1 - (v - min) / range) * (H - pad.t - pad.b);
  };

  const makeLine = (arr: { value: number }[]) =>
    arr.map((d, i) => `${i ? "L" : "M"}${xFor(arr.length, i).toFixed(1)},${y(d.value).toFixed(1)}`).join(" ");

  const area = `${makeLine(strategy)} L${xFor(n, n - 1)},${H - pad.b} L${xFor(n, 0)},${H - pad.b} Z`;
  const ticks = [min, (min + max) / 2, max];
  const labelStep = Math.max(1, Math.floor(n / 6));
  const outperf = isBenchmarkEnabled ? data.outperformance_pct : null;

  const lastStrat = strategy.length ? strategy[strategy.length - 1].value : null;
  const lastSp = isBenchmarkEnabled && sp500.length ? sp500[sp500.length - 1].value : null;

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-[18px] text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-2">
          <i className="inline-block h-[3px] w-3 rounded-sm bg-brand align-middle" /> IBTastyAgent
          {lastStrat != null && (
            <span className="font-mono text-foreground font-semibold">({fmtMoney0(lastStrat)})</span>
          )}
        </span>
        {isBenchmarkEnabled && (
          <span className="inline-flex items-center gap-2">
            <i className="inline-block h-[3px] w-3 rounded-sm bg-muted-foreground align-middle" /> S&amp;P 500
            {lastSp != null && (
              <span className="font-mono text-muted-foreground font-medium">({fmtMoney0(lastSp)})</span>
            )}
          </span>
        )}
        {isBenchmarkEnabled && outperf != null && (
          <span
            className={cn(
              "ml-auto rounded-full px-2.5 py-0.5 font-mono text-[11px] font-medium",
              outperf >= 0 ? "bg-gain-soft text-gain" : "bg-loss-soft text-loss"
            )}
          >
            outperformance {outperf > 0 ? "+" : outperf < 0 ? "−" : ""}
            {Math.abs(outperf).toFixed(2)}%
          </span>
        )}
        {!isBenchmarkEnabled && (
          <span className="ml-auto rounded-full border border-border bg-surface-2 px-2.5 py-0.5 font-mono text-[11px] text-muted-foreground">
            Custom capital disabled · S&amp;P 500 benchmark inactive
          </span>
        )}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" preserveAspectRatio="xMidYMid meet">
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={pad.l} x2={W - pad.r} y1={y(t)} y2={y(t)} stroke="hsl(var(--border))" strokeDasharray="3 3" />
            <text x={pad.l - 8} y={y(t) + 4} textAnchor="end" className="fill-text-faint font-mono text-[10px]">
              ${(t / 1000).toFixed(1)}k
            </text>
          </g>
        ))}
        {strategy
          .filter((_, i) => i % labelStep === 0)
          .map((d, i) => (
            <text key={i} x={xFor(n, i * labelStep)} y={H - 4} textAnchor="middle" className="fill-text-faint font-mono text-[10px]">
              {d.date}
            </text>
          ))}
        <path d={area} fill="hsl(var(--brand) / 0.13)" />
        {isBenchmarkEnabled && sp500.length > 1 && (
          <path
            d={makeLine(sp500)}
            fill="none"
            stroke="hsl(var(--muted-foreground))"
            strokeWidth="2"
            strokeDasharray="4 2"
          />
        )}
        <path d={makeLine(strategy)} fill="none" stroke="hsl(var(--brand))" strokeWidth="2.5" />
      </svg>
    </div>
  );
}

