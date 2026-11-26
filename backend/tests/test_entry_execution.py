"""Broker callbacks, delayed fills and the limit-price boundary on opening orders."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from ib_async import CommissionReport, Execution, Fill, OrderStatus
from sqlalchemy import select

from tastyagent.config import TradingMode
from tastyagent.db.models import ExecutionOrder, TradeStatus
from tastyagent.db.session import in_memory_session
from tastyagent.execution.executor import Executor
from tastyagent.ibkr.placer import IBKRPlacer
from tastyagent.models import Action, OptionType, Strategy
from tastyagent.portfolio.ledger import Ledger
from tastyagent.strategy.sizing import effective_buying_power

from .conftest import make_candidate, make_leg

pytest_plugins = ("tests.test_ibkr_placer",)


def setup_lot():
    ledger = Ledger(in_memory_session())
    lots = make_candidate(
        symbol="SPY",
        strategy=Strategy.CALL_CREDIT_SPREAD,
        legs=(
            make_leg(OptionType.CALL, 100, Action.SELL_TO_OPEN),
            make_leg(OptionType.CALL, 105, Action.BUY_TO_OPEN),
        ),
        net_credit=105.5,
        max_profit=105.5,
        buying_power_reduction=394.5,
    )
    trade = ledger.record_planned(lots, 2, "test", TradingMode.SANDBOX)
    assert trade.buying_power == 800.0
    return ledger, trade


def mock_submissions(client):
    orders = []

    def place(contract, order):
        order.orderId = 100 + len(orders)
        order.permId = 1000 + order.orderId
        submitted = SimpleNamespace(
            contract=contract,
            order=order,
            orderStatus=OrderStatus(status="Submitted", filled=0),
            fills=[],
        )
        orders.append(submitted)
        return submitted

    client.trading_ib.placeOrder.side_effect = place
    return orders


async def test_walk_last_limit_is_rounded_up_and_canceled(mock_client):
    ledger, trade = setup_lot()
    orders = mock_submissions(mock_client)

    def cancel(order):
        assert orders[-1].order == order
        orders[-1].orderStatus.status = "Cancelled"

    mock_client.trading_ib.cancelOrder.side_effect = cancel
    placer = IBKRPlacer(
        mock_client,
        walk_interval=0,
        walk_step=0.10,
        index_call_min_credit_width_ratio=0.201,
    )
    out = await Executor(ledger, TradingMode.SANDBOX, placer)._place(trade)
    assert out.action == "error"
    assert [t.order.lmtPrice for t in orders] == [-1.05, -1.01]
    assert len({t.order.orderRef for t in orders}) == 2
    assert trade.status == TradeStatus.CANCELED
    assert all(t.terminal for t in ledger.s.scalars(select(ExecutionOrder)))


@pytest.mark.parametrize("quantity", [0, 1, 2])
async def test_fill_racing_cancel_requires_execution_before_open(mock_client, quantity):
    ledger, trade = setup_lot()
    orders = mock_submissions(mock_client)

    def cancel(order):
        t = orders[-1]
        t.orderStatus.status = "Cancelled" if quantity == 1 else "Filled"
        t.orderStatus.filled = quantity or 2
        t.orderStatus.avgFillPrice = -1.05
        if quantity:
            now = datetime.now(timezone.utc)
            e = Execution(
                execId="execution-1",
                acctNumber=order.account,
                permId=order.permId,
                orderRef=order.orderRef,
                shares=quantity,
                side="BOT",
                price=-1.05,
                time=now,
            )
            t.fills.append(
                Fill(
                    t.contract,
                    e,
                    CommissionReport(execId=e.execId, commission=0.5, currency="USD"),
                    now,
                )
            )

    mock_client.trading_ib.cancelOrder.side_effect = cancel
    placer = IBKRPlacer(mock_client, walk_interval=0)
    result = await Executor(ledger, TradingMode.SANDBOX, placer)._place(trade)
    if quantity == 0:
        assert result.action == "error"
        assert trade.status == TradeStatus.WORKING
        assert len(orders) == 1
        return
    assert result.action == "placed"
    assert trade.status == TradeStatus.OPEN
    assert trade.contracts == quantity
    assert trade.entry_credit == 105 * quantity
    assert trade.entry_commission == 0.5
    assert effective_buying_power(trade) == 395 * quantity
    assert len(orders) == 2
    assert orders[-1].order.totalQuantity == quantity
    assert orders[-1].order.orderRef.startswith("TastyAgent_TP_")
    assert await placer.attach_take_profit(trade) == str(orders[-1].order.orderId)
    assert len(orders) == 2


async def test_unacknowledged_tp_is_never_submitted_twice(mock_client):
    ledger, trade = setup_lot()
    trade.execution_verified = True
    ledger.mark_open(trade)

    def disconnect(*_):
        raise ConnectionError("connection lost after send")

    mock_client.trading_ib.placeOrder.side_effect = disconnect
    placer = IBKRPlacer(mock_client, walk_interval=0)
    with pytest.raises(ConnectionError):
        await placer.attach_take_profit(trade)
    assert await placer.attach_take_profit(trade) is None
    assert mock_client.trading_ib.placeOrder.call_count == 1
