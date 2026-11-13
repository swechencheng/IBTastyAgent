from tastyagent.config import TradingMode
from tastyagent.db.models import TradeStatus
from tastyagent.db.session import in_memory_session
from tastyagent.decision.orchestrator import CycleResult, PlannedTrade
from tastyagent.execution.executor import Executor
from tastyagent.execution.tracker import apply_marks, reconcile_fills
from tastyagent.portfolio.ledger import Ledger

from .conftest import make_candidate


def planned(n=1):
    return PlannedTrade(make_candidate(), n, "because IVR is high")


def cycle(planned_list=None, rejected=None):
    return CycleResult(
        planned=planned_list or [],
        rejected=rejected or [],
        commentary="x",
        considered=1,
    )


class FakePlacer:
    def __init__(self):
        self.calls = 0

    async def __call__(self, trade):
        self.calls += 1
        return f"ORD{trade.id}"


def setup(mode):
    lg = Ledger(in_memory_session())
    d = lg.record_decision(mode, "", 1)
    return lg, d


async def test_sandbox_places_immediately():
    lg, d = setup(TradingMode.SANDBOX)
    placer = FakePlacer()
    ex = Executor(lg, TradingMode.SANDBOX, placer)
    out = await ex.execute_cycle(cycle([planned()]), d)
    assert placer.calls == 1
    t = lg.all_trades()[-1]
    assert t.status is TradeStatus.WORKING
    assert t.broker_order_id == f"ORD{t.id}"
    assert out[-1].action == "placed"


async def test_live_approval_queues_then_approve_places():
    lg, d = setup(TradingMode.LIVE_APPROVAL)
    placer = FakePlacer()
    ex = Executor(lg, TradingMode.LIVE_APPROVAL, placer)
    out = await ex.execute_cycle(cycle([planned()]), d)
    assert placer.calls == 0
    assert out[-1].action == "queued_for_approval"
    pending = lg.pending_approval()
    assert len(pending) == 1
    res = await ex.approve(pending[0].id)
    assert res.action == "placed" and placer.calls == 1
    assert pending[0].status is TradeStatus.WORKING


async def test_live_approval_reject_cancels():
    lg, d = setup(TradingMode.LIVE_APPROVAL)
    ex = Executor(lg, TradingMode.LIVE_APPROVAL, FakePlacer())
    await ex.execute_cycle(cycle([planned()]), d)
    pend = lg.pending_approval()[0]
    ex.reject(pend.id)
    assert pend.status is TradeStatus.CANCELED


async def test_backtest_records_planned_without_placing():
    lg, d = setup(TradingMode.BACKTEST)
    placer = FakePlacer()
    ex = Executor(lg, TradingMode.BACKTEST, placer)
    await ex.execute_cycle(cycle([planned()]), d)
    assert placer.calls == 0
    assert lg.all_trades()[-1].status is TradeStatus.PLANNED


async def test_rejected_candidates_recorded():
    lg, d = setup(TradingMode.SANDBOX)
    ex = Executor(lg, TradingMode.SANDBOX, FakePlacer())
    out = await ex.execute_cycle(
        cycle(rejected=[(make_candidate(), "risk: too big")]), d
    )
    assert any(o.action == "rejected" for o in out)
    assert lg.all_trades()[-1].status is TradeStatus.REJECTED


async def test_placer_error_cancels_trade():
    class BadPlacer:
        async def __call__(self, trade):
            raise RuntimeError("boom")

    lg, d = setup(TradingMode.SANDBOX)
    ex = Executor(lg, TradingMode.SANDBOX, BadPlacer())
    out = await ex.execute_cycle(cycle([planned()]), d)
    assert out[-1].action == "error"
    assert lg.all_trades()[-1].status is TradeStatus.CANCELED


def test_tracker_reconciles_fill_and_marks():
    lg = Ledger(in_memory_session())
    d = lg.record_decision(TradingMode.SANDBOX, "", 0)
    t = lg.record_planned(
        make_candidate(max_profit=250.0), 1, "r", TradingMode.SANDBOX, d
    )
    lg.mark_working(t, "ORD1")
    reconcile_fills(lg, {"ORD1": "Filled"})
    assert t.status is TradeStatus.OPEN
    apply_marks(lg, {t.id: 120.0})
    assert t.current_cost_to_close == 120.0


def test_tracker_cancels_dead_order():
    lg = Ledger(in_memory_session())
    d = lg.record_decision(TradingMode.SANDBOX, "", 0)
    t = lg.record_planned(make_candidate(), 1, "r", TradingMode.SANDBOX, d)
    lg.mark_working(t, "ORD9")
    reconcile_fills(lg, {"ORD9": "Rejected"})
    assert t.status is TradeStatus.CANCELED


import asyncio as _asyncio
from datetime import date as _date
import pytest as _pytest
from tastyagent.config import TradingMode as _TradingMode
from tastyagent.db.models import TradeStatus as _TradeStatus
from tastyagent.db.session import in_memory_session as _in_memory_session
from tastyagent.execution.executor import Executor as _Executor
from tastyagent.models import (
    Action as _Action,
    CandidateTrade as _CandidateTrade,
    Leg as _Leg,
    Liquidity as _Liquidity,
    OptionType as _OptionType,
    Strategy as _Strategy,
)
from tastyagent.portfolio.ledger import Ledger as _Ledger


@_pytest.mark.asyncio
async def test_executor_place_cancels_trade_on_asyncio_cancelled_error():
    sess = _in_memory_session()
    ledger = _Ledger(sess)
    candidate = _CandidateTrade(
        symbol="IGV",
        strategy=_Strategy.NAKED_PUT,
        legs=(
            _Leg(
                option_type=_OptionType.PUT,
                strike=94.0,
                expiration=_date(2026, 12, 18),
                action=_Action.SELL_TO_OPEN,
                delta=-0.30,
            ),
        ),
        dte=44,
        net_credit=6.20,
        max_profit=620.0,
        max_loss=8780.0,
        buying_power_reduction=1800.0,
        underlying_price=105.0,
        iv_rank=0.4,
        liquidity=_Liquidity(bid_ask_width_pct=0.05, open_interest=100, daily_volume=20),
    )
    trade = ledger.record_planned(
        candidate, contracts=1, rationale="test", mode=_TradingMode.SANDBOX
    )

    async def _canceling_placer(_trade):
        raise _asyncio.CancelledError()

    executor = _Executor(
        ledger=ledger,
        mode=_TradingMode.SANDBOX,
        placer=_canceling_placer,
    )

    with _pytest.raises(_asyncio.CancelledError):
        await executor._place(trade)

    assert trade.status == _TradeStatus.CANCELED
    assert trade.exit_reason == "placement canceled during shutdown"
    assert [t.id for t in ledger.open_trades()] == []
    assert any(
        ev.kind == "canceled" and "shutdown" in ev.detail for ev in trade.events
    )
