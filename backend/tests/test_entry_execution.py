"""Broker callbacks, delayed fills and the limit-price boundary on opening orders."""

import asyncio
from datetime import UTC, datetime, timezone
from types import SimpleNamespace

import pytest
from ib_async import CommissionReport, Execution, Fill, OrderStatus
from sqlalchemy import select

from tastyagent.config import TradingMode
from tastyagent.db.models import ExecutionOrder, TradeEvent, TradeStatus
from tastyagent.db.session import (
    in_memory_session,
    init_db,
    make_engine,
    session_factory,
)
from tastyagent.execution.executor import Executor
from tastyagent.execution.reconciliation import (
    bind_order,
    prepare_order,
    reconcile_broker_activity,
)
from tastyagent.ibkr.placer import IBKRPlacer
from tastyagent.models import Action, OptionType, Strategy
from tastyagent.portfolio.ledger import Ledger
from tastyagent.strategy.sizing import effective_buying_power

from .conftest import make_candidate, make_leg

pytest_plugins = ("tests.test_ibkr_placer",)


def setup_lot(session=None):
    ledger = Ledger(session or in_memory_session())
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
    placer = IBKRPlacer(mock_client, walk_interval=0, execution_wait=0)
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


def execution_of(placed, quantity, exec_id="execution-late"):
    now = datetime.now(UTC)
    e = Execution(
        execId=exec_id,
        acctNumber=placed.order.account,
        permId=placed.order.permId,
        orderRef=placed.order.orderRef,
        shares=quantity,
        side="BOT",
        price=-1.05,
        time=now,
    )
    report = CommissionReport(execId=exec_id, commission=0.5, currency="USD")
    return Fill(placed.contract, e, report, now)


async def test_entry_waits_for_executions_that_trail_the_filled_status(mock_client):
    # Arrange: IB reports the entry Filled before it delivers the execution details.
    ledger, trade = setup_lot()
    orders = mock_submissions(mock_client)
    submit = mock_client.trading_ib.placeOrder.side_effect
    loop = asyncio.get_running_loop()

    def fill_entry_with_late_executions(contract, order):
        placed = submit(contract, order)
        if len(orders) == 1:
            placed.orderStatus = OrderStatus(
                status="Filled", filled=2, avgFillPrice=-1.05
            )
            loop.call_later(0.3, placed.fills.append, execution_of(placed, 2))
        return placed

    mock_client.trading_ib.placeOrder.side_effect = fill_entry_with_late_executions
    placer = IBKRPlacer(mock_client, walk_interval=0)

    # Act
    result = await Executor(ledger, TradingMode.SANDBOX, placer)._place(trade)

    # Assert
    assert result.action == "placed"
    assert trade.status == TradeStatus.OPEN
    assert trade.contracts == 2
    assert orders[-1].order.orderRef.startswith("TastyAgent_TP_")
    events = ledger.s.execute(
        select(TradeEvent.kind, TradeEvent.detail).where(
            TradeEvent.trade_id == trade.id
        )
    ).all()
    assert [kind for kind, _ in events].count("open") == 1
    assert not any("placement error" in detail for _, detail in events)
    entry = ledger.s.scalar(
        select(ExecutionOrder).where(
            ExecutionOrder.trade_id == trade.id, ExecutionOrder.kind == "entry"
        )
    )
    assert entry.terminal and entry.applied == 2


async def test_fill_check_sees_a_lot_the_background_sync_already_applied(
    tmp_path, mock_client
):
    # Arrange: the placer and the background sync hold separate sessions on one
    # ledger. The sync applies the first lot between the two partial fills.
    engine = make_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    init_db(engine)
    new_session = session_factory(engine)
    ledger, trade = setup_lot(new_session())
    account = "DU123456"
    intent = prepare_order(ledger, trade, "entry", account=account)
    bind_order(ledger, intent, order_id=100, perm_id=1100, status="Submitted")
    ledger.s.refresh(trade)  # the placer last saw the row WORKING
    order = SimpleNamespace(
        contract=SimpleNamespace(secType="BAG", symbol="SPY"),
        order=SimpleNamespace(
            orderId=100, permId=1100, orderRef=intent.order_ref, account=account
        ),
        orderStatus=OrderStatus(status="Submitted", filled=1),
        fills=[],
        log=[],
    )
    order.fills.append(execution_of(order, 1, "lot-1"))
    reconcile_broker_activity(Ledger(new_session()), [order], [], account=account)
    order.orderStatus = OrderStatus(status="Filled", filled=2)
    order.fills.append(execution_of(order, 1, "lot-2"))
    placer = IBKRPlacer(mock_client, execution_wait=0)

    # Act
    verified = await placer._await_verified_entry(ledger, intent, trade, order, account)

    # Assert
    assert verified
    assert trade.status == TradeStatus.OPEN
    assert trade.contracts == 2


@pytest.mark.parametrize("wait", [float("nan"), float("inf"), -1.0])
def test_placer_rejects_an_unbounded_execution_wait(mock_client, wait):
    with pytest.raises(ValueError):
        IBKRPlacer(mock_client, execution_wait=wait)


async def test_entry_walk_stops_when_the_market_closes(mock_client, monkeypatch):
    # Arrange: the first price is not filled and the market closes mid-walk.
    ledger, trade = setup_lot()
    orders = mock_submissions(mock_client)

    def cancel(order):
        orders[-1].orderStatus.status = "Cancelled"

    mock_client.trading_ib.cancelOrder.side_effect = cancel
    monkeypatch.setattr("tastyagent.ibkr.placer.is_market_open", lambda: False)
    placer = IBKRPlacer(
        mock_client,
        walk_interval=0,
        walk_step=0.10,
        index_call_min_credit_width_ratio=0.201,
    )

    # Act
    result = await Executor(ledger, TradingMode.SANDBOX, placer)._place(trade)

    # Assert
    assert result.action == "error"
    assert len(orders) == 1
    assert trade.status == TradeStatus.CANCELED


def test_placer_rejects_a_walk_interval_beyond_the_cap(mock_client):
    with pytest.raises(ValueError):
        IBKRPlacer(mock_client, walk_interval=900)
