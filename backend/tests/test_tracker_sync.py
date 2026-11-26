from dataclasses import dataclass
from datetime import date
from typing import Optional
import pytest

from tastyagent.config import TradingMode
from tastyagent.db.models import EquitySnapshot, TradeStatus
from tastyagent.db.session import in_memory_session
from tastyagent.execution.tracker import (
    calculate_marks_from_portfolio,
    reconcile_positions_from_broker,
    sync_positions_and_marks,
)
from tastyagent.portfolio.ledger import Ledger

from .conftest import make_candidate


@dataclass
class FakeContract:
    symbol: str
    right: str
    strike: float
    lastTradeDateOrContractMonth: str


@dataclass
class FakePosition:
    contract: FakeContract
    position: float


@dataclass
class FakePortfolioItem:
    contract: FakeContract
    position: float
    marketPrice: float


class FakeIB:
    def __init__(self, positions=None, portfolio=None, trades=None):
        self._positions = positions or []
        self._portfolio = portfolio or []
        self._trades = trades or []

    def isConnected(self) -> bool:
        return True

    def positions(self):
        return self._positions

    def portfolio(self):
        return self._portfolio

    def trades(self):
        return self._trades


class FakeClient:
    def __init__(self, active_ib, data_ib=None):
        self.active_trading_ib = active_ib
        self.data_ib = data_ib or active_ib


def test_account_positions_alone_do_not_promote_an_owned_trade():
    s = in_memory_session()
    lg = Ledger(s)
    c = make_candidate(symbol="SPY", max_profit=200.0)
    # Trade starts WORKING in sandbox
    t = lg.record_planned(c, 1, "test working", TradingMode.SANDBOX)
    assert t.status is TradeStatus.WORKING
    assert len(t.legs) == 2

    # Legs are put 550 and call 610 (from conftest make_candidate)
    leg_p, leg_c = t.legs[0], t.legs[1]
    positions = [
        FakePosition(
            FakeContract(
                "SPY",
                "P" if leg_p.option_type == "put" else "C",
                leg_p.strike,
                str(leg_p.expiration),
            ),
            -1.0,
        ),
        FakePosition(
            FakeContract(
                "SPY",
                "P" if leg_c.option_type == "put" else "C",
                leg_c.strike,
                str(leg_c.expiration),
            ),
            -1.0,
        ),
    ]

    transitioned = reconcile_positions_from_broker(lg, positions)
    assert transitioned == 0
    assert t.status is TradeStatus.WORKING


def test_calculate_marks_from_portfolio():
    s = in_memory_session()
    lg = Ledger(s)
    c = make_candidate(symbol="AAPL", max_profit=300.0)
    t = lg.record_planned(c, 2, "test mark", TradingMode.SANDBOX)
    lg.mark_open(t)

    # Legs are 2 short legs (strangle) with contracts=2
    leg1, leg2 = t.legs[0], t.legs[1]
    portfolio = [
        FakePortfolioItem(
            FakeContract("AAPL", "P", leg1.strike, str(leg1.expiration)),
            position=-2.0,
            marketPrice=1.20,
        ),
        FakePortfolioItem(
            FakeContract("AAPL", "C", leg2.strike, str(leg2.expiration)),
            position=-2.0,
            marketPrice=0.80,
        ),
    ]

    marks = calculate_marks_from_portfolio([t], portfolio)
    assert t.id in marks
    # (1.20 + 0.80) * 100 * 2 contracts = $400.0
    assert marks[t.id] == 400.0


@pytest.mark.asyncio
async def test_sync_positions_and_marks():
    s = in_memory_session()
    lg = Ledger(s)
    c = make_candidate(symbol="NVDA", max_profit=250.0)
    t = lg.record_planned(c, 1, "test sync", TradingMode.SANDBOX)

    lg.mark_open(t)
    leg1, leg2 = t.legs[0], t.legs[1]
    pos = [
        FakePosition(
            FakeContract("NVDA", "P", leg1.strike, str(leg1.expiration)), -1.0
        ),
        FakePosition(
            FakeContract("NVDA", "C", leg2.strike, str(leg2.expiration)), -1.0
        ),
    ]
    port = [
        FakePortfolioItem(
            FakeContract("NVDA", "P", leg1.strike, str(leg1.expiration)), -1.0, 1.00
        ),
        FakePortfolioItem(
            FakeContract("NVDA", "C", leg2.strike, str(leg2.expiration)), -1.0, 0.50
        ),
    ]

    fake_ib = FakeIB(positions=pos, portfolio=port)
    client = FakeClient(fake_ib)

    class FakeRuntime:
        starting_capital = 50000.0

    res = await sync_positions_and_marks(
        client=client, session=s, runtime=FakeRuntime()
    )
    assert res["reconciled"] == 0
    assert res["marks_updated"] == 1
    # Cost to close = (1.00 + 0.50) * 100 = 150.0
    assert t.current_cost_to_close == 150.0
    # Entry credit is 250.0 -> unrealized PnL is 250 - 150 = +100.0
    assert res["unrealized_pnl"] == 100.0
    assert t.status is TradeStatus.OPEN

    # Check snapshot was added
    snap = s.query(EquitySnapshot).first()
    assert snap is not None
    assert snap.unrealized_pnl == 100.0


