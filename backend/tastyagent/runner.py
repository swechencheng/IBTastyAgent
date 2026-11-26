"""The cycle tick: one full pass of the IBTastyAgent with Interactive Brokers.

gather account state -> build market context (IBKR Market Scanner & IV Rank) ->
generate candidates (IBKR Greeks/Chains) -> orchestrator.run_cycle ->
executor (IBKRPlacer with walk-the-book & 50% Take Profit) -> reconcile fills ->
audit take-profit orders -> persist equity snapshot.
"""

from __future__ import annotations

from datetime import date
import logging
from typing import Optional

from ib_async import Option
from sqlalchemy.orm import Session

from .config import TradingMode
from .db.models import Trade, TradeLeg
from .decision.context import DEFAULT_WATCHLIST, gather_context, rank_universe
from .decision.orchestrator import PortfolioInput, run_cycle
from .execution.executor import Executor
from .execution import roll_orders
from .execution.reconciliation import (
    bind_order, broker_account, outstanding_tp_ref, prepare_order, reconcile_broker_activity,
)
from .execution.exit_manager import (
    PositionMark,
    audit_take_profit_orders,
    manage_exits,
)
from .ibkr.client import IBKRClient
from .ibkr.marketdata import (
    get_option_chain_parameters,
    get_underlying_price,
    get_vix_level,
    select_by_delta,
    snapshot_options,
)
from .ibkr.placer import IBKRPlacer
from .ibkr.scanner import scan_high_options_volume
from .models import OptionType, Strategy
from .portfolio.ledger import Ledger
from .portfolio.watchlist import WatchlistRepo
from .ibkr.metrics import get_iv_metrics
from .strategy.candidates import (
    _parse_exp_date,
    _select_spread_pair,
    build_credit_spread_candidate,
    build_iron_condor_candidate,
    build_naked_put_candidate,
    build_strangle_candidate,
    generate_candidates,
    pick_expiration,
)
from .risk.entry import account_exposure, check_order_entry
from .risk.session import account_modes, session_losses, unverified_symbols, unverified_trades
from .strategy.exits import RollKind
from .strategy.guardrails import (
    INDEX_OR_ETF_SYMBOLS,
    is_vix_below_min,
    validate_roll_candidate,
)

logger = logging.getLogger(__name__)


def _format_expiration(exp: str) -> str:
    return exp.replace("-", "")


async def _qualify_trade_leg(client: IBKRClient, symbol: str, leg: TradeLeg) -> Option:
    right = "P" if leg.option_type.lower() == "put" else "C"
    opt = Option(
        symbol=symbol,
        lastTradeDateOrContractMonth=_format_expiration(str(leg.expiration)),
        strike=round(float(leg.strike), 2),
        right=right,
        exchange="SMART",
        currency="USD",
    )
    await client.data_ib.qualifyContractsAsync(opt)
    return opt


async def _live_mark(client: IBKRClient, trade: Trade) -> PositionMark:
    """Mark an open position using live quotes and Greeks from IBKR.

    Prioritizes IBKR's broker portfolio valuation (`marketPrice`) across all legs,
    matching `calculate_marks_from_portfolio`. Live options snapshots are gathered
    primarily to observe current Greeks (`delta`) and act as a fallback when legs
    are not found in the broker portfolio.
    """
    opts = [await _qualify_trade_leg(client, trade.symbol, leg) for leg in trade.legs]

    # Pre-build portfolio market price lookup for fallback
    port_map: dict[tuple[str, str, float, str], float] = {}
    for ib_session in (client.active_trading_ib, client.data_ib):
        if ib_session and ib_session.isConnected():
            for p in ib_session.portfolio():
                c = getattr(p, "contract", None)
                if (
                    c is not None
                    and getattr(p, "marketPrice", None) is not None
                    and float(p.marketPrice) >= 0.0
                ):
                    sym = getattr(c, "symbol", "").upper()
                    right = getattr(c, "right", "").upper()
                    strike = round(float(getattr(c, "strike", 0.0)), 2)
                    exp = str(
                        getattr(c, "lastTradeDateOrContractMonth", "") or ""
                    ).replace("-", "")
                    port_map[(sym, right, strike, exp)] = float(p.marketPrice)

    # Check if all legs are available in the broker portfolio
    all_legs_in_port = True
    port_cost_per_share = 0.0
    for leg in trade.legs:
        right = "P" if leg.option_type.lower() == "put" else "C"
        strike = round(float(leg.strike), 2)
        exp = str(leg.expiration).replace("-", "")
        key = (trade.symbol.upper(), right, strike, exp)
        if key in port_map:
            mkt_price = port_map[key]
            if "sell" in str(leg.action).lower():
                port_cost_per_share += mkt_price
            else:
                port_cost_per_share -= mkt_price
        else:
            all_legs_in_port = False
            break

    snaps = {}
    try:
        snaps = await snapshot_options(client.data_ib, opts, timeout=6.0)
    except Exception as e:
        logger.debug("snapshot_options failed in _live_mark: %s", e)

    total_cost_per_share = port_cost_per_share if all_legs_in_port else 0.0
    short_deltas: list[tuple[OptionType, float]] = []

    for leg, opt in zip(trade.legs, opts):
        snap = snaps.get(opt.conId)
        if not all_legs_in_port:
            mid_price: float | None = None
            right = "P" if leg.option_type.lower() == "put" else "C"
            strike = round(float(leg.strike), 2)
            exp = str(leg.expiration).replace("-", "")
            key = (trade.symbol.upper(), right, strike, exp)
            if key in port_map:
                mid_price = port_map[key]
            elif snap is not None and snap.mid is not None:
                mid_price = float(snap.mid)

            if mid_price is None:
                raise RuntimeError(
                    f"Incomplete mark for {trade.symbol} leg {opt.conId}"
                )

            if "sell" in str(leg.action).lower():
                total_cost_per_share += mid_price
            else:
                total_cost_per_share -= mid_price

        if (
            snap is not None
            and "sell" in str(leg.action).lower()
            and snap.delta is not None
        ):
            short_deltas.append((OptionType(leg.option_type), abs(snap.delta)))

    tested_side = max_short_delta = None
    if short_deltas:
        tested_side, max_short_delta = max(short_deltas, key=lambda x: x[1])

    return PositionMark(
        cost_to_close=max(0.0, round(total_cost_per_share * 100 * trade.contracts, 2)),
        max_short_delta=max_short_delta,
        tested_side=tested_side,
    )


