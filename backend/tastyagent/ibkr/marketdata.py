"""Market data via IBKR: underlying prices, option chain parameters, quotes, and Greeks.

Streams real-time option ticks (including tick 106 for Greeks) and extracts
bid, ask, delta, and implied volatility for strategy construction.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
import logging
import math
from typing import Dict, List, Optional, Tuple

from ib_async import IB, Index, Option, Stock

logger = logging.getLogger(__name__)


@dataclass
class OptionSnapshot:
    con_id: int = 0
    local_symbol: str = ""
    strike: float = 0.0
    right: str = ""  # 'C' or 'P'
    expiration: str = ""  # 'YYYYMMDD'
    streamer_symbol: str = ""
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None
    delta: Optional[float] = None
    implied_vol: Optional[float] = None
    model_price: Optional[float] = None
    open_interest: Optional[int] = None
    daily_volume: Optional[int] = None

    def __post_init__(self):
        if not self.streamer_symbol:
            self.streamer_symbol = self.local_symbol or str(self.con_id)

    @property
    def complete(self) -> bool:
        return self.bid is not None and self.ask is not None and self.delta is not None

    @property
    def mid(self) -> Optional[Decimal]:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2


async def get_underlying_price(ib: IB, symbol: str, timeout: float = 6.0) -> Decimal:
    """Fetch the market price of an underlying stock via real-time market data or historical fallback."""
    contract = Stock(symbol, "SMART", "USD")
    await asyncio.wait_for(ib.qualifyContractsAsync(contract), timeout=timeout / 2)

    # Allow delayed streaming data if live subscription is not present
    ib.reqMarketDataType(3)

    ticker = ib.reqMktData(contract, genericTickList="", snapshot=False)
    end_time = asyncio.get_event_loop().time() + min(timeout, 3.0)

    try:
        while asyncio.get_event_loop().time() < end_time:
            await asyncio.sleep(0.2)
            if (
                ticker.bid is not None
                and ticker.ask is not None
                and ticker.bid > 0
                and ticker.ask > 0
            ):
                return Decimal(str((ticker.bid + ticker.ask) / 2))
            if ticker.last is not None and ticker.last > 0:
                return Decimal(str(ticker.last))
            if ticker.close is not None and ticker.close > 0:
                return Decimal(str(ticker.close))
    finally:
        ib.cancelMktData(contract)

    # Check marketPrice
    mp = ticker.marketPrice()
    if mp and mp > 0 and not (isinstance(mp, float) and (mp != mp)):  # check not nan
        return Decimal(str(mp))

    # Fallback to historical daily bar close (available for all symbols without Level 1 live quote subscription)
    try:
        bars = await asyncio.wait_for(
            ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr="2 D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
            ),
            timeout=timeout,
        )
        if bars:
            return Decimal(str(bars[-1].close))
    except Exception as e:
        logger.debug("Historical price fallback failed for %s: %s", symbol, e)

    raise RuntimeError(f"Could not determine underlying market price for {symbol}")


async def get_option_chain_parameters(
    ib: IB, symbol: str, timeout: float = 10.0
) -> Tuple[List[str], List[float]]:
    """Retrieve available expirations and strikes for the underlying symbol."""
    stock = Stock(symbol, "SMART", "USD")
    await asyncio.wait_for(ib.qualifyContractsAsync(stock), timeout=timeout / 2)
    chains = await asyncio.wait_for(
        ib.reqSecDefOptParamsAsync(stock.symbol, "", stock.secType, stock.conId),
        timeout=timeout,
    )
    if not chains:
        return [], []

    # Prefer SMART exchange chain
    chain = next((c for c in chains if c.exchange == "SMART"), chains[0])
    expirations = sorted(chain.expirations)
    strikes = sorted(chain.strikes)
    return expirations, strikes


async def snapshot_options(
    ib: IB,
    contracts: List[Option],
    timeout: float = 10.0,
) -> Dict[int, OptionSnapshot]:
    """Collect bid, ask, and Greeks for a list of qualified Option contracts.

    Uses genericTickList="106" to request option model Greeks.
    """
    if not contracts:
        return {}

    snaps: Dict[int, OptionSnapshot] = {
        c.conId: OptionSnapshot(
            con_id=c.conId,
            local_symbol=c.localSymbol
            or f"{c.symbol}_{c.right}_{c.strike}_{c.lastTradeDateOrContractMonth}",
            strike=float(c.strike),
            right=c.right,
            expiration=c.lastTradeDateOrContractMonth,
        )
        for c in contracts
    }

    tickers = [
        ib.reqMktData(c, genericTickList="100,101,106", snapshot=False)
        for c in contracts
    ]
    end_time = asyncio.get_event_loop().time() + timeout

    def _valid_count(val) -> Optional[int]:
        if isinstance(val, (int, float)) and math.isfinite(val) and val >= 0:
            return int(val)
        return None

    try:
        while asyncio.get_event_loop().time() < end_time:
            await asyncio.sleep(0.3)
            all_done = True
            for c, t in zip(contracts, tickers):
                snap = snaps[c.conId]
                if t.bid is not None and t.bid > 0:
                    snap.bid = Decimal(str(t.bid))
                if t.ask is not None and t.ask > 0:
                    snap.ask = Decimal(str(t.ask))

                vol = _valid_count(getattr(t, "volume", None))
                if vol is not None and vol > 0:
                    snap.daily_volume = vol

                oi_raw = (
                    getattr(t, "putOpenInterest", None)
                    if c.right == "P"
                    else getattr(t, "callOpenInterest", None)
                )
                oi = _valid_count(oi_raw)
                if oi is None:
                    oi = _valid_count(getattr(t, "openInterest", None))
                if oi is not None and oi > 0:
                    snap.open_interest = oi

                # Check Greeks (model, bid, or ask)
                greeks = t.modelGreeks or t.bidGreeks or t.askGreeks
                if greeks and greeks.delta is not None:
                    snap.delta = float(greeks.delta)
                    snap.implied_vol = (
                        float(greeks.impliedVol)
                        if greeks.impliedVol is not None
                        else None
                    )
                    snap.model_price = (
                        float(greeks.optPrice) if greeks.optPrice is not None else None
                    )

                if not snap.complete:
                    all_done = False

            if all_done:
                break
    finally:
        for c in contracts:
            ib.cancelMktData(c)

    return snaps


def select_by_delta(
    options: List[Option],
    snaps: Dict[int, OptionSnapshot],
    target_delta: float,
) -> Optional[Option]:
    """Find the option in the list whose absolute delta is closest to target_delta."""
    with_delta = [
        o for o in options if (s := snaps.get(o.conId)) and s.delta is not None
    ]
    if not with_delta:
        return None
    return min(
        with_delta, key=lambda o: abs(abs(snaps[o.conId].delta or 0.0) - target_delta)
    )


def _positive_finite(val) -> Optional[float]:
    if isinstance(val, (int, float)) and math.isfinite(val) and val > 0:
        return float(val)
    return None


async def _fetch_vix_ibkr(ib: Optional[IB], timeout: float = 3.0) -> Optional[float]:
    """Best-effort VIX quote via IBKR CBOE index contract."""
    if not ib or not ib.isConnected():
        return None
    vix = Index("VIX", "CBOE", "USD")
    try:
        await asyncio.wait_for(ib.qualifyContractsAsync(vix), timeout=min(timeout, 2.0))
        if not vix.conId:
            return None
        ib.reqMarketDataType(3)
        ticker = ib.reqMktData(vix, genericTickList="", snapshot=False)
        try:
            end_time = asyncio.get_event_loop().time() + min(timeout, 2.0)
            while asyncio.get_event_loop().time() < end_time:
                await asyncio.sleep(0.2)
                for candidate in (
                    getattr(ticker, "last", None),
                    getattr(ticker, "close", None),
                    ticker.marketPrice(),
                ):
                    px = _positive_finite(candidate)
                    if px is not None:
                        return round(px, 2)
        finally:
            ib.cancelMktData(vix)

        bars = await asyncio.wait_for(
            ib.reqHistoricalDataAsync(
                vix,
                endDateTime="",
                durationStr="2 D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
            ),
            timeout=timeout,
        )
        if bars:
            px = _positive_finite(bars[-1].close)
            if px is not None:
                return round(px, 2)
    except Exception as e:
        logger.debug("IBKR VIX lookup unavailable: %s", e)
    return None


async def _fetch_vix_yahoo(timeout: float = 6.0) -> Optional[float]:
    """Fallback best-effort live VIX quote via Yahoo Finance's keyless chart endpoint."""
    try:
        import httpx  # noqa: PLC0415

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
                params={"range": "5d", "interval": "5m"},
                headers={"User-Agent": "tastyagent/0.1"},
            )
            resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
        meta_px = _positive_finite(result.get("meta", {}).get("regularMarketPrice"))
        if meta_px is not None:
            return round(meta_px, 2)
        closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        for close in reversed(closes):
            px = _positive_finite(close)
            if px is not None:
                return round(px, 2)
    except Exception as e:
        logger.debug("Yahoo VIX lookup failed: %s", e)
    return None


async def get_vix_level(
    ib: Optional[IB] = None,
    timeout: float = 5.0,
) -> Optional[float]:
    """Fetch current CBOE VIX level (IBKR primary, Yahoo ^VIX fallback)."""
    px = await _fetch_vix_ibkr(ib, timeout=timeout)
    if px is not None:
        return px
    return await _fetch_vix_yahoo(timeout=timeout)