@dataclass
class FakeOrder:
    orderId: int
    orderRef: str


@dataclass
class FakeOrderStatus:
    status: str


@dataclass
class FakeExecution:
    price: float
    time: date | None = None
    side: str = "SLD"


@dataclass
class FakeFill:
    contract: FakeContract
    execution: FakeExecution


@dataclass
class FakeIBTrade:
    order: FakeOrder
    orderStatus: FakeOrderStatus
    fills: list = None

    def __post_init__(self):
        if self.fills is None:
            self.fills = []


def test_close_without_owned_execution_id_is_not_booked():
    from tastyagent.execution.tracker import reconcile_closing_orders

    s = in_memory_session()
    lg = Ledger(s)
    c = make_candidate(symbol="TLT", max_profit=59.0)
    t = lg.record_planned(c, 1, "test close", TradingMode.SANDBOX)
    lg.mark_open(t)
    t.tp_order_id = "551"
    s.commit()
    assert t.status is TradeStatus.OPEN

    # Simulated filled CLOSE combo order (BAG)
    fake_trade = FakeIBTrade(
        order=FakeOrder(orderId=596, orderRef=f"TastyAgent_CLOSE_{t.id}"),
        orderStatus=FakeOrderStatus(status="Filled"),
        fills=[
            FakeFill(
                contract=FakeContract("TLT", "", 0.0, "", secType="BAG")
                if hasattr(FakeContract, "secType")
                else FakeContract("TLT", "", 0.0, ""),
                execution=FakeExecution(price=-0.88, time=None, side="SLD"),
            )
        ],
    )
    # Set secType on contract for BAG check
    fake_trade.fills[0].contract.secType = "BAG"

    reconciled = reconcile_closing_orders(lg, [fake_trade])
    assert reconciled == 0
    assert t.status is TradeStatus.OPEN
    assert t.exit_debit is None


def test_tp_status_without_execution_does_not_book_a_limit_price():
    from tastyagent.execution.tracker import reconcile_closing_orders

    s = in_memory_session()
    lg = Ledger(s)
    c = make_candidate(symbol="SPY", max_profit=200.0)
    t = lg.record_planned(c, 1, "test tp", TradingMode.SANDBOX)
    lg.mark_open(t)
    t.tp_order_id = "777"
    t.take_profit_debit = 100.0
    s.commit()

    fake_tp = FakeIBTrade(
        order=FakeOrder(orderId=777, orderRef=f"TastyAgent_TP_{t.id}"),
        orderStatus=FakeOrderStatus(status="Filled"),
        fills=[],
    )

    reconciled = reconcile_closing_orders(lg, [fake_tp])
    assert reconciled == 0
    assert t.status is TradeStatus.OPEN
    assert t.realized_pnl is None


from datetime import datetime as _datetime, timedelta as _timedelta, timezone as _timezone
from types import SimpleNamespace as _SimpleNamespace
from unittest.mock import MagicMock as _MagicMock
import pytest as _pytest
from tastyagent.config import TradingMode as _TradingMode
from tastyagent.db.models import Trade as _Trade, TradeStatus as _TradeStatus
from tastyagent.db.session import in_memory_session as _in_memory_session
from tastyagent.execution.tracker import (
    reconcile_stale_working_trades as _reconcile_stale_working_trades,
    sync_positions_and_marks as _sync_positions_and_marks,
)
from tastyagent.portfolio.ledger import Ledger as _Ledger