def _find_trade_leg(
    trade: Trade, option_type: str, action_substr: str
) -> Optional[TradeLeg]:
    for leg in trade.legs:
        if (
            leg.option_type.lower() == option_type
            and action_substr in leg.action.lower()
        ):
            return leg
    return None


def _trade_strike_width(trade: Trade) -> Optional[float]:
    """Return defined-risk max wing width for a Trade, or None if undefined risk."""
    sp = _find_trade_leg(trade, "put", "sell")
    lp = _find_trade_leg(trade, "put", "buy")
    sc = _find_trade_leg(trade, "call", "sell")
    lc = _find_trade_leg(trade, "call", "buy")
    widths = []
    if sp and lp:
        widths.append(abs(float(sp.strike) - float(lp.strike)))
    if sc and lc:
        widths.append(abs(float(sc.strike) - float(lc.strike)))
    return max(widths) if widths else None


def _match_contract_strike(
    options: list[Option], target_strike: float
) -> Optional[Option]:
    exact = [
        o for o in options if round(float(o.strike), 2) == round(target_strike, 2)
    ]
    if exact:
        return exact[0]
    return min(
        options, key=lambda o: abs(float(o.strike) - target_strike), default=None
    )


def _build_same_strike_roll_out(
    trade: Trade,
    sym: str,
    underlying: float,
    iv_rank: float,
    dte: int,
    puts: list[Option],
    calls: list[Option],
    snaps: dict,
):
    """Rebuild the same strikes in a later expiration when delta-target roll fails credit."""
    strat_str = str(getattr(trade.strategy, "value", trade.strategy)).lower()
    sp_leg = _find_trade_leg(trade, "put", "sell")
    lp_leg = _find_trade_leg(trade, "put", "buy")
    sc_leg = _find_trade_leg(trade, "call", "sell")
    lc_leg = _find_trade_leg(trade, "call", "buy")

    def sn(o: Optional[Option]):
        return snaps.get(o.conId) if o else None

    if strat_str == "put_credit_spread" and sp_leg and lp_leg:
        p_s = _match_contract_strike(puts, float(sp_leg.strike))
        p_l = _match_contract_strike(puts, float(lp_leg.strike))
        if p_s and p_l and sn(p_s) and sn(p_l) and p_l.strike < p_s.strike:
            return build_credit_spread_candidate(
                sym,
                underlying,
                iv_rank,
                dte,
                p_s,
                p_l,
                sn(p_s),
                sn(p_l),
                option_type=OptionType.PUT,
                strategy=Strategy.PUT_CREDIT_SPREAD,
            )
    elif strat_str == "call_credit_spread" and sc_leg and lc_leg:
        c_s = _match_contract_strike(calls, float(sc_leg.strike))
        c_l = _match_contract_strike(calls, float(lc_leg.strike))
        if c_s and c_l and sn(c_s) and sn(c_l) and c_l.strike > c_s.strike:
            return build_credit_spread_candidate(
                sym,
                underlying,
                iv_rank,
                dte,
                c_s,
                c_l,
                sn(c_s),
                sn(c_l),
                option_type=OptionType.CALL,
                strategy=Strategy.CALL_CREDIT_SPREAD,
            )
    elif strat_str == "naked_put" and sp_leg:
        p_s = _match_contract_strike(puts, float(sp_leg.strike))
        if p_s and sn(p_s):
            return build_naked_put_candidate(sym, underlying, iv_rank, dte, p_s, sn(p_s))
    elif strat_str == "iron_condor" and sp_leg and lp_leg and sc_leg and lc_leg:
        p_s = _match_contract_strike(puts, float(sp_leg.strike))
        p_l = _match_contract_strike(puts, float(lp_leg.strike))
        c_s = _match_contract_strike(calls, float(sc_leg.strike))
        c_l = _match_contract_strike(calls, float(lc_leg.strike))
        if (
            p_s
            and p_l
            and c_s
            and c_l
            and p_l.strike < p_s.strike < c_s.strike < c_l.strike
        ):
            return build_iron_condor_candidate(
                sym, underlying, iv_rank, dte, p_s, p_l, c_s, c_l, snaps
            )
    elif strat_str == "short_strangle" and sp_leg and sc_leg:
        p_s = _match_contract_strike(puts, float(sp_leg.strike))
        c_s = _match_contract_strike(calls, float(sc_leg.strike))
        if p_s and c_s and sn(p_s) and sn(c_s) and p_s.strike < c_s.strike:
            return build_strangle_candidate(
                sym, underlying, iv_rank, dte, p_s, c_s, sn(p_s), sn(c_s)
            )
    return None


