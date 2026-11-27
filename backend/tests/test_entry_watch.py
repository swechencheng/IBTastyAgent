"""Entry orders the placer stopped managing are resolved against IB after 15 minutes."""

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

from tastyagent.config import TradingMode
from tastyagent.db.models import TradeStatus
from tastyagent.db.session import in_memory_session
from tastyagent.execution.entry_watch import check_unresolved_entries
from tastyagent.execution.reconciliation import bind_order, prepare_order
from tastyagent.execution.tracker import sync_positions_and_marks
from tastyagent.portfolio.ledger import Ledger

from .conftest import make_candidate

ACCOUNT = "paper-test"
NOW = datetime(2026, 11, 30, 15, 0, tzinfo=UTC)
ABANDONED = timedelta(minutes=20)


def abandoned_entry(ledger, *, age=ABANDONED, order_id=None, now=NOW):
    """A WORKING entry whose last order the placer stopped managing ``age`` ago.

    Without ``order_id`` the intent stays ``Prepared``: placeOrder raised after
    the journal entry was written.
    """
    trade = ledger.record_planned(
        make_candidate(symbol="NVDA"), 1, "test", TradingMode.SANDBOX
    )
    intent = prepare_order(ledger, trade, "entry", account=ACCOUNT)
    trade.order_ref = intent.order_ref
    if order_id is not None:
        trade.broker_order_id = str(order_id)
        bind_order(
            ledger,
            intent,
            order_id=order_id,
            perm_id=9_000 + order_id,
            status="Submitted",
        )
    trade.created_at = intent.created_at = (now - age).replace(tzinfo=None)
    ledger.s.commit()
    return trade, intent


def broker_order(intent, status, *, filled=0.0):
    order = NS(
        orderRef=intent.order_ref,
        orderId=int(intent.order_id or 0),
        permId=int(intent.perm_id or 0),
        account=ACCOUNT,
        filledQuantity=filled,
    )
    return NS(
        order=order,
        orderStatus=NS(status=status, filled=0.0, permId=order.permId),
        fills=[],
        log=[],
    )


def broker(open_orders=(), completed=()):
    ib = MagicMock()
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=list(open_orders))
    ib.reqCompletedOrdersAsync = AsyncMock(return_value=list(completed))
    return ib


def held_leg(trade):
    leg = trade.legs[0]
    right = str(getattr(leg.option_type, "value", leg.option_type))[:1].upper()
    contract = NS(
        symbol=trade.symbol,
        secType="OPT",
        right=right,
        strike=leg.strike,
        lastTradeDateOrContractMonth=str(leg.expiration).replace("-", ""),
    )
    return NS(contract=contract, position=-1)


async def test_cancels_an_entry_order_still_working_after_15_minutes():
    # Arrange
    ledger = Ledger(in_memory_session())
    trade, intent = abandoned_entry(ledger, order_id=2601)
    live = broker_order(intent, "Submitted")
    ib = broker(open_orders=[live])

    # Act
    await check_unresolved_entries(ledger, ib, ACCOUNT, [], [], now=NOW)

    # Assert
    ib.cancelOrder.assert_called_once_with(live.order)
    assert not intent.terminal
    assert trade.status is TradeStatus.WORKING


async def test_settles_an_entry_the_broker_reports_cancelled():
    # Arrange
    ledger = Ledger(in_memory_session())
    trade, intent = abandoned_entry(ledger, order_id=2601)
    ib = broker(completed=[broker_order(intent, "Cancelled", filled=0.0)])

    # Act
    settled = await check_unresolved_entries(ledger, ib, ACCOUNT, [], [], now=NOW)

    # Assert
    assert settled == 1
    assert intent.terminal and intent.applied == 0
    assert trade.broker_order_id is None
    ib.cancelOrder.assert_not_called()


async def test_treats_an_order_ib_never_saw_as_unfilled():
    # Arrange
    ledger = Ledger(in_memory_session())
    trade, intent = abandoned_entry(ledger)

    # Act
    settled = await check_unresolved_entries(ledger, broker(), ACCOUNT, [], [], now=NOW)

    # Assert
    assert settled == 1
    assert intent.terminal and intent.status == "NotFound"
    assert trade.status is TradeStatus.WORKING  # the stale cleanup retires the row


async def test_keeps_an_unknown_order_while_its_legs_are_held(caplog):
    # Arrange
    ledger = Ledger(in_memory_session())
    trade, intent = abandoned_entry(ledger)

    # Act
    with caplog.at_level(logging.WARNING):
        settled = await check_unresolved_entries(
            ledger, broker(), ACCOUNT, [], [held_leg(trade)], now=NOW
        )

    # Assert
    assert settled == 0
    assert not intent.terminal
    assert intent.order_ref in caplog.text


async def test_keeps_a_cancelled_order_that_reports_filled_contracts(caplog):
    # Arrange: IB says the order ended with a fill the journal never saw.
    ledger = Ledger(in_memory_session())
    _, intent = abandoned_entry(ledger, order_id=2601)
    ib = broker(completed=[broker_order(intent, "Cancelled", filled=1.0)])

    # Act
    with caplog.at_level(logging.WARNING):
        settled = await check_unresolved_entries(ledger, ib, ACCOUNT, [], [], now=NOW)

    # Assert
    assert settled == 0
    assert not intent.terminal
    assert intent.order_ref in caplog.text


async def test_leaves_recent_entries_to_the_placer():
    # Arrange
    ledger = Ledger(in_memory_session())
    abandoned_entry(ledger, age=timedelta(minutes=5), order_id=2601)
    ib = broker()

    # Act
    settled = await check_unresolved_entries(ledger, ib, ACCOUNT, [], [], now=NOW)

    # Assert
    assert settled == 0
    ib.reqAllOpenOrdersAsync.assert_not_awaited()


async def test_sync_retires_an_entry_ib_never_saw():
    # Arrange
    session = in_memory_session()
    ledger = Ledger(session)
    trade, intent = abandoned_entry(ledger, now=datetime.now(UTC))
    active_ib = broker()
    active_ib.isConnected.return_value = True
    active_ib.trades.return_value = []
    active_ib.fills.return_value = []
    active_ib.reqExecutionsAsync = AsyncMock(return_value=[])
    active_ib.positions.return_value = []
    active_ib.portfolio.return_value = []
    client = MagicMock(account=ACCOUNT, active_trading_ib=active_ib)

    # Act
    await sync_positions_and_marks(client=client, session=session)

    # Assert
    assert intent.terminal
    assert trade.status is TradeStatus.CANCELED
