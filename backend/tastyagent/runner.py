"""The cycle tick: one full pass of the IBTastyAgent with Interactive Brokers.

gather account state -> build market context (IBKR Market Scanner & IV Rank) ->
generate candidates (IBKR Greeks/Chains) -> orchestrator.run_cycle ->
executor (IBKRPlacer with walk-the-book & 50% Take Profit) -> reconcile fills ->
audit take-profit orders -> persist equity snapshot.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date
import logging
from typing import Optional

from ib_async import Option
from sqlalchemy.orm import Session

from .config import TradingMode
from .db.models import EquitySnapshot, Trade, TradeLeg, TradeStatus
from .decision.context import DEFAULT_WATCHLIST, gather_context, rank_universe
from .decision.orchestrator import PortfolioInput, run_cycle
from .execution.executor import Executor
from .execution.exit_manager import (
    PositionMark,
    RollResult,
    audit_take_profit_orders,
    manage_exits,
)
from .execution.tracker import reconcile_fills
from .ibkr.client import IBKRClient
from .ibkr.marketdata import (
    get_option_chain_parameters,
    get_underlying_price,
    select_by_delta,
    snapshot_options,
)
from .ibkr.placer import IBKRPlacer
from .ibkr.scanner import scan_high_options_volume
from .models import OptionType
from .portfolio.benchmark import latest_sp500_close
from .portfolio.ledger import Ledger
from .portfolio.pnl import summarize
from .portfolio.watchlist import WatchlistRepo
from .strategy.candidates import (
    build_strangle_candidate,
    generate_candidates,
    pick_expiration,
)
from .strategy.exits import RollKind

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


async def _build_roll_candidate(
    client: IBKRClient, trade: Trade, roll_kind: RollKind, mark: PositionMark, params
):
    """Build the replacement strangle for a roll."""
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
        from .strategy.candidates import _parse_exp_date

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
        p = select_by_delta(puts, snaps, params.target_short_delta)
        c = select_by_delta(calls, snaps, params.target_short_delta)
        if not p or not c:
            return None

        sp, sc = snaps.get(p.conId), snaps.get(c.conId)
        if not sp or not sc:
            return None

        return build_strangle_candidate(
            sym, underlying, 1.0, (exp_date - today).days, p, c, sp, sc
        )

    return None


async def run_one_cycle(
    *,
    client: IBKRClient,
    metrics_session=None,  # Kept for compatibility; client.data_ib is used
    session: Session,
    runtime,
    watchlist: list[str] | None = None,
) -> dict:
    params = runtime.strategy
    if runtime.mode is TradingMode.SANDBOX:
        params = replace(params, max_bid_ask_width_pct=0.50)
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
    placer = (
        IBKRPlacer(
            client=client,
            walk_step=getattr(runtime, "ibkr_walk_step", 0.01),
            walk_interval=getattr(runtime, "ibkr_walk_interval", 5),
            attach_tp=getattr(runtime, "ibkr_attach_tp", True),
            tp_pct=getattr(runtime, "ibkr_tp_pct", 0.50),
        )
        if runtime.mode in (TradingMode.SANDBOX, TradingMode.LIVE_AUTO)
        else runtime.placer
    )

    if isinstance(placer, IBKRPlacer):
        # Audit open positions for missing Take-Profit orders
        try:
            open_trades = client.active_trading_ib.openTrades()
            active_ids = {str(t.order.orderId) for t in open_trades if t.isActive()}
            tp_alerts = await audit_take_profit_orders(ledger, active_ids)
        except Exception as e:
            logger.debug("Take-profit audit failed: %s", e)

        async def _roll(trade, roll_kind, mark):
            new_cand = await _build_roll_candidate(
                client, trade, roll_kind, mark, params
            )
            if new_cand is None or new_cand.net_credit <= 0:
                return None
            await placer.close(trade, mark.cost_to_close)
            new_order_id = await placer.open_candidate(new_cand, trade.contracts)
            return RollResult(
                new_candidate=new_cand,
                contracts=trade.contracts,
                exit_debit=mark.cost_to_close,
                new_order_id=new_order_id,
            )

        exit_outcomes = await manage_exits(
            ledger,
            params,
            mark_fn=lambda t: _live_mark(client, t),
            close_fn=placer.close,
            roll_fn=_roll,
        )

    bp_used = sum(t.buying_power for t in ledger.open_trades())
    portfolio = PortfolioInput(
        net_liq=net_liq,
        bp_used=bp_used,
        positions_by_symbol=ledger.positions_by_symbol(),
    )

    # 4. Market Context (IV Rank for universe via IBKR 1-year historical IV + cache)
    metrics, regime = await gather_context(client.data_ib, params, universe)
    top_symbols = rank_universe(metrics, params.universe_top_n)
    candidates = await generate_candidates(
        client, client.data_ib, params, top_symbols, metrics=metrics
    )

    # 5. Decision cycle (LLM / Guardrails)
    result = await run_cycle(candidates, portfolio, regime, params, limits)
    decision = ledger.record_decision(
        runtime.mode.value if hasattr(runtime.mode, "value") else str(runtime.mode),
        result.commentary,
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
        "commentary": result.commentary,
        "net_liq": net_liq,
        "open_positions": len(ledger.open_trades()),
        "universe_size": len(universe),
        "analyzed": top_symbols,
    }
