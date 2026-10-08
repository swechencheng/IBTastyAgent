"""FastAPI app exposing the agent's ledger, P/L, benchmark, and live controls.

`create_app(session_factory, runtime)` is the testable factory (inject an in-memory
DB). A module-level `app` is also built from settings for `uvicorn`.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, fields, replace
from datetime import date, datetime
import logging
import os
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from ..config import TradingMode
from ..db.models import Decision, EquitySnapshot, Trade, TradeEvent, TradeStatus
from ..decision.context import DEFAULT_WATCHLIST
from ..execution.executor import Executor
from ..portfolio import benchmark as bench
from ..portfolio.ledger import Ledger
from ..portfolio.pnl import summarize
from ..portfolio.watchlist import WatchlistRepo
from ..scheduler import is_market_open
from .runtime import Runtime
from .schemas import (
    ActionResult,
    ActivityItem,
    ActivityTrade,
    BenchmarkOut,
    BenchmarkPoint,
    ConnectionStatusOut,
    EventFeedItem,
    IBKRConfig,
    KillSwitchRequest,
    LLMConfig,
    ModeRequest,
    PnLOut,
    RankedSymbol,
    ReasoningItem,
    ResetSandboxOut,
    SchedulerConfig,
    SettingsOut,
    SettingsUpdate,
    StatusOut,
    SystemConfig,
    TastytradeWatchlist,
    ToggleRequest,
    TradeOut,
    WatchlistAdd,
    WatchlistImport,
    WatchlistItem,
)


def _coerce(typ: str, value):
    if typ == "int":
        return int(value)
    if typ == "float":
        return float(value)
    if typ == "bool":
        return bool(value)
    return value


def _apply_updates(obj, updates: dict):
    """Apply a partial dict of updates to a frozen dataclass, ignoring unknown keys."""
    types = {f.name: f.type for f in fields(obj)}
    clean = {k: _coerce(types[k], v) for k, v in updates.items() if k in types}
    return replace(obj, **clean)


def create_app(
    session_factory: sessionmaker[Session],
    runtime: Runtime,
    *,
    client=None,
    metrics_session=None,
    env_file: Path | str | None = None,
) -> FastAPI:
    app = FastAPI(title="IBTastyAgent", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.session_factory = session_factory
    app.state.runtime = runtime
    app.state.client = client
    app.state.metrics_session = metrics_session
    app.state.env_file = Path(env_file).resolve() if env_file else None
    app.state.scheduler_task = None
    app.state.scheduler_stop = None
    app.state.scheduler_wake = None
    app.state.last_cycle_time = None

    try:
        with session_factory() as init_s:
            from datetime import timezone

            last_dec = init_s.scalar(
                select(Decision.created_at)
                .order_by(Decision.created_at.desc())
                .limit(1)
            )
            if last_dec is not None:
                if getattr(last_dec, "tzinfo", None) is None:
                    last_dec = last_dec.replace(tzinfo=timezone.utc)
                app.state.last_cycle_time = last_dec.timestamp()
    except Exception:
        app.state.last_cycle_time = None

    def _sync_env(updates: dict) -> None:
        target = getattr(app.state, "env_file", None)
        if target is None and not os.environ.get("PYTEST_CURRENT_TEST"):
            from ..settings import find_env_file

            target = find_env_file()
        if target:
            from ..settings import persist_settings_to_env

            persist_settings_to_env(updates, env_file=target)

    def scheduler_running() -> bool:
        t = app.state.scheduler_task
        return bool(t and not t.done())

    def get_session() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    def get_runtime() -> Runtime:
        return app.state.runtime

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/connection", response_model=ConnectionStatusOut)
    def connection() -> ConnectionStatusOut:
        if app.state.client is not None and hasattr(
            app.state.client, "connection_status"
        ):
            cs = app.state.client.connection_status
            if isinstance(getattr(cs, "status", None), str):
                return ConnectionStatusOut(
                    real_connected=bool(getattr(cs, "real_connected", False)),
                    paper_connected=bool(getattr(cs, "paper_connected", False)),
                    status=str(cs.status),
                    detail=str(getattr(cs, "detail", "")),
                )
        return ConnectionStatusOut(
            real_connected=False,
            paper_connected=False,
            status="disconnected",
            detail="No IBKR client configured",
        )

    @app.get("/api/status", response_model=StatusOut)
    def status(rt: Runtime = Depends(get_runtime)) -> StatusOut:
        conn = None
        if app.state.client is not None and hasattr(
            app.state.client, "connection_status"
        ):
            cs = app.state.client.connection_status
            if isinstance(getattr(cs, "status", None), str):
                conn = ConnectionStatusOut(
                    real_connected=bool(getattr(cs, "real_connected", False)),
                    paper_connected=bool(getattr(cs, "paper_connected", False)),
                    status=str(cs.status),
                    detail=str(getattr(cs, "detail", "")),
                )
        return StatusOut(
            mode=rt.mode.value,
            kill_switch=rt.kill_switch,
            market_open=is_market_open(),
            starting_capital=rt.starting_capital,
            requires_approval=rt.mode.requires_approval,
            scheduler_running=scheduler_running(),
            connection=conn,
        )

    @app.get("/api/trades", response_model=list[TradeOut])
    def trades(s: Session = Depends(get_session)) -> list[TradeOut]:
        return [TradeOut.model_validate(t) for t in Ledger(s).all_trades()]

    @app.get("/api/positions", response_model=list[TradeOut])
    def positions(s: Session = Depends(get_session)) -> list[TradeOut]:
        return [TradeOut.model_validate(t) for t in Ledger(s).open_trades()]

    @app.get("/api/trades/closed", response_model=list[TradeOut])
    def closed(s: Session = Depends(get_session)) -> list[TradeOut]:
        return [TradeOut.model_validate(t) for t in Ledger(s).closed_trades()]

    @app.get("/api/approvals", response_model=list[TradeOut])
    def approvals(s: Session = Depends(get_session)) -> list[TradeOut]:
        return [TradeOut.model_validate(t) for t in Ledger(s).pending_approval()]

    @app.get("/api/pnl", response_model=PnLOut)
    def pnl(
        s: Session = Depends(get_session), rt: Runtime = Depends(get_runtime)
    ) -> PnLOut:
        summary = summarize(Ledger(s).all_trades())
        return PnLOut(
            realized_pnl=summary.realized_pnl,
            unrealized_pnl=summary.unrealized_pnl,
            total_pnl=summary.total_pnl,
            open_count=summary.open_count,
            closed_count=summary.closed_count,
            wins=summary.wins,
            losses=summary.losses,
            win_rate=summary.win_rate,
            profit_pct=summary.profit_pct(rt.starting_capital),
            starting_capital=rt.starting_capital,
        )

    @app.post("/api/pnl/sync")
    async def pnl_sync(
        s: Session = Depends(get_session), rt: Runtime = Depends(get_runtime)
    ) -> dict:
        """Trigger an immediate reconciliation of fills and mark update from IBKR."""
        if app.state.client is None:
            raise HTTPException(503, "no broker client configured on this server")
        from ..execution.tracker import sync_positions_and_marks
        from ..runner import _live_mark

        return await sync_positions_and_marks(
            client=app.state.client,
            session=s,
            runtime=rt,
            live_mark_fn=_live_mark,
        )

    @app.get("/api/benchmark", response_model=BenchmarkOut)
    def benchmark(
        s: Session = Depends(get_session), rt: Runtime = Depends(get_runtime)
    ) -> BenchmarkOut:
        use_custom_cap = getattr(rt, "use_custom_working_capital", True)
        snaps = list(s.scalars(select(EquitySnapshot).order_by(EquitySnapshot.ts)))
        if not snaps:
            return BenchmarkOut(
                strategy_return_pct=0.0,
                sp500_return_pct=0.0,
                outperformance_pct=None if not use_custom_cap else 0.0,
                strategy_curve=[],
                sp500_curve=[],
                use_custom_working_capital=use_custom_cap,
            )

        # Filter out isolated transient single-point spikes (e.g. illiquid bid-ask dropouts)
        clean_snaps: list[EquitySnapshot] = []
        n_snaps = len(snaps)
        for i, snap in enumerate(snaps):
            if 0 < i < n_snaps - 1:
                prev_s = snaps[i - 1]
                next_s = snaps[i + 1]
                dt_total = (next_s.ts - prev_s.ts).total_seconds()
                if dt_total <= 1200:  # within 20 minutes
                    d1 = snap.net_liq - prev_s.net_liq
                    d2 = next_s.net_liq - snap.net_liq
                    if abs(d1) > 12.0 and abs(d2) > 12.0 and (d1 * d2 < 0):
                        if abs(next_s.net_liq - prev_s.net_liq) <= 0.5 * max(
                            abs(d1), abs(d2)
                        ):
                            continue
            clean_snaps.append(snap)
        snaps = clean_snaps

        # When custom working capital is disabled, we do NOT compare against S&P 500
        # because dynamic cost basis on a fluctuating multi-strategy IBKR account cannot be reliably tracked.
        if not use_custom_cap:
            strat_pts = [
                BenchmarkPoint(date=snap.ts.date(), value=round(snap.net_liq, 2))
                for snap in snaps
            ]
            strat_ret = (
                bench._return_pct(strat_pts[0].value, strat_pts[-1].value)
                if strat_pts
                else 0.0
            )
            return BenchmarkOut(
                strategy_return_pct=strat_ret,
                sp500_return_pct=0.0,
                outperformance_pct=None,
                strategy_curve=strat_pts,
                sp500_curve=[],
                use_custom_working_capital=False,
            )

        # 1. Forward-fill known S&P 500 closes across snapshots
        last_sp: float | None = None
        sp_values: list[float | None] = []
        for snap in snaps:
            if snap.sp500_close is not None:
                last_sp = snap.sp500_close
            sp_values.append(last_sp)

        # 2. Backward-fill any initial snapshots before the first recorded S&P close
        first_known = next((v for v in sp_values if v is not None), None)
        if first_known is None:
            try:
                first_known = bench.latest_sp500_close()
            except Exception:
                first_known = None

        equity_curve: list[tuple[date, float]] = []
        sp_curve: list[tuple[date, float]] = []
        for snap, sp_val in zip(snaps, sp_values):
            val = sp_val if sp_val is not None else first_known
            equity_curve.append((snap.ts.date(), snap.net_liq))
            if val is not None:
                sp_curve.append((snap.ts.date(), val))

        # Fallback if no S&P data existed at all
        if equity_curve and not sp_curve:
            try:
                sp_curve = bench.fetch_sp500_closes(equity_curve[0][0], date.today())
            except Exception:
                sp_curve = []

        cmp = bench.compare(equity_curve, sp_curve)
        return BenchmarkOut(
            strategy_return_pct=cmp.strategy_return_pct,
            sp500_return_pct=cmp.sp500_return_pct,
            outperformance_pct=cmp.outperformance_pct,
            strategy_curve=[
                BenchmarkPoint(date=d, value=v) for d, v in cmp.strategy_curve
            ],
            sp500_curve=[BenchmarkPoint(date=d, value=v) for d, v in cmp.sp500_curve],
            use_custom_working_capital=True,
        )

    # --- controls ---
    @app.post("/api/mode", response_model=StatusOut)
    def set_mode(req: ModeRequest, rt: Runtime = Depends(get_runtime)) -> StatusOut:
        try:
            rt.mode = TradingMode(req.mode)
        except ValueError:
            raise HTTPException(400, f"invalid mode: {req.mode}")
        if getattr(app.state, "client", None):
            app.state.client.settings.mode = rt.mode
            app.state.client.refresh_account()
        _sync_env({"mode": rt.mode.value})
        return status(rt)

    @app.post("/api/kill-switch", response_model=StatusOut)
    def kill_switch(
        req: KillSwitchRequest, rt: Runtime = Depends(get_runtime)
    ) -> StatusOut:
        rt.kill_switch = req.engaged
        return status(rt)

    @app.post("/api/sandbox/reset", response_model=ResetSandboxOut)
    async def reset_sandbox(
        s: Session = Depends(get_session),
        rt: Runtime = Depends(get_runtime),
    ) -> ResetSandboxOut:
        logger = logging.getLogger("tastyagent.api")
        logger.info(
            "Executing Sandbox Reset: wiping sandbox trades, decisions, events, and canceling broker orders..."
        )

        orders_cancelled = 0
        client = getattr(app.state, "client", None)
        if client and hasattr(client, "active_trading_ib") and client.active_trading_ib:
            try:
                active_ib = client.active_trading_ib
                if active_ib.isConnected():
                    open_trades = list(active_ib.openTrades())
                    for ot in open_trades:
                        ref = getattr(ot.order, "orderRef", "") or ""
                        if ref.startswith("TastyAgent"):
                            active_ib.cancelOrder(ot.order)
                            orders_cancelled += 1
                            logger.info(
                                "Cancelled broker order #%s (%s)",
                                ot.order.orderId,
                                ref,
                            )
            except Exception as e:
                logger.warning(
                    "Failed to cancel open broker orders during sandbox reset: %s", e
                )

        # 1. Delete all sandbox trades (cascades to TradeLeg and TradeEvent)
        sandbox_trades = list(s.scalars(select(Trade).where(Trade.mode == "sandbox")))
        trades_count = len(sandbox_trades)
        sandbox_trade_ids = [t.id for t in sandbox_trades]
        for t in sandbox_trades:
            s.delete(t)

        # Explicitly purge any remaining trade_events linked to sandbox trades or orphaned
        if sandbox_trade_ids:
            s.execute(
                delete(TradeEvent).where(TradeEvent.trade_id.in_(sandbox_trade_ids))
            )
        s.execute(
            delete(TradeEvent).where(
                ~TradeEvent.trade_id.in_(select(Trade.id))
            )
        )

        # 2. Delete all sandbox decisions
        sandbox_decisions = list(
            s.scalars(select(Decision).where(Decision.mode == "sandbox"))
        )
        decisions_count = len(sandbox_decisions)
        for d in sandbox_decisions:
            s.delete(d)

        # 3. Reset equity snapshots and seed baseline snapshot at starting capital
        all_snaps = list(s.scalars(select(EquitySnapshot)))
        for snap in all_snaps:
            s.delete(snap)

        sp_close: float | None = None
        try:
            sp_close = await asyncio.to_thread(bench.latest_sp500_close)
        except Exception as e:
            logger.debug("Failed to fetch S&P close during sandbox reset: %s", e)

        base_cap = getattr(rt, "starting_capital", 15000.0)
        s.add(
            EquitySnapshot(
                net_liq=base_cap,
                realized_pnl_cum=0.0,
                unrealized_pnl=0.0,
                sp500_close=sp_close,
            )
        )

        s.commit()

        app.state.last_cycle_time = None

        logger.info(
            "Sandbox reset completed: %d trades deleted, %d decisions deleted, %d broker orders cancelled.",
            trades_count,
            decisions_count,
            orders_cancelled,
        )

        return ResetSandboxOut(
            status="ok",
            message="Sandbox reset successfully. All sandbox positions, orders, and history have been cleared.",
            trades_deleted=trades_count,
            decisions_deleted=decisions_count,
            orders_cancelled=orders_cancelled,
        )

    @app.post("/api/approvals/{trade_id}/approve", response_model=ActionResult)
    async def approve(
        trade_id: int,
        s: Session = Depends(get_session),
        rt: Runtime = Depends(get_runtime),
    ) -> ActionResult:
        ex = Executor(Ledger(s), rt.mode, rt.placer)
        out = await ex.approve(trade_id)
        if out.action == "error":
            raise HTTPException(409, out.detail)
        return ActionResult(**out.__dict__)

    @app.post("/api/approvals/{trade_id}/reject", response_model=ActionResult)
    def reject(
        trade_id: int,
        s: Session = Depends(get_session),
        rt: Runtime = Depends(get_runtime),
    ) -> ActionResult:
        out = Executor(Ledger(s), rt.mode, rt.placer).reject(trade_id)
        if out.action == "error":
            raise HTTPException(409, out.detail)
        return ActionResult(**out.__dict__)

    @app.post("/api/cycle/run")
    async def run_cycle_now(
        s: Session = Depends(get_session), rt: Runtime = Depends(get_runtime)
    ) -> dict:
        if app.state.client is None:
            raise HTTPException(503, "no broker client configured on this server")
        from ..runner import run_one_cycle

        try:
            return await run_one_cycle(
                client=app.state.client,
                metrics_session=app.state.metrics_session,
                session=s,
                runtime=rt,
            )
        finally:
            import time as time_mod

            app.state.last_cycle_time = time_mod.time()
            if getattr(app.state, "scheduler_wake", None) is not None:
                app.state.scheduler_wake.set()

    async def _tick() -> None:
        sess = session_factory()
        try:
            from ..runner import run_one_cycle

            await run_one_cycle(
                client=app.state.client,
                metrics_session=app.state.metrics_session,
                session=sess,
                runtime=app.state.runtime,
            )
        finally:
            import time as time_mod

            app.state.last_cycle_time = time_mod.time()
            sess.close()

    PNL_POLL_INTERVAL_SECONDS = 300.0  # 5 minutes

    async def _pnl_poll_loop(interval: float = PNL_POLL_INTERVAL_SECONDS) -> None:
        """Background loop: polls and updates position marks & PnL every 5 minutes during market hours."""
        logger = logging.getLogger("tastyagent.api")
        logger.info(
            "Starting PnL & Position mark polling loop (interval=%ds)...",
            int(interval),
        )
        from ..execution.tracker import sync_positions_and_marks
        from ..runner import _live_mark
        from ..scheduler import is_market_open

        # Allow initial gateway connections to complete
        await asyncio.sleep(3.0)

        while getattr(app.state, "pnl_poll_running", True):
            try:
                client = getattr(app.state, "client", None)
                if (
                    client is not None
                    and getattr(client, "active_trading_ib", None)
                    and client.active_trading_ib.isConnected()
                ):
                    initial_synced = getattr(app.state, "initial_pnl_synced", False)
                    if is_market_open() or not initial_synced:
                        sess = session_factory()
                        try:
                            await sync_positions_and_marks(
                                client=client,
                                session=sess,
                                runtime=getattr(app.state, "runtime", None),
                                live_mark_fn=_live_mark,
                            )
                            app.state.initial_pnl_synced = True
                        finally:
                            sess.close()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Error in PnL polling loop: %s", e)

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    @app.on_event("startup")
    async def _on_startup() -> None:
        if app.state.client is not None and hasattr(app.state.client, "connect"):
            try:
                await app.state.client.connect()
            except Exception as e:
                logging.getLogger("tastyagent.api").warning(
                    "IBKR connection failed on startup: %s", e
                )
            if hasattr(app.state.client, "start_connection_monitor"):
                app.state.client.start_connection_monitor()

        # Start 5-minute position mark & PnL polling loop
        app.state.pnl_poll_running = True
        app.state.pnl_poll_task = asyncio.create_task(_pnl_poll_loop())

        auto_start = os.environ.get(
            "TASTYAGENT_AUTO_START_SCHEDULER", "false"
        ).lower() in ("1", "true", "yes") or getattr(
            app.state.runtime, "auto_start_scheduler", False
        )
        if auto_start:
            if not scheduler_running():
                from ..scheduler import run_loop

                stop = asyncio.Event()
                wake = getattr(app.state, "scheduler_wake", None)
                if wake is None:
                    wake = asyncio.Event()
                    app.state.scheduler_wake = wake
                app.state.scheduler_stop = stop
                app.state.scheduler_task = asyncio.create_task(
                    run_loop(
                        _tick,
                        interval_seconds=lambda: app.state.runtime.scheduler_interval_seconds,
                        market_hours_only=lambda: app.state.runtime.scheduler_market_hours_only,
                        stop=stop,
                        wake_event=wake,
                        get_last_tick_time=lambda: getattr(
                            app.state, "last_cycle_time", None
                        ),
                    )
                )

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:
        app.state.pnl_poll_running = False
        if getattr(app.state, "pnl_poll_task", None) is not None:
            app.state.pnl_poll_task.cancel()
        if app.state.scheduler_stop is not None:
            app.state.scheduler_stop.set()
        if getattr(app.state, "scheduler_wake", None) is not None:
            app.state.scheduler_wake.set()
        if app.state.client is not None:
            if hasattr(app.state.client, "stop_connection_monitor"):
                app.state.client.stop_connection_monitor()
            if hasattr(app.state.client, "disconnect"):
                try:
                    await app.state.client.disconnect()
                except Exception as e:
                    logging.getLogger("tastyagent.api").debug(
                        "IBKR disconnect on shutdown: %s", e
                    )

    @app.post("/api/scheduler/start", response_model=StatusOut)
    async def scheduler_start(
        body: dict | None = None, rt: Runtime = Depends(get_runtime)
    ) -> StatusOut:
        if app.state.client is None:
            raise HTTPException(503, "no broker client configured")
        body = body or {}
        if "interval_seconds" in body and body["interval_seconds"] is not None:
            rt.scheduler_interval_seconds = max(30.0, float(body["interval_seconds"]))
            _sync_env({"scheduler_interval_seconds": rt.scheduler_interval_seconds})
        if "market_hours_only" in body and body["market_hours_only"] is not None:
            rt.scheduler_market_hours_only = bool(body["market_hours_only"])
            _sync_env({"scheduler_market_hours_only": rt.scheduler_market_hours_only})

        if not scheduler_running():
            from ..scheduler import run_loop

            stop = asyncio.Event()
            wake = getattr(app.state, "scheduler_wake", None)
            if wake is None:
                wake = asyncio.Event()
                app.state.scheduler_wake = wake
            app.state.scheduler_stop = stop
            app.state.scheduler_task = asyncio.create_task(
                run_loop(
                    _tick,
                    interval_seconds=lambda: app.state.runtime.scheduler_interval_seconds,
                    market_hours_only=lambda: app.state.runtime.scheduler_market_hours_only,
                    stop=stop,
                    wake_event=wake,
                    get_last_tick_time=lambda: getattr(
                        app.state, "last_cycle_time", None
                    ),
                )
            )
        elif getattr(app.state, "scheduler_wake", None) is not None:
            app.state.scheduler_wake.set()

        rt.auto_start_scheduler = True
        os.environ["TASTYAGENT_AUTO_START_SCHEDULER"] = "true"
        _sync_env({"auto_start_scheduler": True})
        return status(rt)

    @app.post("/api/scheduler/stop", response_model=StatusOut)
    async def scheduler_stop(rt: Runtime = Depends(get_runtime)) -> StatusOut:
        if app.state.scheduler_stop is not None:
            app.state.scheduler_stop.set()
        if getattr(app.state, "scheduler_wake", None) is not None:
            app.state.scheduler_wake.set()
        rt.auto_start_scheduler = False
        os.environ["TASTYAGENT_AUTO_START_SCHEDULER"] = "false"
        _sync_env({"auto_start_scheduler": False})
        return status(rt)

    # --- watchlist (the agent's trading universe) ---
    @app.get("/api/watchlist", response_model=list[WatchlistItem])
    async def watchlist(s: Session = Depends(get_session)) -> list[WatchlistItem]:
        repo = WatchlistRepo(s)
        data_ib = (
            getattr(app.state.client, "data_ib", None) or app.state.metrics_session
        )
        if (
            not repo.all()
            and data_ib is not None
            and getattr(data_ib, "isConnected", lambda: False)()
        ):
            from ..ibkr.scanner import scan_high_options_volume

            try:
                symbols = await scan_high_options_volume(data_ib, num_rows=25)
                if symbols:
                    repo.add_many(symbols)
            except Exception:  # noqa: BLE001 - fall back to the static default
                pass
        repo.seed_default_if_empty(DEFAULT_WATCHLIST)
        return [WatchlistItem.model_validate(e) for e in repo.all()]

    @app.post("/api/watchlist", response_model=WatchlistItem)
    def watchlist_add(
        req: WatchlistAdd, s: Session = Depends(get_session)
    ) -> WatchlistItem:
        return WatchlistItem.model_validate(WatchlistRepo(s).add(req.symbol))

    @app.delete("/api/watchlist/{symbol}")
    def watchlist_remove(symbol: str, s: Session = Depends(get_session)) -> dict:
        if not WatchlistRepo(s).remove(symbol):
            raise HTTPException(404, f"{symbol} not in watchlist")
        return {"removed": symbol.upper()}

    @app.post("/api/watchlist/{symbol}/toggle", response_model=WatchlistItem)
    def watchlist_toggle(
        symbol: str, req: ToggleRequest, s: Session = Depends(get_session)
    ) -> WatchlistItem:
        entry = WatchlistRepo(s).set_enabled(symbol, req.enabled)
        if entry is None:
            raise HTTPException(404, f"{symbol} not in watchlist")
        return WatchlistItem.model_validate(entry)

    @app.post("/api/watchlist/import")
    def watchlist_import(
        req: WatchlistImport, s: Session = Depends(get_session)
    ) -> dict:
        added = WatchlistRepo(s).import_symbols(req.symbols, req.source)
        return {"added": added}

    @app.get("/api/watchlist/ranked", response_model=list[RankedSymbol])
    async def watchlist_ranked(s: Session = Depends(get_session)) -> list[RankedSymbol]:
        data_ib = (
            getattr(app.state.client, "data_ib", None) or app.state.metrics_session
        )
        if data_ib is None:
            raise HTTPException(503, "no market-metrics session configured")
        from ..ibkr.metrics import get_iv_metrics

        repo = WatchlistRepo(s)
        repo.seed_default_if_empty(DEFAULT_WATCHLIST)
        try:
            metrics = await get_iv_metrics(data_ib, [e.symbol for e in repo.all()])
        except Exception as e:
            raise HTTPException(503, f"IV rank unavailable: {e}")
        items = [
            RankedSymbol(
                symbol=m.symbol,
                iv_rank=m.iv_rank,
                iv_percentile=m.iv_percentile,
                liquidity_rating=m.liquidity_rating,
            )
            for m in metrics.values()
        ]
        items.sort(
            key=lambda x: (x.iv_rank if x.iv_rank is not None else -1.0), reverse=True
        )
        return items

    @app.get("/api/tastytrade-watchlists", response_model=list[TastytradeWatchlist])
    @app.get("/api/ibkr-scanner", response_model=list[TastytradeWatchlist])
    async def ibkr_scanner_watchlists() -> list[TastytradeWatchlist]:
        data_ib = (
            getattr(app.state.client, "data_ib", None) or app.state.metrics_session
        )
        if data_ib is None:
            raise HTTPException(503, "no production session configured for watchlists")
        from ..ibkr.scanner import scan_high_options_volume

        try:
            symbols = await scan_high_options_volume(data_ib, num_rows=30)
        except Exception:
            symbols = DEFAULT_WATCHLIST
        return [
            TastytradeWatchlist(
                name="High Options Volume (IBKR)", group="Popular", symbols=symbols
            )
        ]

    # --- settings (manage the agent's strategy / risk / capital / scheduler) ---
    def _settings_out(rt: Runtime) -> SettingsOut:
        risk = asdict(rt.risk)
        risk.pop("kill_switch", None)  # kill switch is its own dedicated toggle

        cash_usd = None
        cash_base = None
        base_curr = None
        client = getattr(app.state, "client", None)
        if client:
            summary = client.cached_cash_summary()
            if summary:
                cash_usd = summary.total_cash_usd
                cash_base = summary.total_cash_base
                base_curr = summary.base_currency

        st = getattr(rt, "settings", None)
        if st is None:
            from ..settings import load_settings

            st = load_settings()
            rt.settings = st

        from ..settings import load_frontend_env

        fe_env = load_frontend_env()
        fe_port = int(fe_env.get("PORT", "3066")) if fe_env.get("PORT") else 3066
        fe_api_base = fe_env.get(
            "NEXT_PUBLIC_API_BASE", f"http://localhost:{st.api_port}"
        )

        raw_key = st.openrouter_api_key or ""
        masked_key = (
            f"{raw_key[:10]}••••••••{raw_key[-4:]}"
            if len(raw_key) > 14
            else ("••••••••" if raw_key else "")
        )

        ibkr_cfg = IBKRConfig(
            sandbox_host=st.ibkr_sandbox_host,
            sandbox_port=st.ibkr_sandbox_port,
            sandbox_client_id=st.ibkr_sandbox_client_id,
            live_host=st.ibkr_live_host,
            live_port=st.ibkr_live_port,
            live_client_id=st.ibkr_live_client_id,
            host=st.ibkr_sandbox_host,
            port=st.ibkr_sandbox_port,
            client_id=st.ibkr_sandbox_client_id,
            account=st.ibkr_account,
            data_host=st.ibkr_live_host,
            data_port=st.ibkr_live_port,
            data_client_id=st.ibkr_live_client_id,
            scan_code=st.ibkr_scan_code,
            scan_rows=st.ibkr_scan_rows,
            walk_step=getattr(rt, "ibkr_walk_step", st.ibkr_walk_step),
            walk_interval=getattr(rt, "ibkr_walk_interval", st.ibkr_walk_interval),
            attach_tp=getattr(rt, "ibkr_attach_tp", st.ibkr_attach_tp),
            tp_pct=getattr(rt, "ibkr_tp_pct", st.ibkr_tp_pct),
        )

        llm_cfg = LLMConfig(
            api_key=masked_key,
            model=st.openrouter_model,
            base_url=st.openrouter_base_url,
            site_url=st.openrouter_site_url,
            app_name=st.openrouter_app_name,
        )

        sys_cfg = SystemConfig(
            api_host=st.api_host,
            api_port=st.api_port,
            frontend_port=fe_port,
            frontend_api_base=fe_api_base,
            auto_start_scheduler=getattr(
                rt, "auto_start_scheduler", st.auto_start_scheduler
            ),
        )

        return SettingsOut(
            mode=rt.mode.value,
            kill_switch=rt.kill_switch,
            use_custom_working_capital=getattr(rt, "use_custom_working_capital", True),
            working_capital=rt.starting_capital,
            account_cash_usd=cash_usd,
            account_cash_base=cash_base,
            account_base_currency=base_curr,
            scheduler=SchedulerConfig(
                interval_seconds=rt.scheduler_interval_seconds,
                market_hours_only=rt.scheduler_market_hours_only,
            ),
            strategy=asdict(rt.strategy),
            risk=risk,
            ibkr=ibkr_cfg,
            llm=llm_cfg,
            system=sys_cfg,
        )

    @app.get("/api/settings", response_model=SettingsOut)
    def get_settings(rt: Runtime = Depends(get_runtime)) -> SettingsOut:
        return _settings_out(rt)

    @app.put("/api/settings", response_model=SettingsOut)
    def put_settings(
        req: SettingsUpdate,
        rt: Runtime = Depends(get_runtime),
        s: Session = Depends(get_session),
    ) -> SettingsOut:
        if req.use_custom_working_capital is not None:
            rt.use_custom_working_capital = req.use_custom_working_capital
        if req.working_capital is not None:
            if req.working_capital <= 0:
                raise HTTPException(400, "working_capital must be > 0")
            rt.starting_capital = req.working_capital
            # Recalculate all existing equity snapshots with the new working capital
            # so both the equity curve and S&P 500 benchmark curve immediately rebase.
            snaps = list(s.scalars(select(EquitySnapshot).order_by(EquitySnapshot.ts)))
            if snaps:
                for snap in snaps:
                    snap.net_liq = round(
                        req.working_capital
                        + (snap.realized_pnl_cum or 0.0)
                        + (snap.unrealized_pnl or 0.0),
                        2,
                    )
                s.commit()
        if req.scheduler_interval_seconds is not None:
            rt.scheduler_interval_seconds = max(30.0, req.scheduler_interval_seconds)
            if getattr(app.state, "scheduler_wake", None) is not None:
                app.state.scheduler_wake.set()
        if req.scheduler_market_hours_only is not None:
            rt.scheduler_market_hours_only = req.scheduler_market_hours_only
            if getattr(app.state, "scheduler_wake", None) is not None:
                app.state.scheduler_wake.set()
        if req.auto_start_scheduler is not None:
            rt.auto_start_scheduler = req.auto_start_scheduler
        if req.strategy:
            try:
                rt.strategy = _apply_updates(rt.strategy, req.strategy)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
        if req.risk:
            rt.risk = _apply_updates(
                rt.risk, {k: v for k, v in req.risk.items() if k != "kill_switch"}
            )
        st = getattr(rt, "settings", None)
        if st is None:
            from ..settings import load_settings

            st = load_settings()
            rt.settings = st

        if st is not None:
            if req.ibkr:
                for k, v in req.ibkr.items():
                    attr = f"ibkr_{k}" if not k.startswith("ibkr_") else k
                    if hasattr(st, attr) and v is not None:
                        setattr(st, attr, v)
                if "walk_step" in req.ibkr and req.ibkr["walk_step"] is not None:
                    rt.ibkr_walk_step = float(req.ibkr["walk_step"])
                if "walk_interval" in req.ibkr and req.ibkr["walk_interval"] is not None:
                    rt.ibkr_walk_interval = int(req.ibkr["walk_interval"])
                if "attach_tp" in req.ibkr and req.ibkr["attach_tp"] is not None:
                    rt.ibkr_attach_tp = bool(req.ibkr["attach_tp"])
                if "tp_pct" in req.ibkr and req.ibkr["tp_pct"] is not None:
                    rt.ibkr_tp_pct = float(req.ibkr["tp_pct"])
            if req.llm:
                for k, v in req.llm.items():
                    attr = f"openrouter_{k}" if not k.startswith("openrouter_") else k
                    if hasattr(st, attr) and v is not None:
                        if k == "api_key" and ("••••" in str(v) or "..." in str(v)):
                            continue
                        setattr(st, attr, str(v))
            if req.system:
                if "api_host" in req.system and req.system["api_host"] is not None:
                    st.api_host = str(req.system["api_host"])
                if "api_port" in req.system and req.system["api_port"] is not None:
                    st.api_port = int(req.system["api_port"])
                if (
                    "auto_start_scheduler" in req.system
                    and req.system["auto_start_scheduler"] is not None
                ):
                    rt.auto_start_scheduler = bool(req.system["auto_start_scheduler"])
                    st.auto_start_scheduler = bool(req.system["auto_start_scheduler"])

        _sync_env(req.model_dump(exclude_unset=True))
        return _settings_out(rt)

    # --- activity (recent decision cycles + LLM rationale) ---
    PLACED_STATUSES = (
        TradeStatus.WORKING,
        TradeStatus.OPEN,
        TradeStatus.PENDING_APPROVAL,
        TradeStatus.PLANNED,
    )

    def _act_trade(t: Trade, detail: str = "") -> ActivityTrade:
        return ActivityTrade(
            symbol=t.symbol,
            strategy=t.strategy,
            contracts=t.contracts,
            credit=t.entry_credit,
            pop=t.probability_of_profit,
            status=t.status.value,
            detail=detail,
            realized_pnl=t.realized_pnl,
        )

    @app.get("/api/activity", response_model=list[ActivityItem])
    def activity(
        limit: int = 25, s: Session = Depends(get_session)
    ) -> list[ActivityItem]:
        decisions = list(
            s.scalars(
                select(Decision).order_by(Decision.created_at.desc()).limit(limit)
            )
        )
        # "Managed" actions (exits/rolls) aren't linked directly to a decision foreign key,
        # so bucket CLOSED trades into the cycle concluding at that decision.
        closed: list[Trade] = list(
            s.scalars(
                select(Trade)
                .where(Trade.status == TradeStatus.CLOSED, Trade.closed_at.is_not(None))
                .order_by(Trade.closed_at)
            )
        )

        items: list[ActivityItem] = []
        for i, d in enumerate(decisions):
            # A decision d_i represents the cycle concluding at d_i.created_at.
            # Actions managed in this cycle occurred between the prior decision d_{i+1} and d_i
            # (or for the newest decision i == 0, anything since d_{1}.created_at).
            prior_d = decisions[i + 1] if i + 1 < len(decisions) else None
            lower_bound = prior_d.created_at if prior_d is not None else None
            upper_bound = None if i == 0 else d.created_at

            placed = [t for t in d.trades if t.status in PLACED_STATUSES]
            rejected = [t for t in d.trades if t.status is TradeStatus.REJECTED]
            managed = [
                t
                for t in closed
                if t.closed_at is not None
                and (lower_bound is None or t.closed_at > lower_bound)
                and (upper_bound is None or t.closed_at <= upper_bound)
            ]

            # Per-ticker reasoning bullets (structured, easy to follow).
            reasoning = []
            for t in placed:
                if t.rationale:
                    reasoning.append(
                        ReasoningItem(symbol=t.symbol, text=t.rationale, tone="placed")
                    )
            for t in rejected:
                reasoning.append(
                    ReasoningItem(
                        symbol=t.symbol,
                        text=t.rationale or "Rejected by guardrails.",
                        tone="rejected",
                    )
                )
            for t in managed:
                reasoning.append(
                    ReasoningItem(
                        symbol=t.symbol,
                        text=t.exit_reason or "Managed.",
                        tone="managed",
                    )
                )

            planned = placed + rejected
            items.append(
                ActivityItem(
                    id=d.id,
                    created_at=d.created_at,
                    mode=d.mode,
                    commentary=d.commentary,
                    considered=d.considered,
                    placed=len(placed),
                    rejected=len(rejected),
                    managed=len(managed),
                    symbols=sorted({t.symbol for t in placed}),
                    reasoning=reasoning,
                    planned_trades=[_act_trade(t) for t in planned],
                    placed_trades=[_act_trade(t) for t in placed],
                    rejected_trades=[_act_trade(t, t.rationale) for t in rejected],
                    managed_trades=[
                        _act_trade(t, t.exit_reason or "") for t in managed
                    ],
                )
            )
        return items

    # --- live event feed (status changes -> toast / desktop notifications) ---
    @app.get("/api/events", response_model=list[EventFeedItem])
    def events(
        after: int = 0, limit: int = 50, s: Session = Depends(get_session)
    ) -> list[EventFeedItem]:
        max_id = s.scalar(select(func.max(TradeEvent.id))) or 0
        if after > max_id:
            # Client has an offset from before a database reset.
            # Reset offset back to 0 so fresh events can be fetched.
            after = 0

        if after == 0:
            # Seed request: fetch latest `limit` events in chronological order
            subq = select(TradeEvent).order_by(TradeEvent.id.desc()).limit(limit)
            rows = list(reversed(list(s.scalars(subq))))
        else:
            rows = list(
                s.scalars(
                    select(TradeEvent)
                    .where(TradeEvent.id > after)
                    .order_by(TradeEvent.id)
                    .limit(limit)
                )
            )
        out: list[EventFeedItem] = []
        for e in rows:
            t = e.trade
            out.append(
                EventFeedItem(
                    id=e.id,
                    ts=e.ts,
                    trade_id=e.trade_id,
                    symbol=t.symbol if t else "?",
                    strategy=t.strategy if t else "",
                    kind=e.kind,
                    detail=e.detail,
                )
            )
        return out

    return app


def _default_app() -> FastAPI:
    from pathlib import Path

    from dotenv import load_dotenv

    env_path = Path(__file__).resolve().parents[2] / ".env"
    # uvicorn doesn't load .env; do it here, overriding any empty harness vars.
    load_dotenv(env_path, override=True)

    from ..db.session import init_db, make_engine, session_factory
    from ..ibkr.client import IBKRClient
    from ..settings import load_settings

    settings = load_settings()
    engine = make_engine()
    init_db(engine)
    runtime = Runtime(
        mode=settings.mode,
        use_custom_working_capital=settings.use_custom_working_capital,
        starting_capital=settings.working_capital,
        strategy=settings.strategy_params(),
        risk=settings.risk_limits(),
        scheduler_interval_seconds=settings.scheduler_interval_seconds,
        scheduler_market_hours_only=settings.scheduler_market_hours_only,
        auto_start_scheduler=settings.auto_start_scheduler,
        ibkr_walk_step=settings.ibkr_walk_step,
        ibkr_walk_interval=settings.ibkr_walk_interval,
        ibkr_attach_tp=settings.ibkr_attach_tp,
        ibkr_tp_pct=settings.ibkr_tp_pct,
        settings=settings,
    )

    client = IBKRClient(settings)
    metrics_session = client.data_ib

    sf = session_factory(engine)
    if settings.use_custom_working_capital:
        with sf() as sess:
            snaps = list(sess.scalars(select(EquitySnapshot).order_by(EquitySnapshot.ts)))
            if snaps and abs(snaps[0].net_liq - settings.working_capital) > 0.01:
                for snap in snaps:
                    snap.net_liq = round(
                        settings.working_capital
                        + (snap.realized_pnl_cum or 0.0)
                        + (snap.unrealized_pnl or 0.0),
                        2,
                    )
                sess.commit()

    return create_app(
        sf,
        runtime,
        client=client,
        metrics_session=metrics_session,
        env_file=env_path,
    )


app = _default_app()
