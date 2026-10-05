"""Market metrics: Implied Volatility Rank (IVR) and Percentile calculation.

Fetches 1-year historical implied volatility (252 daily bars) from IBKR via
`reqHistoricalDataAsync(..., whatToShow='OPTION_IMPLIED_VOLATILITY')` and computes:
  - IV Rank = (Current IV - Min IV) / (Max IV - Min IV)
  - IV Percentile = Count(IV_t < Current IV) / Total Days

Results are cached in SQLite with a daily TTL so repeated scans within the same
trading day avoid redundant historical data requests.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
import logging
import sqlite3
import time
from typing import Dict, List, Optional

from ib_async import IB, Stock

from ..db.session import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IVMetrics:
    symbol: str
    iv_rank: Optional[float]  # 0..1 fraction
    iv_percentile: Optional[float]  # 0..1 fraction
    current_iv: Optional[float]
    min_iv: Optional[float]
    max_iv: Optional[float]
    liquidity_rating: int = 4
    next_earnings: Optional[date] = None


def _init_cache_table(db_path: str = DEFAULT_DB_PATH) -> None:
    """Ensure the IV cache table exists in the local SQLite database."""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS iv_metrics_cache (
                    symbol TEXT NOT NULL,
                    cache_date TEXT NOT NULL,
                    iv_rank REAL,
                    iv_percentile REAL,
                    current_iv REAL,
                    min_iv REAL,
                    max_iv REAL,
                    PRIMARY KEY (symbol, cache_date)
                )
                """)
            conn.commit()
    except Exception as e:
        logger.warning("Could not initialize IV metrics cache table: %s", e)


def _get_cached_metric(
    symbol: str, today_str: str, db_path: str = DEFAULT_DB_PATH
) -> Optional[IVMetrics]:
    """Retrieve cached IV metrics for today if available."""
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT iv_rank, iv_percentile, current_iv, min_iv, max_iv
                FROM iv_metrics_cache
                WHERE symbol = ? AND cache_date = ?
                """,
                (symbol, today_str),
            )
            row = cursor.fetchone()
            if row:
                return IVMetrics(
                    symbol=symbol,
                    iv_rank=row[0],
                    iv_percentile=row[1],
                    current_iv=row[2],
                    min_iv=row[3],
                    max_iv=row[4],
                )
    except Exception as e:
        logger.debug("Cache lookup failed for %s: %s", symbol, e)
    return None


def _save_cached_metric(
    m: IVMetrics, today_str: str, db_path: str = DEFAULT_DB_PATH
) -> None:
    """Save calculated IV metrics to local cache."""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO iv_metrics_cache
                (symbol, cache_date, iv_rank, iv_percentile, current_iv, min_iv, max_iv)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    m.symbol,
                    today_str,
                    m.iv_rank,
                    m.iv_percentile,
                    m.current_iv,
                    m.min_iv,
                    m.max_iv,
                ),
            )
            conn.commit()
    except Exception as e:
        logger.debug("Failed to cache IV metrics for %s: %s", m.symbol, e)


async def fetch_symbol_iv_metric(
    ib: IB,
    symbol: str,
    today_str: str,
    db_path: str = DEFAULT_DB_PATH,
    timeout: float = 12.0,
    max_retry_seconds: float = 300.0,
    initial_backoff: float = 2.0,
    backoff_multiplier: float = 2.0,
    max_backoff: float = 60.0,
    sem: Optional[asyncio.Semaphore] = None,
) -> IVMetrics:
    """Fetch 1-year historical IV and calculate IV Rank / Percentile for a single symbol.

    If not cached for today, retries with exponential backoff on failure up to max_retry_seconds.
    Does not fall back to prior-day cached metrics.
    """
    cached = _get_cached_metric(symbol, today_str, db_path)
    if cached is not None:
        return cached

    contract = Stock(symbol, "SMART", "USD")
    start_time = time.monotonic()
    backoff = initial_backoff
    attempt = 1

    while True:
        try:
            if not (ib and ib.isConnected()):
                raise ConnectionError("IB session is not connected")

            async def _req():
                await asyncio.wait_for(
                    ib.qualifyContractsAsync(contract), timeout=timeout / 2
                )
                return await asyncio.wait_for(
                    ib.reqHistoricalDataAsync(
                        contract,
                        endDateTime="",
                        durationStr="1 Y",
                        barSizeSetting="1 day",
                        whatToShow="OPTION_IMPLIED_VOLATILITY",
                        useRTH=True,
                    ),
                    timeout=timeout,
                )

            if sem is not None:
                async with sem:
                    bars = await _req()
            else:
                bars = await _req()

            if not bars:
                raise ValueError(f"No historical IV bars returned for {symbol}")

            ivs = [b.close for b in bars if b.close > 0]
            if not ivs:
                raise ValueError(f"No positive historical IV bars for {symbol}")

            cur_iv = ivs[-1]
            min_iv = min(ivs)
            max_iv = max(ivs)

            if max_iv > min_iv:
                iv_rank = round((cur_iv - min_iv) / (max_iv - min_iv), 4)
            else:
                iv_rank = 0.0

            iv_percentile = round(sum(1 for x in ivs if x < cur_iv) / len(ivs), 4)

            metric = IVMetrics(
                symbol=symbol,
                iv_rank=iv_rank,
                iv_percentile=iv_percentile,
                current_iv=round(cur_iv, 4),
                min_iv=round(min_iv, 4),
                max_iv=round(max_iv, 4),
            )
            _save_cached_metric(metric, today_str, db_path)
            logger.info(
                "%s IV Rank: %.2f%% (Current: %.2f%%, Min: %.2f%%, Max: %.2f%%)",
                symbol,
                iv_rank * 100,
                cur_iv * 100,
                min_iv * 100,
                max_iv * 100,
            )
            return metric

        except Exception as e:
            elapsed = time.monotonic() - start_time
            remaining = max_retry_seconds - elapsed
            if remaining <= 0:
                logger.warning(
                    "Historical IV fetch for %s failed after %d attempt(s) (%.1fs elapsed): %s. No more retries.",
                    symbol,
                    attempt,
                    elapsed,
                    e,
                )
                return IVMetrics(
                    symbol=symbol,
                    iv_rank=None,
                    iv_percentile=None,
                    current_iv=None,
                    min_iv=None,
                    max_iv=None,
                )

            sleep_duration = min(backoff, remaining)
            logger.warning(
                "Historical IV fetch for %s failed on attempt %d (%s). Retrying in %.1fs (%.1fs remaining of %.1fs max retry)...",
                symbol,
                attempt,
                e,
                sleep_duration,
                remaining,
                max_retry_seconds,
            )
            await asyncio.sleep(sleep_duration)
            backoff = min(backoff * backoff_multiplier, max_backoff)
            attempt += 1


async def get_iv_metrics(
    ib: IB,
    symbols: List[str],
    concurrency: int = 5,
    db_path: str = DEFAULT_DB_PATH,
    max_retry_seconds: float = 300.0,
) -> Dict[str, IVMetrics]:
    """Calculate IV Rank and Percentile for a list of symbols with concurrency limiting."""
    _init_cache_table(db_path)
    today_str = date.today().isoformat()
    sem = asyncio.Semaphore(concurrency)

    async def _guarded(sym: str) -> IVMetrics:
        return await fetch_symbol_iv_metric(
            ib,
            sym,
            today_str,
            db_path=db_path,
            max_retry_seconds=max_retry_seconds,
            sem=sem,
        )

    results = await asyncio.gather(*(_guarded(s) for s in symbols))
    return {m.symbol: m for m in results}
