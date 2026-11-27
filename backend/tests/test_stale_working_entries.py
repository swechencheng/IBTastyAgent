"""Stale WORKING entries are settled by the entry journal, not by symbol-level positions.

Regression for the 2026-11-27 ghosts (#14, #26 NVDA and #67 AVGO): the placer gave up
with "Entry cancellation unconfirmed", the broker later confirmed every entry order
cancelled with zero fills, yet the rows stayed WORKING because the account still held
other option lots on the same underlying.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select

from tastyagent.config import TradingMode
from tastyagent.db.models import Trade, TradeEvent, TradeStatus
from tastyagent.db.session import in_memory_session
from tastyagent.execution.reconciliation import (
    bind_order,
    prepare_order,
    reconcile_broker_activity,
)
from tastyagent.execution.tracker import (
    reconcile_stale_working_trades,
    sync_positions_and_marks,
)
from tastyagent.portfolio.ledger import Ledger

from .conftest import make_candidate

ACCOUNT = "paper-test"
NOW = datetime(2026, 11, 27, 18, 30, tzinfo=UTC)
STALE_AFTER_SECONDS = 600.0
FIRST_ORDER_ID = 2598


def other_lot_on(symbol):
    """A held option position on the same underlying that belongs to another lot."""
    return NS(contract=NS(symbol=symbol, secType="OPT"), position=-2)


def broker_view(intent, status, filled=0.0):
    """The broker's report of one entry order, as ib_async exposes it."""
    perm_id = int(intent.perm_id)
    return NS(
        order=NS(
            orderRef=intent.order_ref,
            orderId=int(intent.order_id),
            permId=perm_id,
            account=ACCOUNT,
        ),
        orderStatus=NS(status=status, filled=filled, permId=perm_id),
        fills=[],
        log=[],
    )


def submit_attempt(ledger, trade, *, order_id, at):
    """Mirror IBKRPlacer.submit: journal the intent first, then bind broker identity."""
    intent = prepare_order(ledger, trade, "entry", account=ACCOUNT)
    trade.order_ref = intent.order_ref
    trade.broker_order_id = str(order_id)
    bind_order(
        ledger,
        intent,
        order_id=order_id,
        perm_id=987_000_000 + order_id,
        status="Submitted",
    )
    intent.created_at = at.replace(tzinfo=None)
    ledger.s.commit()
    return intent


def walked_entry(ledger, symbol, *, attempts, started_at, step=timedelta(seconds=10)):
    """A walk-the-book entry whose earlier attempts the broker confirmed cancelled.

    The last attempt is left as the placer abandons it on "Entry cancellation
    unconfirmed": still bound to its broker order and not yet terminal.
    """
    trade = ledger.record_planned(
        make_candidate(symbol=symbol), 1, "test", TradingMode.SANDBOX
    )
    trade.created_at = started_at.replace(tzinfo=None)
    ledger.s.commit()
    intents = []
    for n in range(attempts):
        intent = submit_attempt(
            ledger, trade, order_id=FIRST_ORDER_ID + n, at=started_at + n * step
        )
        intents.append(intent)
        if n < attempts - 1:
            confirm_cancel(ledger, intent)
    return trade, intents


def confirm_cancel(ledger, intent):
    ended = broker_view(intent, "Cancelled")
    reconcile_broker_activity(ledger, [ended], [], account=ACCOUNT)
    assert intent.terminal and intent.applied == 0
    return ended


def test_cancels_entry_whose_orders_all_ended_unfilled_despite_other_lot_on_symbol():
    # Arrange: like #14, four attempts; the broker later confirms the last cancel.
    ledger = Ledger(in_memory_session())
    trade, intents = walked_entry(
        ledger, "NVDA", attempts=4, started_at=NOW - timedelta(hours=3)
    )
    ended = confirm_cancel(ledger, intents[-1])

    # Act
    canceled = reconcile_stale_working_trades(
        ledger,
        ib_trades=[ended],
        positions=[other_lot_on("NVDA")],
        stale_after_seconds=STALE_AFTER_SECONDS,
        now=NOW,
        mode=TradingMode.SANDBOX,
    )

    # Assert
    assert canceled == 1
    assert trade.status is TradeStatus.CANCELED
    kinds = ledger.s.scalars(
        select(TradeEvent.kind).where(TradeEvent.trade_id == trade.id)
    ).all()
    assert kinds[-1] == "canceled"
    assert "never filled" in trade.exit_reason
    assert len(trade.exit_reason) <= Trade.__table__.c.exit_reason.type.length


