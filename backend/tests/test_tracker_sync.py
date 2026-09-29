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


def test_reconcile_positions_from_broker():
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
    assert transitioned == 1
    assert t.status is TradeStatus.OPEN


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
    assert res["reconciled"] == 1
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