def test_reconcile_stale_working_trades_cancels_orphans_and_preserves_valid_trades():
    sess = _in_memory_session()
    ledger = _Ledger(sess)
    now = _datetime(2026, 11, 13, 15, 0, 0, tzinfo=_timezone.utc)

    # 1. Old orphan (no broker_order_id, no broker order, no position) -> CANCELED
    t_orphan = _Trade(
        symbol="IGV",
        strategy="naked_put",
        contracts=1,
        status=_TradeStatus.WORKING,
        mode=_TradingMode.SANDBOX,
        broker_order_id=None,
        created_at=now - _timedelta(minutes=20),
    )
    # 2. Fresh in-flight placement (created 30s ago) -> preserved
    t_fresh = _Trade(
        symbol="AAPL",
        strategy="naked_put",
        contracts=1,
        status=_TradeStatus.WORKING,
        mode=_TradingMode.SANDBOX,
        broker_order_id=None,
        created_at=now - _timedelta(seconds=30),
    )
    # 3. Real working order (has broker_order_id) -> preserved
    t_with_id = _Trade(
        symbol="MSFT",
        strategy="put_credit_spread",
        contracts=1,
        status=_TradeStatus.WORKING,
        mode=_TradingMode.SANDBOX,
        broker_order_id="12345",
        created_at=now - _timedelta(hours=1),
    )
    # 4. Working order matched on broker by orderRef -> preserved
    t_broker_ref = _Trade(
        symbol="NVDA",
        strategy="put_credit_spread",
        contracts=1,
        status=_TradeStatus.WORKING,
        mode=_TradingMode.SANDBOX,
        broker_order_id=None,
        created_at=now - _timedelta(hours=1),
    )
    # 5. Working trade whose symbol already has an open OPT position -> preserved
    t_with_pos = _Trade(
        symbol="TSLA",
        strategy="naked_put",
        contracts=1,
        status=_TradeStatus.WORKING,
        mode=_TradingMode.SANDBOX,
        broker_order_id=None,
        created_at=now - _timedelta(hours=1),
    )
    sess.add_all([t_orphan, t_fresh, t_with_id, t_broker_ref, t_with_pos])
    sess.flush()

    ib_trades = [
        _SimpleNamespace(
            order=_SimpleNamespace(orderId=999, orderRef=f"TastyAgent_{t_broker_ref.id}"),
            orderStatus=_SimpleNamespace(status="Submitted"),
        )
    ]
    positions = [
        _SimpleNamespace(
            contract=_SimpleNamespace(symbol="TSLA", secType="OPT"),
            position=-1,
        )
    ]

    canceled = _reconcile_stale_working_trades(
        ledger,
        ib_trades=ib_trades,
        positions=positions,
        stale_after_seconds=600.0,
        now=now,
    )

    assert canceled == 1
    assert t_orphan.status == _TradeStatus.CANCELED
    assert t_fresh.status == _TradeStatus.WORKING
    assert t_with_id.status == _TradeStatus.WORKING
    assert t_broker_ref.status == _TradeStatus.WORKING
    assert t_with_pos.status == _TradeStatus.WORKING


@_pytest.mark.asyncio
async def test_sync_positions_and_marks_cleans_stale_working_trades():
    sess = _in_memory_session()
    old_utc = _datetime.now(_timezone.utc) - _timedelta(minutes=30)
    fresh_utc = _datetime.now(_timezone.utc) - _timedelta(seconds=10)

    t_stale = _Trade(
        symbol="IGV",
        strategy="naked_put",
        contracts=1,
        status=_TradeStatus.WORKING,
        mode=_TradingMode.SANDBOX,
        broker_order_id=None,
        created_at=old_utc,
    )
    t_fresh = _Trade(
        symbol="QQQ",
        strategy="naked_put",
        contracts=1,
        status=_TradeStatus.WORKING,
        mode=_TradingMode.SANDBOX,
        broker_order_id=None,
        created_at=fresh_utc,
    )
    sess.add_all([t_stale, t_fresh])
    sess.commit()

    active_ib = _MagicMock()
    active_ib.isConnected.return_value = True
    active_ib.trades.return_value = []
    active_ib.positions.return_value = []
    active_ib.portfolio.return_value = []

    client = _MagicMock()
    client.active_trading_ib = active_ib

    await _sync_positions_and_marks(client=client, session=sess)

    assert t_stale.status == _TradeStatus.CANCELED
    assert t_fresh.status == _TradeStatus.WORKING