def test_keeps_entry_while_its_cancel_is_still_unconfirmed():
    # Arrange: the abandoned attempt has no broker verdict yet and the broker no
    # longer lists it (e.g. after a gateway restart): only the journal keeps the row.
    ledger = Ledger(in_memory_session())
    trade, _ = walked_entry(
        ledger, "AVGO", attempts=2, started_at=NOW - timedelta(hours=3)
    )

    # Act
    canceled = reconcile_stale_working_trades(
        ledger,
        ib_trades=[],
        positions=[],
        stale_after_seconds=STALE_AFTER_SECONDS,
        now=NOW,
        mode=TradingMode.SANDBOX,
    )

    # Assert
    assert canceled == 0
    assert trade.status is TradeStatus.WORKING


def test_keeps_entry_while_the_broker_still_reports_its_order_working():
    # Arrange: the journal says the attempt ended, but the broker still lists it live.
    ledger = Ledger(in_memory_session())
    trade, intents = walked_entry(
        ledger, "NVDA", attempts=1, started_at=NOW - timedelta(hours=3)
    )
    confirm_cancel(ledger, intents[-1])

    # Act
    canceled = reconcile_stale_working_trades(
        ledger,
        ib_trades=[broker_view(intents[-1], "Submitted")],
        positions=[],
        stale_after_seconds=STALE_AFTER_SECONDS,
        now=NOW,
        mode=TradingMode.SANDBOX,
    )

    # Assert
    assert canceled == 0
    assert trade.status is TradeStatus.WORKING


def test_keeps_entry_whose_latest_attempt_is_recent_even_if_the_row_is_old():
    # Arrange: a row planned hours ago (e.g. approved late) that is walking now.
    # Between a confirmed cancel and the replacement submit, every attempt is terminal.
    ledger = Ledger(in_memory_session())
    trade, intents = walked_entry(
        ledger,
        "NVDA",
        attempts=1,
        started_at=NOW - timedelta(seconds=30),
    )
    trade.created_at = (NOW - timedelta(hours=5)).replace(tzinfo=None)
    ledger.s.commit()
    ended = confirm_cancel(ledger, intents[-1])

    # Act
    canceled = reconcile_stale_working_trades(
        ledger,
        ib_trades=[ended],
        positions=[],
        stale_after_seconds=STALE_AFTER_SECONDS,
        now=NOW,
        mode=TradingMode.SANDBOX,
    )

    # Assert
    assert canceled == 0
    assert trade.status is TradeStatus.WORKING


def test_never_cancels_entry_with_verified_fills():
    # Arrange: defense in depth. _apply_units normally flips such a row to OPEN, but
    # a journal that holds applied fills must never be cleaned up as unfilled.
    ledger = Ledger(in_memory_session())
    trade, intents = walked_entry(
        ledger, "NVDA", attempts=1, started_at=NOW - timedelta(hours=3)
    )
    ended = confirm_cancel(ledger, intents[-1])
    intents[-1].applied = 1
    ledger.s.commit()

    # Act
    canceled = reconcile_stale_working_trades(
        ledger,
        ib_trades=[ended],
        positions=[],
        stale_after_seconds=STALE_AFTER_SECONDS,
        now=NOW,
        mode=TradingMode.SANDBOX,
    )

    # Assert
    assert canceled == 0
    assert trade.status is TradeStatus.WORKING


async def test_sync_retires_entry_once_broker_confirms_its_unconfirmed_cancel():
    # Arrange: the placer abandoned attempt #3 on an unconfirmed cancel; the broker
    # now reports it Cancelled with nothing filled while another NVDA lot is held.
    session = in_memory_session()
    ledger = Ledger(session)
    trade, intents = walked_entry(
        ledger,
        "NVDA",
        attempts=3,
        started_at=datetime.now(UTC) - timedelta(hours=3),
    )
    active_ib = MagicMock()
    active_ib.isConnected.return_value = True
    active_ib.trades.return_value = [broker_view(intents[-1], "Cancelled")]
    active_ib.fills.return_value = []
    active_ib.reqExecutionsAsync = AsyncMock(return_value=[])
    active_ib.positions.return_value = [other_lot_on("NVDA")]
    active_ib.portfolio.return_value = []
    client = MagicMock(account=ACCOUNT, active_trading_ib=active_ib)

    # Act
    await sync_positions_and_marks(client=client, session=session)

    # Assert
    assert intents[-1].terminal and intents[-1].status == "Cancelled"
    assert trade.broker_order_id is None
    assert trade.status is TradeStatus.CANCELED
