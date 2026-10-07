"""Candidate generation: scan a watchlist into concrete CandidateTrades using IBKR data.

For each underlying we pull IV rank (via 1-year historical IV), the option chain
parameters, streamed Greeks/quotes via IBKR market data, pick the ~target-DTE
expiration, choose short strikes near target delta, and assemble strangles, credit
spreads, or iron condors.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from decimal import Decimal
import logging
from typing import List, Optional

from ib_async import Option

from ..config import StrategyParams
from ..ibkr.client import IBKRClient
from ..ibkr.marketdata import (
    OptionSnapshot,
    get_option_chain_parameters,
    get_underlying_price,
    select_by_delta,
    snapshot_options,
)
from ..ibkr.metrics import get_iv_metrics
from ..models import Action, CandidateTrade, Leg, Liquidity, OptionType, Strategy

logger = logging.getLogger(__name__)

# Placeholders for open interest and daily volume
_PLACEHOLDER_OI = 1000
_PLACEHOLDER_VOL = 500


def _parse_exp_date(exp: str | date) -> date:
    """Parse date from YYYYMMDD string or return date object."""
    if isinstance(exp, date):
        return exp
    clean = str(exp).replace("-", "")
    return date(int(clean[:4]), int(clean[4:6]), int(clean[6:8]))


def _get_strike(opt) -> float:
    return float(getattr(opt, "strike", getattr(opt, "strike_price", 0.0)))


def _get_exp(opt) -> date:
    raw = getattr(
        opt, "lastTradeDateOrContractMonth", getattr(opt, "expiration_date", None)
    )
    return _parse_exp_date(raw)


def pick_expiration(
    expirations: list[date], params: StrategyParams, today: date
) -> date | None:
    """Choose the listed expiration closest to target DTE within the [min,max] window."""
    target = today + timedelta(days=params.target_dte)
    valid = [
        d for d in expirations if params.min_dte <= (d - today).days <= params.max_dte
    ]
    if not valid:
        return None
    return min(valid, key=lambda d: abs((d - target).days))


def estimate_undefined_bp(underlying_price: float) -> float:
    """Rough per-contract margin for an undefined-risk short option (~20% notional)."""
    return round(0.20 * underlying_price * 100, 2)


def _width_pct(snap: OptionSnapshot) -> float:
    if snap.bid is None or snap.ask is None or snap.mid in (None, 0):
        return 1.0  # treat unknown as illiquid
    return float((snap.ask - snap.bid) / snap.mid)


def build_strangle_candidate(
    symbol: str,
    underlying_price: float,
    iv_rank: float,
    dte: int,
    put: Option,
    call: Option,
    put_snap: OptionSnapshot,
    call_snap: OptionSnapshot,
    *,
    earnings_in_days: int | None = None,
) -> CandidateTrade | None:
    if put_snap.mid is None or call_snap.mid is None:
        return None
    credit = (float(put_snap.mid) + float(call_snap.mid)) * 100  # dollars per contract
    if credit <= 0:
        return None

    legs = (
        Leg(
            OptionType.PUT,
            _get_strike(put),
            _get_exp(put),
            Action.SELL_TO_OPEN,
            delta=put_snap.delta or 0.0,
        ),
        Leg(
            OptionType.CALL,
            _get_strike(call),
            _get_exp(call),
            Action.SELL_TO_OPEN,
            delta=call_snap.delta or 0.0,
        ),
    )
    liquidity = Liquidity(
        bid_ask_width_pct=max(_width_pct(put_snap), _width_pct(call_snap)),
        open_interest=_PLACEHOLDER_OI,
        daily_volume=_PLACEHOLDER_VOL,
    )
    return CandidateTrade(
        symbol=symbol,
        strategy=Strategy.SHORT_STRANGLE,
        legs=legs,
        dte=dte,
        net_credit=credit,
        max_profit=credit,
        max_loss=float("inf"),
        buying_power_reduction=estimate_undefined_bp(underlying_price),
        underlying_price=underlying_price,
        iv_rank=iv_rank,
        liquidity=liquidity,
        earnings_in_days=earnings_in_days,
    )


def build_naked_put_candidate(
    symbol,
    underlying_price,
    iv_rank,
    dte,
    put: Option,
    put_snap: OptionSnapshot,
    *,
    earnings_in_days: int | None = None,
) -> CandidateTrade | None:
    if put_snap.mid is None:
        return None
    credit = float(put_snap.mid) * 100
    if credit <= 0:
        return None
    strike = _get_strike(put)
    legs = (
        Leg(
            OptionType.PUT,
            strike,
            _get_exp(put),
            Action.SELL_TO_OPEN,
            delta=put_snap.delta or 0.0,
        ),
    )
    return CandidateTrade(
        symbol=symbol,
        strategy=Strategy.NAKED_PUT,
        legs=legs,
        dte=dte,
        net_credit=credit,
        max_profit=credit,
        max_loss=strike * 100 - credit,
        buying_power_reduction=estimate_undefined_bp(underlying_price),
        underlying_price=underlying_price,
        iv_rank=iv_rank,
        liquidity=Liquidity(_width_pct(put_snap), _PLACEHOLDER_OI, _PLACEHOLDER_VOL),
        earnings_in_days=earnings_in_days,
    )


def build_credit_spread_candidate(
    symbol,
    underlying_price,
    iv_rank,
    dte,
    short_opt: Option,
    long_opt: Option,
    short_snap: OptionSnapshot,
    long_snap: OptionSnapshot,
    *,
    option_type: OptionType,
    strategy: Strategy,
    earnings_in_days: int | None = None,
) -> CandidateTrade | None:
    """Defined-risk vertical: short + protective long of the same type."""
    if short_snap.mid is None or long_snap.mid is None:
        return None
    net_ps = float(short_snap.mid) - float(long_snap.mid)
    width = abs(_get_strike(short_opt) - _get_strike(long_opt))
    if net_ps <= 0 or width <= 0:
        return None
    credit = net_ps * 100
    max_loss = (width - net_ps) * 100
    legs = (
        Leg(
            option_type,
            _get_strike(short_opt),
            _get_exp(short_opt),
            Action.SELL_TO_OPEN,
            delta=short_snap.delta or 0.0,
        ),
        Leg(
            option_type,
            _get_strike(long_opt),
            _get_exp(long_opt),
            Action.BUY_TO_OPEN,
            delta=long_snap.delta or 0.0,
        ),
    )
    return CandidateTrade(
        symbol=symbol,
        strategy=strategy,
        legs=legs,
        dte=dte,
        net_credit=credit,
        max_profit=credit,
        max_loss=max_loss,
        buying_power_reduction=max_loss,  # defined risk: BP == max loss
        underlying_price=underlying_price,
        iv_rank=iv_rank,
        liquidity=Liquidity(_width_pct(short_snap), _PLACEHOLDER_OI, _PLACEHOLDER_VOL),
        earnings_in_days=earnings_in_days,
    )


def build_iron_condor_candidate(
    symbol,
    underlying_price,
    iv_rank,
    dte,
    put_short: Option,
    put_long: Option,
    call_short: Option,
    call_long: Option,
    snaps: dict,
    *,
    earnings_in_days: int | None = None,
) -> CandidateTrade | None:
    def _lookup_snap(opt) -> OptionSnapshot | None:
        con_id = getattr(opt, "conId", None)
        streamer_sym = getattr(opt, "streamer_symbol", None)
        return (snaps.get(con_id) if con_id else None) or snaps.get(streamer_sym)

    ps, pl = _lookup_snap(put_short), _lookup_snap(put_long)
    cs, cl = _lookup_snap(call_short), _lookup_snap(call_long)
    if not all(s is not None and s.mid is not None for s in (ps, pl, cs, cl)):
        return None
    net_ps = (float(ps.mid) + float(cs.mid)) - (float(pl.mid) + float(cl.mid))
    if net_ps <= 0:
        return None
    put_width = abs(_get_strike(put_short) - _get_strike(put_long))
    call_width = abs(_get_strike(call_short) - _get_strike(call_long))
    width = max(put_width, call_width)
    credit = net_ps * 100
    max_loss = (width - net_ps) * 100
    legs = (
        Leg(
            OptionType.PUT,
            _get_strike(put_short),
            _get_exp(put_short),
            Action.SELL_TO_OPEN,
            delta=ps.delta or 0.0,
        ),
        Leg(
            OptionType.PUT,
            _get_strike(put_long),
            _get_exp(put_long),
            Action.BUY_TO_OPEN,
            delta=pl.delta or 0.0,
        ),
        Leg(
            OptionType.CALL,
            _get_strike(call_short),
            _get_exp(call_short),
            Action.SELL_TO_OPEN,
            delta=cs.delta or 0.0,
        ),
        Leg(
            OptionType.CALL,
            _get_strike(call_long),
            _get_exp(call_long),
            Action.BUY_TO_OPEN,
            delta=cl.delta or 0.0,
        ),
    )
    return CandidateTrade(
        symbol=symbol,
        strategy=Strategy.IRON_CONDOR,
        legs=legs,
        dte=dte,
        net_credit=credit,
        max_profit=credit,
        max_loss=max_loss,
        buying_power_reduction=max_loss,
        underlying_price=underlying_price,
        iv_rank=iv_rank,
        liquidity=Liquidity(
            max(_width_pct(ps), _width_pct(cs)), _PLACEHOLDER_OI, _PLACEHOLDER_VOL
        ),
        earnings_in_days=earnings_in_days,
    )


async def _candidates_for_symbol(
    client: IBKRClient, params: StrategyParams, today: date, symbol: str, m
) -> list[CandidateTrade]:
    """Build feasible options strategies for one underlying using IBKR chain and Greeks."""
    ib_session = (
        client.data_ib
        if (client.data_ib and client.data_ib.isConnected())
        else client.active_trading_ib
    )
    raw_exps, strikes = await get_option_chain_parameters(ib_session, symbol)
    if not raw_exps or not strikes:
        return []

    parsed_exps = [_parse_exp_date(e) for e in raw_exps]
    exp_date = pick_expiration(parsed_exps, params, today)
    if exp_date is None:
        return []

    exp_str = exp_date.strftime("%Y%m%d")
    underlying = float(await get_underlying_price(ib_session, symbol))

    # Fetch valid option contracts directly via reqContractDetailsAsync to avoid invalid strike errors
    pattern = Option(symbol, exp_str, right="", exchange="SMART")
    try:
        cds = await asyncio.wait_for(
            ib_session.reqContractDetailsAsync(pattern), timeout=8.0
        )
        contracts = [cd.contract for cd in cds]
    except Exception as e:
        logger.debug(
            "Failed to fetch option contract details for %s %s: %s", symbol, exp_str, e
        )
        contracts = []

    if not contracts:
        lo, hi = underlying * 0.7, underlying * 1.3
        eligible_strikes = [s for s in strikes if lo <= s <= hi]
        contracts = [
            Option(symbol, exp_str, s, r, "SMART", currency="USD")
            for s in eligible_strikes
            for r in ("P", "C")
        ]
        try:
            await asyncio.wait_for(
                ib_session.qualifyContractsAsync(*contracts), timeout=8.0
            )
            contracts = [c for c in contracts if c.conId > 0]
        except Exception as e:
            logger.debug("Failed to qualify fallback contracts for %s: %s", symbol, e)
            contracts = []

    if not contracts:
        return []

    # Focus snapshot on contracts within +/- 25% of underlying spot
    lo, hi = underlying * 0.75, underlying * 1.25
    relevant_contracts = [c for c in contracts if lo <= c.strike <= hi] or contracts

    snaps = await snapshot_options(ib_session, relevant_contracts, timeout=8.0)

    puts = [c for c in relevant_contracts if c.right == "P"]
    calls = [c for c in relevant_contracts if c.right == "C"]

    dte = (exp_date - today).days
    ivr = (m.iv_rank or 0.0) if m else 0.0
    earn = (
        (m.next_earnings - today).days
        if (m and getattr(m, "next_earnings", None))
        else None
    )

    # Short legs must respect max_short_leg_delta cap
    eligible_short_puts = [
        p
        for p in puts
        if (s := snaps.get(getattr(p, "conId", None)))
        and s.delta is not None
        and abs(s.delta) <= params.max_short_leg_delta
    ] or puts
    eligible_short_calls = [
        c
        for c in calls
        if (s := snaps.get(getattr(c, "conId", None)))
        and s.delta is not None
        and abs(s.delta) <= params.max_short_leg_delta
    ] or calls

    p_short = select_by_delta(eligible_short_puts, snaps, params.target_short_delta)
    c_short = select_by_delta(eligible_short_calls, snaps, params.target_short_delta)
    p_long = select_by_delta(puts, snaps, params.spread_long_delta)
    c_long = select_by_delta(calls, snaps, params.spread_long_delta)

    def sn(o):
        return snaps.get(o.conId)

    out: list[CandidateTrade] = []

    if p_short and c_short and sn(p_short) and sn(c_short):
        out.append(
            build_strangle_candidate(
                symbol,
                underlying,
                ivr,
                dte,
                p_short,
                c_short,
                sn(p_short),
                sn(c_short),
                earnings_in_days=earn,
            )
        )
    if p_short and sn(p_short):
        out.append(
            build_naked_put_candidate(
                symbol,
                underlying,
                ivr,
                dte,
                p_short,
                sn(p_short),
                earnings_in_days=earn,
            )
        )
    if (
        p_short
        and p_long
        and sn(p_short)
        and sn(p_long)
        and _get_strike(p_long) < _get_strike(p_short)
    ):
        out.append(
            build_credit_spread_candidate(
                symbol,
                underlying,
                ivr,
                dte,
                p_short,
                p_long,
                sn(p_short),
                sn(p_long),
                option_type=OptionType.PUT,
                strategy=Strategy.PUT_CREDIT_SPREAD,
                earnings_in_days=earn,
            )
        )
    if (
        c_short
        and c_long
        and sn(c_short)
        and sn(c_long)
        and _get_strike(c_long) > _get_strike(c_short)
    ):
        out.append(
            build_credit_spread_candidate(
                symbol,
                underlying,
                ivr,
                dte,
                c_short,
                c_long,
                sn(c_short),
                sn(c_long),
                option_type=OptionType.CALL,
                strategy=Strategy.CALL_CREDIT_SPREAD,
                earnings_in_days=earn,
            )
        )
    if (
        p_short
        and c_short
        and p_long
        and c_long
        and _get_strike(p_long) < _get_strike(p_short)
        and _get_strike(c_long) > _get_strike(c_short)
    ):
        out.append(
            build_iron_condor_candidate(
                symbol,
                underlying,
                ivr,
                dte,
                p_short,
                p_long,
                c_short,
                c_long,
                snaps,
                earnings_in_days=earn,
            )
        )

    return [c for c in out if c is not None]


async def generate_candidates(
    client: IBKRClient,
    metrics_session,  # Kept for signature compatibility
    params: StrategyParams,
    watchlist: list[str],
    *,
    concurrency: int = 5,
    metrics: dict | None = None,
) -> list[CandidateTrade]:
    """Scan watchlist: build feasible strategies concurrently via IBKR."""
    today = date.today()
    if metrics is None:
        ib_data = (
            client.data_ib
            if (client.data_ib and client.data_ib.isConnected())
            else client.active_trading_ib
        )
        metrics = await get_iv_metrics(ib_data, watchlist)

    # Prioritize symbols with known IV metrics, but never starve candidate generation
    eligible = [s for s in watchlist if (m := metrics.get(s)) and m.iv_rank is not None]
    if not eligible:
        eligible = [s for s in watchlist if s in (metrics or {})] or list(watchlist)

    sem = asyncio.Semaphore(concurrency)

    async def guarded(symbol: str) -> list[CandidateTrade]:
        async with sem:
            try:
                m = metrics.get(symbol) if metrics else None
                return await _candidates_for_symbol(client, params, today, symbol, m)
            except Exception as e:
                logger.debug("Failed candidate generation for %s: %s", symbol, e)
                return []

    results = await asyncio.gather(*(guarded(s) for s in eligible))
    return [c for lst in results for c in lst]