async def _build_roll_candidate(
    client: IBKRClient, trade: Trade, roll_kind: RollKind, mark: PositionMark, params
):
    """Build the replacement trade for a roll (OUT or UNTESTED)."""
    sym = trade.symbol
    today = date.today()
    raw_exps, strikes = await get_option_chain_parameters(client.data_ib, sym)
    if not raw_exps or not strikes:
        return None

    underlying = float(await get_underlying_price(client.data_ib, sym))
    lo, hi = underlying * 0.7, underlying * 1.3
    eligible_strikes = [s for s in strikes if lo <= s <= hi]
    if not eligible_strikes:
        return None

    if roll_kind is RollKind.OUT:
        parsed_exps = [_parse_exp_date(e) for e in raw_exps]
        exp_date = pick_expiration(parsed_exps, params, today)
        if exp_date is None:
            return None

        exp_str = exp_date.strftime("%Y%m%d")
        pattern = Option(sym, exp_str, right="", exchange="SMART")
        try:
            cds = await client.data_ib.reqContractDetailsAsync(pattern)
            contracts = [cd.contract for cd in cds]
        except Exception:
            contracts = []

        if not contracts:
            contracts = [
                Option(sym, exp_str, s, r, "SMART", currency="USD")
                for s in eligible_strikes
                for r in ("P", "C")
            ]
            await client.data_ib.qualifyContractsAsync(*contracts)
            contracts = [c for c in contracts if c.conId > 0]

        if not contracts:
            return None

        lo_rel, hi_rel = underlying * 0.75, underlying * 1.25
        relevant_contracts = [
            c for c in contracts if lo_rel <= c.strike <= hi_rel
        ] or contracts
        snaps = await snapshot_options(client.data_ib, relevant_contracts, timeout=8.0)

        puts = [c for c in relevant_contracts if c.right == "P"]
        calls = [c for c in relevant_contracts if c.right == "C"]

        # Retrieve fresh IV rank for underlying or fall back to min_iv_rank
        iv_rank = params.min_iv_rank
        try:
            m = await get_iv_metrics(client.data_ib, sym)
            if m and m.iv_rank is not None:
                iv_rank = float(m.iv_rank)
        except Exception as e:
            logger.debug("Roll candidate IV rank lookup failed for %s: %s", sym, e)

        dte = (exp_date - today).days
        strat_str = str(getattr(trade.strategy, "value", trade.strategy)).lower()
        is_index = sym.upper() in INDEX_OR_ETF_SYMBOLS
        spread_target = getattr(params, "spread_target_short_delta", 0.30)
        spread_cap = getattr(params, "spread_max_short_leg_delta", 0.35)
        min_put_ratio = params.min_credit_width_ratio
        min_call_ratio = (
            getattr(params, "index_call_min_credit_width_ratio", 0.20)
            if is_index
            else params.min_credit_width_ratio
        )
        orig_width = _trade_strike_width(trade)
        max_loss_cap = orig_width * 100.0 if orig_width is not None else None

        fresh = None
        if strat_str == "put_credit_spread":
            p_short, p_long = _select_spread_pair(
                puts,
                puts,
                snaps,
                is_put=True,
                target_delta=spread_target,
                max_delta=spread_cap,
                min_ratio=min_put_ratio,
                fallback_delta=params.spread_long_delta,
                max_max_loss=max_loss_cap,
            )
            sp_s = snaps.get(p_short.conId) if p_short else None
            sp_l = snaps.get(p_long.conId) if p_long else None
            if p_short and p_long and sp_s and sp_l and p_long.strike < p_short.strike:
                fresh = build_credit_spread_candidate(
                    sym,
                    underlying,
                    iv_rank,
                    dte,
                    p_short,
                    p_long,
                    sp_s,
                    sp_l,
                    option_type=OptionType.PUT,
                    strategy=Strategy.PUT_CREDIT_SPREAD,
                )

        elif strat_str == "call_credit_spread":
            c_short, c_long = _select_spread_pair(
                calls,
                calls,
                snaps,
                is_put=False,
                target_delta=spread_target,
                max_delta=spread_cap,
                min_ratio=min_call_ratio,
                fallback_delta=params.spread_long_delta,
                max_max_loss=max_loss_cap,
            )
            sc_s = snaps.get(c_short.conId) if c_short else None
            sc_l = snaps.get(c_long.conId) if c_long else None
            if c_short and c_long and sc_s and sc_l and c_long.strike > c_short.strike:
                fresh = build_credit_spread_candidate(
                    sym,
                    underlying,
                    iv_rank,
                    dte,
                    c_short,
                    c_long,
                    sc_s,
                    sc_l,
                    option_type=OptionType.CALL,
                    strategy=Strategy.CALL_CREDIT_SPREAD,
                )

        elif strat_str == "naked_put":
            p_short = select_by_delta(puts, snaps, params.target_short_delta)
            sp_s = snaps.get(p_short.conId) if p_short else None
            if p_short and sp_s:
                fresh = build_naked_put_candidate(
                    sym, underlying, iv_rank, dte, p_short, sp_s
                )

        elif strat_str == "iron_condor":
            p_short, p_long = _select_spread_pair(
                puts,
                puts,
                snaps,
                is_put=True,
                target_delta=spread_target,
                max_delta=spread_cap,
                min_ratio=min_put_ratio,
                fallback_delta=params.spread_long_delta,
                max_max_loss=max_loss_cap,
            )
            c_short, c_long = _select_spread_pair(
                calls,
                calls,
                snaps,
                is_put=False,
                target_delta=spread_target,
                max_delta=spread_cap,
                min_ratio=min_call_ratio,
                fallback_delta=params.spread_long_delta,
                max_max_loss=max_loss_cap,
            )
            if (
                p_short
                and p_long
                and c_short
                and c_long
                and p_long.strike < p_short.strike < c_short.strike < c_long.strike
            ):
                fresh = build_iron_condor_candidate(
                    sym,
                    underlying,
                    iv_rank,
                    dte,
                    p_short,
                    p_long,
                    c_short,
                    c_long,
                    snaps,
                )

        else:
            p = select_by_delta(puts, snaps, params.target_short_delta)
            c = select_by_delta(calls, snaps, params.target_short_delta)
            sp = snaps.get(p.conId) if p else None
            sc = snaps.get(c.conId) if c else None
            if p and c and sp and sc and p.strike < c.strike:
                fresh = build_strangle_candidate(
                    sym, underlying, iv_rank, dte, p, c, sp, sc
                )

        contracts_n = max(1, trade.contracts or 1)
        fresh_ok = (
            fresh is not None
            and fresh.net_credit * contracts_n >= mark.cost_to_close
            and (
                orig_width is None
                or fresh.strike_width is None
                or fresh.strike_width <= orig_width + 1e-6
            )
        )
        if fresh_ok:
            return fresh

        # If a fresh delta-reset roll fails net-credit or expands wings (common when tested/ITM),
        # preserve original strikes in the next cycle to collect time value without expanding risk.
        same_strike = _build_same_strike_roll_out(
            trade, sym, underlying, iv_rank, dte, puts, calls, snaps
        )
        if same_strike is not None and (
            orig_width is None
            or same_strike.strike_width is None
            or same_strike.strike_width <= orig_width + 1e-6
        ):
            if fresh is None or same_strike.net_credit >= fresh.net_credit:
                return same_strike
        return fresh

    if roll_kind is RollKind.UNTESTED:
        short_puts = [
            l
            for l in trade.legs
            if l.option_type.lower() == "put" and "sell" in l.action.lower()
        ]
        short_calls = [
            l
            for l in trade.legs
            if l.option_type.lower() == "call" and "sell" in l.action.lower()
        ]
        if not short_puts or not short_calls:
            return None

        sp_leg = short_puts[0]
        sc_leg = short_calls[0]
        long_puts = [
            l
            for l in trade.legs
            if l.option_type.lower() == "put" and "buy" in l.action.lower()
        ]
        long_calls = [
            l
            for l in trade.legs
            if l.option_type.lower() == "call" and "buy" in l.action.lower()
        ]
        is_iron_condor = bool(long_puts and long_calls)
        lp_leg = long_puts[0] if is_iron_condor else None
        lc_leg = long_calls[0] if is_iron_condor else None
        put_width = abs(sp_leg.strike - lp_leg.strike) if lp_leg else 0.0
        call_width = abs(sc_leg.strike - lc_leg.strike) if lc_leg else 0.0

        # Untested side roll stays in the SAME expiration
        exp = min((leg.expiration for leg in trade.legs), default=today)
        exp_date = _parse_exp_date(exp)
        exp_str = exp_date.strftime("%Y%m%d")
        dte = max(0, (exp_date - today).days)

        pattern = Option(sym, exp_str, right="", exchange="SMART")
        try:
            cds = await client.data_ib.reqContractDetailsAsync(pattern)
            contracts = [cd.contract for cd in cds]
        except Exception:
            contracts = []

        if not contracts:
            contracts = [
                Option(sym, exp_str, s, r, "SMART", currency="USD")
                for s in eligible_strikes
                for r in ("P", "C")
            ]
            await client.data_ib.qualifyContractsAsync(*contracts)
            contracts = [c for c in contracts if c.conId > 0]

        if not contracts:
            return None

        lo_rel, hi_rel = underlying * 0.70, underlying * 1.30
        relevant_contracts = [
            c for c in contracts if lo_rel <= c.strike <= hi_rel
        ] or contracts
        snaps = await snapshot_options(client.data_ib, relevant_contracts, timeout=8.0)

        puts = [c for c in relevant_contracts if c.right == "P"]
        calls = [c for c in relevant_contracts if c.right == "C"]

        # Identify tested side
        tested_side = mark.tested_side
        if tested_side is None:
            if abs(underlying - sc_leg.strike) <= abs(underlying - sp_leg.strike):
                tested_side = OptionType.CALL
            else:
                tested_side = OptionType.PUT
        elif isinstance(tested_side, str):
            tested_side = OptionType(tested_side.lower())

        if tested_side == OptionType.CALL:
            # Short Call tested -> keep call side, roll put side UP towards spot
            # Must keep short put strike < short call strike to prevent inversion
            eligible_puts = [
                p for p in puts if sp_leg.strike < p.strike < sc_leg.strike
            ]
            new_sp = select_by_delta(eligible_puts, snaps, params.target_short_delta)
            if not new_sp:
                return None

            cs_candidates = [
                c for c in calls if round(c.strike, 2) == round(sc_leg.strike, 2)
            ]
            if not cs_candidates:
                return None
            new_sc = cs_candidates[0]

            if is_iron_condor:
                new_lp_strike = round(new_sp.strike - put_width, 2)
                lp_candidates = [p for p in puts if round(p.strike, 2) == new_lp_strike]
                if not lp_candidates:
                    lp_candidates = sorted(
                        (
                            p
                            for p in puts
                            if p.strike < new_sp.strike
                            and (new_sp.strike - p.strike) <= put_width + 1e-6
                        ),
                        key=lambda p: abs(p.strike - new_lp_strike),
                    )
                if not lp_candidates:
                    return None
                new_lp = lp_candidates[0]

                lc_candidates = [
                    c for c in calls if round(c.strike, 2) == round(lc_leg.strike, 2)
                ]
                if not lc_candidates:
                    return None
                new_lc = lc_candidates[0]

                return build_iron_condor_candidate(
                    sym, underlying, 1.0, dte, new_sp, new_lp, new_sc, new_lc, snaps
                )
            else:
                sp_snap = snaps.get(new_sp.conId)
                sc_snap = snaps.get(new_sc.conId)
                if not sp_snap or not sc_snap:
                    return None
                return build_strangle_candidate(
                    sym, underlying, 1.0, dte, new_sp, new_sc, sp_snap, sc_snap
                )

        else:
            # Short Put tested -> keep put side, roll call side DOWN towards spot
            # Must keep short call strike > short put strike to prevent inversion
            eligible_calls = [
                c for c in calls if sp_leg.strike < c.strike < sc_leg.strike
            ]
            new_sc = select_by_delta(eligible_calls, snaps, params.target_short_delta)
            if not new_sc:
                return None

            ps_candidates = [
                p for p in puts if round(p.strike, 2) == round(sp_leg.strike, 2)
            ]
            if not ps_candidates:
                return None
            new_sp = ps_candidates[0]

            if is_iron_condor:
                new_lc_strike = round(new_sc.strike + call_width, 2)
                lc_candidates = [
                    c for c in calls if round(c.strike, 2) == new_lc_strike
                ]
                if not lc_candidates:
                    lc_candidates = sorted(
                        (
                            c
                            for c in calls
                            if c.strike > new_sc.strike
                            and (c.strike - new_sc.strike) <= call_width + 1e-6
                        ),
                        key=lambda c: abs(c.strike - new_lc_strike),
                    )
                if not lc_candidates:
                    return None
                new_lc = lc_candidates[0]

                lp_candidates = [
                    p for p in puts if round(p.strike, 2) == round(lp_leg.strike, 2)
                ]
                if not lp_candidates:
                    return None
                new_lp = lp_candidates[0]

                return build_iron_condor_candidate(
                    sym, underlying, 1.0, dte, new_sp, new_lp, new_sc, new_lc, snaps
                )
            else:
                sp_snap = snaps.get(new_sp.conId)
                sc_snap = snaps.get(new_sc.conId)
                if not sp_snap or not sc_snap:
                    return None
                return build_strangle_candidate(
                    sym, underlying, 1.0, dte, new_sp, new_sc, sp_snap, sc_snap
                )

    return None


