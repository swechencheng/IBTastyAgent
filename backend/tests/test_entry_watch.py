"""Entry orders the placer stopped managing are resolved against IB."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from tastyagent.config import TradingMode
from tastyagent.db.models import BrokerExecution, TradeEvent, TradeStatus
from tastyagent.db.session import in_memory_session
from tastyagent.execution import entry_watch
from tastyagent.execution.entry_watch import check_unresolved_entries
from tastyagent.execution.reconciliation import bind_order, prepare_order
from tastyagent.execution.tracker import sync_positions_and_marks
from tastyagent.market_calendar import ET
from tastyagent.portfolio.ledger import Ledger

from .conftest import make_candidate

ACCOUNT = "paper-test"
NOW = datetime(2026, 11, 30, 15, 0, tzinfo=UTC)
ABANDONED = timedelta(minutes=20)


def abandoned_entry(ledger, *, age=ABANDONED, order_id=None, now=NOW, contracts=1):
    """A WORKING entry whose last order the placer stopped managing ``age`` ago.

    Without ``order_id`` the intent stays ``Prepared``: placeOrder raised after
    the journal entry was written.
    """
    trade = ledger.record_planned(
        make_candidate(symbol="NVDA"), contracts, "test", TradingMode.SANDBOX
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


def synced_client(accounts=(ACCOUNT,)):
    active_ib = broker()
    active_ib.isConnected.return_value = True
    active_ib.managedAccounts.return_value = list(accounts)
    active_ib.trades.return_value = []
    active_ib.fills.return_value = []
    active_ib.reqExecutionsAsync = AsyncMock(return_value=[])
    active_ib.positions.return_value = []
    active_ib.portfolio.return_value = []
    return MagicMock(account=ACCOUNT, active_trading_ib=active_ib)


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


def order_checks(ledger, trade):
    return ledger.s.scalars(
        select(TradeEvent.detail).where(
            TradeEvent.trade_id == trade.id, TradeEvent.kind == "order_check"
        )
    ).all()


async def check(ledger, ib, positions=(), *, verified=True, **kwargs):
    return await check_unresolved_entries(
        ledger,
        ib,
        ACCOUNT,
        [],
        list(positions),
        executions_verified=verified,
        now=NOW,
        **kwargs,
    )


async def test_cancels_an_entry_order_still_working_after_15_minutes():
    # Arrange
    ledger = Ledger(in_memory_session())
    trade, intent = abandoned_entry(ledger, order_id=2601)
    live = broker_order(intent, "Submitted")
    ib = broker(open_orders=[live])

    # Act
    await check(ledger, ib)

    # Assert
    ib.cancelOrder.assert_called_once_with(live.order)
    assert not intent.terminal
    assert trade.status is TradeStatus.WORKING


async def test_records_the_cancel_event_once_per_order():
    # Arrange
    ledger = Ledger(in_memory_session())
    trade, intent = abandoned_entry(ledger, order_id=2601)
    ib = broker(open_orders=[broker_order(intent, "Submitted")])

    # Act
    await check(ledger, ib)
    await check(ledger, ib)

    # Assert
    assert ib.cancelOrder.call_count == 2
    assert sum("cancel sent" in detail for detail in order_checks(ledger, trade)) == 1


async def test_cancels_the_live_remainder_of_a_partially_filled_entry():
    # Arrange: one of two contracts filled; the remainder is still working.
    ledger = Ledger(in_memory_session())
    trade, intent = abandoned_entry(ledger, order_id=2601, contracts=2)
    ledger.s.add(
        BrokerExecution(
            intent_id=intent.id,
            account=ACCOUNT,
            exec_id="partial-1",
            leg="BAG",
            quantity=1,
            cash=100.0,
            ts=(NOW - ABANDONED).replace(tzinfo=None),
        )
    )
    intent.applied, intent.applied_cash = 1, 100.0
    trade.status = TradeStatus.OPEN
    ledger.s.commit()
    live = broker_order(intent, "Submitted")
    ib = broker(open_orders=[live])

    # Act
    await check(ledger, ib)

    # Assert
    ib.cancelOrder.assert_called_once_with(live.order)
    assert trade.status is TradeStatus.OPEN


async def test_settles_an_entry_the_broker_reports_cancelled():
    # Arrange
    ledger = Ledger(in_memory_session())
    trade, intent = abandoned_entry(ledger, order_id=2601)
    ib = broker(completed=[broker_order(intent, "Cancelled", filled=0.0)])

    # Act
    settled = await check(ledger, ib)

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
    settled = await check(ledger, broker())

    # Assert
    assert settled == 1
    assert intent.terminal and intent.status == "NotFound"
    assert trade.status is TradeStatus.WORKING  # the stale cleanup retires the row


async def test_flags_an_unknown_order_when_executions_are_unverified():
    # Arrange: this cycle's execution history fell back to the cache.
    ledger = Ledger(in_memory_session())
    trade, intent = abandoned_entry(ledger)

    # Act
    settled = await check(ledger, broker(), verified=False)

    # Assert
    assert settled == 0
    assert not intent.terminal
    assert any("needs review" in detail for detail in order_checks(ledger, trade))


async def test_flags_an_unknown_order_from_an_earlier_day():
    # Arrange: IB no longer reports a previous day's fills.
    ledger = Ledger(in_memory_session())
    trade, intent = abandoned_entry(ledger, age=timedelta(days=2))

    # Act
    settled = await check(ledger, broker())

    # Assert
    assert settled == 0
    assert not intent.terminal
    assert any("needs review" in detail for detail in order_checks(ledger, trade))


async def test_keeps_an_unknown_order_while_its_legs_are_held(caplog):
    # Arrange
    ledger = Ledger(in_memory_session())
    trade, intent = abandoned_entry(ledger)

    # Act
    with caplog.at_level(logging.WARNING):
        settled = await check(ledger, broker(), [held_leg(trade)])

    # Assert
    assert settled == 0
    assert not intent.terminal
    assert intent.order_ref in caplog.text
    assert any("needs review" in detail for detail in order_checks(ledger, trade))


async def test_keeps_a_cancelled_order_that_reports_filled_contracts(caplog):
    # Arrange: IB says the order ended with a fill the journal never saw.
    ledger = Ledger(in_memory_session())
    _, intent = abandoned_entry(ledger, order_id=2601)
    ib = broker(completed=[broker_order(intent, "Cancelled", filled=1.0)])

    # Act
    with caplog.at_level(logging.WARNING):
        settled = await check(ledger, ib)

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
    settled = await check(ledger, ib)

    # Assert
    assert settled == 0
    ib.reqAllOpenOrdersAsync.assert_not_awaited()


async def test_checks_entries_left_by_a_previous_process_at_once():
    # Arrange: the intent is young, but no placer in this process owns it.
    ledger = Ledger(in_memory_session())
    _, intent = abandoned_entry(ledger, age=timedelta(minutes=2))

    # Act
    settled = await check(ledger, broker(), unmanaged_before=NOW - timedelta(minutes=1))

    # Assert
    assert settled == 1
    assert intent.status == "NotFound"


async def test_skips_the_check_when_ib_order_lookups_time_out(monkeypatch):
    # Arrange: another request orphaned ours, so IB never answers it.
    monkeypatch.setattr(entry_watch, "IB_REQUEST_TIMEOUT_SECONDS", 0.01)
    ledger = Ledger(in_memory_session())
    _, intent = abandoned_entry(ledger)
    ib = broker()
    ib.reqCompletedOrdersAsync = MagicMock(
        return_value=asyncio.get_running_loop().create_future()
    )

    # Act
    settled = await check(ledger, ib)

    # Assert
    assert settled == 0
    assert not intent.terminal


async def test_one_failing_intent_does_not_stop_the_others():
    # Arrange
    ledger = Ledger(in_memory_session())
    _, live_intent = abandoned_entry(ledger, order_id=2601)
    _, lost_intent = abandoned_entry(ledger)
    ib = broker(open_orders=[broker_order(live_intent, "Submitted")])
    ib.cancelOrder.side_effect = ConnectionError("Not connected")

    # Act
    settled = await check(ledger, ib)

    # Assert
    assert settled == 1
    assert lost_intent.status == "NotFound"
    assert not live_intent.terminal


async def test_sync_retires_an_entry_ib_never_saw():
    # Arrange
    now = datetime.now(UTC)
    if (now - ABANDONED).astimezone(ET).date() != now.astimezone(ET).date():
        pytest.skip("the abandoned entry would fall on the previous ET day")
    session = in_memory_session()
    trade, intent = abandoned_entry(Ledger(session), now=now)

    # Act
    await sync_positions_and_marks(client=synced_client(), session=session)

    # Assert
    assert intent.terminal
    assert trade.status is TradeStatus.CANCELED


async def test_sync_skips_the_check_on_a_session_without_the_account():
    # Arrange: a live mode fell back to a session that does not own the account.
    session = in_memory_session()
    trade, intent = abandoned_entry(Ledger(session), now=datetime.now(UTC))

    # Act
    await sync_positions_and_marks(
        client=synced_client(accounts=("DU-OTHER",)), session=session
    )

    # Assert
    assert not intent.terminal
    assert trade.status is TradeStatus.WORKING
