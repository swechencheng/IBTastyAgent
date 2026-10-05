"""Market-context provider — the agent's deterministic "research" stage.

Aggregates IV rank across a watchlist (from the production metrics grant) into the
``regime`` dict the trader LLM consumes. No LLM here: it's data plumbing, so it's
cheap, auditable, and can't drift. The optional VIX value is best-effort.
"""

from __future__ import annotations

from statistics import fmean

from ..config import StrategyParams
from ..ibkr.metrics import IVMetrics, get_iv_metrics

# A liquid, diversified default universe. Override via gather_context(watchlist=...).
DEFAULT_WATCHLIST = [
    "SPY",
    "QQQ",
    "IWM",
    "DIA",  # broad indices
    "XLE",
    "XLF",
    "XLK",
    "GLD",
    "TLT",  # sectors / commodities / bonds
    "AAPL",
    "AMD",
    "TSLA",  # liquid single names
]


def build_regime(
    metrics: dict[str, IVMetrics], params: StrategyParams, vix: float | None
) -> dict:
    """Summarize cross-sectional IV into the LLM-facing regime dict."""
    iv_ranks = {s: m.iv_rank for s, m in metrics.items() if m.iv_rank is not None}
    avg = fmean(iv_ranks.values()) if iv_ranks else 0.0
    high = sorted(s for s, r in iv_ranks.items() if r >= params.min_iv_rank)

    if avg >= 0.40:
        note = "IV elevated broadly — favorable premium-selling environment"
    elif avg < 0.20:
        note = "IV muted broadly — be selective; few rich-premium opportunities"
    else:
        note = "moderate IV — sell premium where IV rank is locally elevated"

    return {
        "vix": vix,
        "avg_iv_rank": round(avg, 3),
        "high_ivr_symbols": high,
        "iv_ranks": {s: round(r, 3) for s, r in sorted(iv_ranks.items())},
        "note": note,
    }


def rank_universe(metrics: dict[str, IVMetrics], top_n: int) -> list[str]:
    """Top-N symbols by IV rank (tie-break on liquidity) — where to spend the
    expensive chain/greeks work each cycle. tastytrade: sell premium where IVR is
    highest, in liquid names."""
    ranked = sorted(
        (m for m in metrics.values() if m.iv_rank is not None),
        key=lambda m: (m.iv_rank, m.liquidity_rating or 0),
        reverse=True,
    )
    result = [m.symbol for m in ranked[:top_n]]
    if len(result) < top_n:
        seen = set(result)
        for sym in metrics.keys():
            if sym not in seen:
                result.append(sym)
                seen.add(sym)
                if len(result) >= top_n:
                    break
    return result


async def gather_context(
    metrics_session,
    params: StrategyParams,
    watchlist: list[str] | None = None,
    max_retry_seconds: float = 300.0,
) -> tuple[dict[str, IVMetrics], dict]:
    """Fetch IV metrics for the watchlist and build the regime. Returns (metrics, regime)."""
    watchlist = watchlist or DEFAULT_WATCHLIST
    metrics = await get_iv_metrics(
        metrics_session, watchlist, max_retry_seconds=max_retry_seconds
    )
    vix: float | None = None  # VIX index metrics are best-effort; left None for now
    return metrics, build_regime(metrics, params, vix)