def make_placer(client, runtime):
    return IBKRPlacer(
        client=client,
        walk_step=runtime.ibkr_walk_step,
        walk_interval=runtime.ibkr_walk_interval,
        attach_tp=runtime.ibkr_attach_tp,
        tp_pct=runtime.ibkr_tp_pct,
        min_credit_width_ratio=runtime.strategy.min_credit_width_ratio,
        index_call_min_credit_width_ratio=runtime.strategy.index_call_min_credit_width_ratio,
        min_cushion_pct=runtime.risk_limits().min_cushion_pct,
    )


async def run_one_cycle(
    *,
    client: IBKRClient,
    metrics_session=None,  # Kept for compatibility; client.data_ib is used
    session: Session,
    runtime,
    watchlist: list[str] | None = None,
) -> dict:
    params = runtime.strategy
    limits = runtime.risk_limits()
    ledger = Ledger(session)

    # 1. Watchlist seeding: if empty, seed from IBKR Market Scanner
    repo = WatchlistRepo(session)
    if not repo.all() and client.data_ib.isConnected():
        try:
            scan_syms = await scan_high_options_volume(client.data_ib, num_rows=25)
            if scan_syms:
                repo.add_many(scan_syms)
        except Exception as e:
            logger.debug("Scanner seeding failed: %s", e)
    repo.seed_default_if_empty(DEFAULT_WATCHLIST)
    universe = watchlist or repo.symbols() or DEFAULT_WATCHLIST

    # 2. Working Capital & Account Balance
    use_custom_capital = getattr(runtime, "use_custom_working_capital", True)
    if use_custom_capital:
        net_liq = runtime.starting_capital
        logger.info("Position sizing using custom working capital: $%.2f USD", net_liq)
    else:
        cash_summary = await client.get_account_cash_summary()
        net_liq = cash_summary.total_cash_usd
        logger.info(
            "Position sizing using IBKR account Total Cash: $%.2f USD "
            "(Base currency: %s, Total Cash Base: %.2f, Forex balances: %s)",
            net_liq,
            cash_summary.base_currency,
            cash_summary.total_cash_base,
            cash_summary.forex_balances,
        )
        if net_liq <= 0:
            logger.warning(
                "IBKR account Total Cash is non-positive ($%.2f USD). New entries will be halted per capital control.",
                net_liq,
            )

    # 3. Position & Exit Management (with Attached Take-Profit)
    exit_outcomes = []
    tp_alerts = []
    placer = make_placer(client, runtime) if runtime.mode in (
        TradingMode.SANDBOX, TradingMode.LIVE_AUTO
    ) else runtime.placer

    if isinstance(placer, IBKRPlacer):
        ib = client.active_trading_ib
        account = broker_account(client)
        if not account or not ib.isConnected():
            raise RuntimeError("Active trading account unavailable for execution reconciliation")
        reconcile_broker_activity(ledger, ib.trades(), ib.fills(), account=account, mode=runtime.mode)
        modes = account_modes(runtime.mode)

        def leg_key(symbol, leg):
            return symbol, str(leg.expiration), leg.option_type, leg.strike

        uncertain_legs = {
            leg_key(trade.symbol, leg)
            for trade in unverified_trades(session, runtime.mode)
            for leg in trade.legs
        }

        def eligible(trade):
            return (
                trade.mode in modes
                and trade.execution_verified is not False
                and not any(leg_key(trade.symbol, leg) in uncertain_legs for leg in trade.legs)
            )

        async def submit_management(trade, mark_cost, reason, replacement=None, staged=None):
            from uuid import uuid4
            kind = "roll" if replacement else "close"
            ref = f"TastyAgent_{kind.upper()}_{trade.id}_{uuid4().hex}"
            if replacement:
                prepared = await roll_orders.prepare_roll_order(
                    placer, trade, replacement, mark_cost, order_ref=ref,
                )
            else:
                prepared = await roll_orders.prepare_close_order(placer, trade, mark_cost, order_ref=ref)
            # Qualifying contracts made no order changes. Commit identity before the first network write.
            intent = prepare_order(
                ledger, trade, kind, account=account, replacement=replacement,
                reason=reason, order_ref=ref,
            )
            try:
                if staged is not None:
                    from .execution.staged_roll import stage_target
                    stage_target(ledger, intent, staged)
                intent.spec = {**intent.spec, "bag_action": prepared.order.action}
                prepared.order.account = account
                bind_order(ledger, intent, order_id=prepared.order.orderId, status="Submitting")
                tp_ref = outstanding_tp_ref(ledger, trade.id, account)
            except Exception:
                ledger.s.rollback()
                bind_order(ledger, intent, status="Aborted")
                raise

            def confirm_tp_cancel(cancelled):
                reconcile_broker_activity(
                    ledger, [cancelled], ib.fills(), account=account, mode=runtime.mode,
                )
                if str(trade.tp_order_id) == str(cancelled.order.orderId):
                    trade.tp_order_id = None
                    ledger.s.commit()

            try:
                bt = await roll_orders.submit_prepared_order(
                    placer, trade, prepared, tp_order_ref=tp_ref,
                    on_tp_cancelled=confirm_tp_cancel,
                )
            except roll_orders.RollBlocked as err:
                bind_order(ledger, intent, status="Aborted")
                ledger.record_event(trade, "blocked", str(err))
                raise
            except BaseException:
                bind_order(ledger, intent, status="Unknown")
                raise
            bind_order(ledger, intent, order_id=bt.order.orderId, perm_id=bt.order.permId,
                       status=bt.orderStatus.status)
            reconcile_broker_activity(ledger, [bt], ib.fills(), account=account, mode=runtime.mode)
            if intent.terminal and not intent.applied:
                raise roll_orders.RollBlocked(f"{kind.capitalize()} ended with no confirmed fills ({bt.orderStatus.status})")
            return ref

        async def _close(trade, cost):
            return await submit_management(trade, cost, "Defensive close")

        async def _roll(trade, roll_kind, mark):
            new_cand = await _build_roll_candidate(
                client, trade, roll_kind, mark, params
            )
            if new_cand is None or new_cand.net_credit <= 0:
                return None
            if any(leg_key(new_cand.symbol, leg) in uncertain_legs for leg in new_cand.legs):
                raise roll_orders.RollBlocked("Replacement overlaps a pending historical execution")

            orig_exp = min(
                (leg.expiration for leg in trade.legs), default=date.today()
            )
            orig_dte = max(0, (_parse_exp_date(orig_exp) - date.today()).days)
            orig_width = _trade_strike_width(trade)
            gr = validate_roll_candidate(
                new_cand,
                roll_kind=roll_kind.value,
                exit_debit_total=mark.cost_to_close,
                contracts=trade.contracts,
                original_dte_remaining=orig_dte,
                original_strike_width=orig_width,
                params=params,
            )
            if not gr.ok:
                logger.warning(
                    "Roll candidate for Trade #%s rejected by roll guardrails: %s; skipping roll",
                    trade.id,
                    "; ".join(gr.violations),
                )
                return None
            try:
                return await submit_management(trade, mark.cost_to_close, roll_kind.value, new_cand)
            except roll_orders.UnsupportedRoll:
                if roll_kind is not RollKind.OUT:
                    raise
                return await submit_management(
                    trade, mark.cost_to_close, "Close for staged expiry roll", staged=new_cand,
                )

        exit_outcomes = await manage_exits(
            ledger,
            params,
            mark_fn=lambda t: _live_mark(client, t),
            close_fn=_close,
            eligible=eligible,
            roll_fn=_roll,
        )

        if placer.attach_tp:
            visible_ids = {str(t.order.orderId) for t in ib.openTrades() if t.isActive()}
            tp_alerts = await audit_take_profit_orders(
                ledger, visible_ids, auto_attach_fn=placer.attach_take_profit, eligible=eligible
            )

    # Check account cushion from IBKR
    cash_summary = await client.get_account_cash_summary()
    cushion = cash_summary.cushion
    is_live_mode = (
        runtime.mode.is_live
        if hasattr(runtime.mode, "is_live")
        else runtime.mode in (TradingMode.LIVE_AUTO, TradingMode.LIVE_APPROVAL)
    )

    cushion_blocked = (
        is_live_mode and cushion is not None and cushion < limits.min_cushion_pct
    )
    if cushion_blocked:
        logger.warning(
            "🚨 [LIVE CUSHION GUARD] IBKR account cushion is %.1f%% (minimum required: %.1f%%). "
            "Halting opening of new positions.",
            cushion * 100,
            limits.min_cushion_pct * 100,
        )

    if isinstance(placer, IBKRPlacer):
        from .execution.staged_roll import resume_staged_rolls

        def guarded_entry(trade):
            check_order_entry(ledger, trade, runtime, net_liq, cushion)

        placer.entry_guard = guarded_entry

        def make_reopen(minimum_credit):
            replacement_placer = make_placer(client, runtime)
            replacement_placer.entry_guard = guarded_entry
            replacement_placer.additional_min_credit = minimum_credit
            return replacement_placer

        await resume_staged_rolls(
            ledger, make_reopen, account=account, mode=runtime.mode, params=params,
        )
    counts, losing_symbols, spread_bp_used, undefined_bp_used = account_exposure(
        ledger, runtime.mode, params,
    )
    losses = session_losses(session, runtime.mode, net_liq, limits)
    quarantined = unverified_symbols(session, runtime.mode)
    universe = [symbol for symbol in universe if symbol not in quarantined]

    portfolio = PortfolioInput(
        net_liq=net_liq,
        bp_used=spread_bp_used + undefined_bp_used,
        positions_by_symbol=dict(counts),
        realized_pnl_today=losses.realized,
        consecutive_losses=losses.streak,
        cushion=cushion,
        is_live=is_live_mode,
        losing_symbols=losing_symbols,
        spread_bp_used=spread_bp_used,
        undefined_bp_used=undefined_bp_used,
    )

    if losses.halt_reason:
        top_symbols, candidates = [], []
        regime = {"entry_halt": losses.halt_reason}
        logger.warning("%s", losses.halt_reason)
    elif cushion_blocked:
        top_symbols = []
        candidates = []
        regime = {"cushion": cushion, "cushion_guard_active": True}
    else:
        metrics_session = client.data_ib or client.active_trading_ib
        vix_level = await get_vix_level(metrics_session)
        if is_vix_below_min(vix_level, params):
            logger.warning(
                "🛑 [VIX GUARD] Live VIX is %.2f (minimum required: %.2f). "
                "Skipping candidate scan and halting opening of new positions.",
                vix_level,
                params.min_vix,
            )
            top_symbols = []
            candidates = []
            regime = {
                "vix": round(float(vix_level), 2),
                "vix_guard_active": True,
                "min_vix": params.min_vix,
            }
        else:
            # 4. Market Context (IV Rank for universe via IBKR 1-year historical IV + cache)
            scheduler_interval = float(
                getattr(runtime, "scheduler_interval_seconds", 300.0)
            )
            max_retry_seconds = min(300.0, scheduler_interval)
            metrics, regime = await gather_context(
                metrics_session,
                params,
                universe,
                max_retry_seconds=max_retry_seconds,
                vix=vix_level,
            )
            top_symbols = rank_universe(metrics, params.universe_top_n)
            logger.info(
                "Universe scan: %d symbols configured, %d top symbols selected by IV rank (%s)",
                len(universe),
                len(top_symbols),
                ", ".join(top_symbols) if top_symbols else "none",
            )
            candidates = await generate_candidates(
                client,
                metrics_session,
                params,
                top_symbols,
                metrics=metrics,
                max_trade_bp=(
                    limits.max_trade_bp_pct * net_liq if net_liq > 0 else None
                ),
            )
            logger.info(
                "Candidate generation complete: %d candidate trades found across %d symbols",
                len(candidates),
                len(top_symbols),
            )

    # 5. Decision cycle (LLM / Guardrails)
    result = await run_cycle(candidates, portfolio, regime, params, limits)
    decision = ledger.record_decision(
        runtime.mode.value if hasattr(runtime.mode, "value") else str(runtime.mode),
        losses.halt_reason or result.commentary,
        result.considered,
    )

    # 6. Execute new entries
    outcomes = await Executor(ledger, runtime.mode, placer).execute_cycle(
        result, decision
    )

    # 7. Reconcile broker fills, synchronize marks, and persist canonical equity snapshot
    try:
        from .execution.tracker import sync_positions_and_marks

        await sync_positions_and_marks(
            client=client,
            session=session,
            runtime=runtime,
            live_mark_fn=_live_mark,
        )
    except Exception as e:
        logger.warning("Post-cycle position & equity sync failed: %s", e)

    return {
        "considered": result.considered,
        "planned": len(result.planned),
        "rejected": len(result.rejected),
        "exits": [o.__dict__ for o in exit_outcomes],
        "closed": sum(1 for o in exit_outcomes if o.action == "closed"),
        "alerts": tp_alerts,
        "outcomes": [o.__dict__ for o in outcomes],
        "commentary": losses.halt_reason or result.commentary,
        "net_liq": net_liq,
        "open_positions": len(ledger.open_trades()),
        "universe_size": len(universe),
        "analyzed": top_symbols,
    }
