"""Commission capture diagnostics and the once-a-day after-close catch-up."""

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from ib_async import CommissionReport, Execution, Fill
from sqlalchemy import select

from tastyagent import market_calendar
from tastyagent.config import TradingMode
from tastyagent.db.models import BrokerExecution
from tastyagent.db.session import (
    in_memory_session,
    init_db,
    make_engine,
    session_factory,
)
from tastyagent.execution.fees import FeeRecovery, warn_missing_fees
from tastyagent.execution.reconciliation import (
    prepare_order,
    reconcile_broker_activity,
)
from tastyagent.ibkr.client import IBKRClient
from tastyagent.portfolio.ledger import Ledger
from tastyagent.settings import Settings

from .conftest import make_candidate

ACCOUNT = "paper-test"
# 2026-11-27 closed early at 13:00 ET (18:00 UTC).
HALF_DAY = "20261127:0930-20261127:1300;20261130:0930-20261130:1600"
AFTER_CLOSE = datetime(2026, 11, 27, 19, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def half_day_calendar():
    market_calendar.clear()
    market_calendar.load(market_calendar.parse_liquid_hours(HALF_DAY, "US/Eastern"))
    yield
    market_calendar.clear()


def entry_intent(ledger):
    trade = ledger.record_planned(make_candidate(), 1, "test", TradingMode.SANDBOX)
    return prepare_order(ledger, trade, "entry", account=ACCOUNT)


def execution(intent, *, at, leg="P:90.000000:20260215", fee=None):
    return BrokerExecution(
        intent_id=intent.id,
        account=ACCOUNT,
        exec_id=f"exec-{uuid4().hex}",
        leg=leg,
        quantity=1,
        cash=100.0,
        fee=fee,
        ts=at.astimezone(UTC).replace(tzinfo=None),
    )


def ledger_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    init_db(engine)
    return session_factory(engine)


def with_missing_fee(open_session):
    """A filled entry whose leg execution never received its commission."""
    session = open_session()
    intent = entry_intent(Ledger(session))
    intent.status, intent.terminal = "Filled", True
    session.add(execution(intent, at=AFTER_CLOSE - timedelta(hours=4)))
    session.commit()
    session.close()


def reconnecting_client():
    client = MagicMock()
    client.reconnect_trading = AsyncMock(return_value=True)
    return client


def test_rejected_commission_report_is_logged_not_recorded(caplog):
    # Arrange: the report arrives, but not in a currency the ledger books.
    ledger = Ledger(in_memory_session())
    intent = entry_intent(ledger)
    now = datetime.now(UTC)
    e = Execution(
        execId="exec-eur",
        acctNumber=ACCOUNT,
        permId=0,
        orderRef=intent.order_ref,
        shares=1,
        side="BOT",
        price=-2.5,
        time=now,
    )
    report = CommissionReport(execId="exec-eur", commission=1.1, currency="EUR")
    fill = Fill(NS(secType="BAG", symbol="SPY"), e, report, now)

    # Act
    with caplog.at_level(logging.WARNING):
        reconcile_broker_activity(ledger, [], [fill], account=ACCOUNT)

    # Assert
    row = ledger.s.scalar(select(BrokerExecution))
    assert row.fee is None
    assert "exec-eur" in caplog.text and "EUR" in caplog.text


def test_missing_fee_warning_names_each_overdue_execution_once(caplog):
    # Arrange
    session = in_memory_session()
    intent = entry_intent(Ledger(session))
    now = datetime.now(UTC)
    overdue = execution(intent, at=now - timedelta(minutes=20))
    combo = execution(intent, at=now - timedelta(minutes=20), leg="BAG")
    booked = execution(intent, at=now - timedelta(minutes=20), fee=1.05)
    fresh = execution(intent, at=now - timedelta(minutes=2))
    session.add_all([overdue, combo, booked, fresh])
    session.commit()

    # Act
    with caplog.at_level(logging.WARNING):
        warn_missing_fees(session, now)
        warn_missing_fees(session, now)

    # Assert
    assert caplog.text.count(overdue.exec_id) == 1
    for quiet in (combo, booked, fresh):
        assert quiet.exec_id not in caplog.text


async def test_reconnects_once_after_the_close_when_fees_are_missing(tmp_path):
    # Arrange
    open_session = ledger_db(tmp_path)
    with_missing_fee(open_session)
    recovery = FeeRecovery(settle_seconds=0)
    client = reconnecting_client()
    resync = AsyncMock()

    # Act
    first = await recovery.run(client, open_session, resync, now=AFTER_CLOSE)
    again = await recovery.run(
        client, open_session, resync, now=AFTER_CLOSE + timedelta(minutes=5)
    )

    # Assert
    assert first and not again
    client.reconnect_trading.assert_awaited_once()
    assert resync.await_count == 2  # before and after the reconnect


async def test_waits_for_the_close_before_reconnecting(tmp_path):
    # Arrange
    open_session = ledger_db(tmp_path)
    with_missing_fee(open_session)
    client = reconnecting_client()
    before_close = datetime(2026, 11, 27, 17, 30, tzinfo=UTC)

    # Act
    ran = await FeeRecovery(settle_seconds=0).run(
        client, open_session, AsyncMock(), now=before_close
    )

    # Assert
    assert not ran
    client.reconnect_trading.assert_not_awaited()


async def test_never_reconnects_while_an_order_is_in_flight(tmp_path):
    # Arrange
    open_session = ledger_db(tmp_path)
    with_missing_fee(open_session)
    session = open_session()
    walking = entry_intent(Ledger(session))
    walking.created_at = (AFTER_CLOSE - timedelta(minutes=2)).replace(tzinfo=None)
    session.commit()
    session.close()
    client = reconnecting_client()

    # Act
    ran = await FeeRecovery(settle_seconds=0).run(
        client, open_session, AsyncMock(), now=AFTER_CLOSE
    )

    # Assert
    assert not ran
    client.reconnect_trading.assert_not_awaited()


async def test_skips_the_reconnect_when_no_fee_is_missing(tmp_path):
    # Arrange
    open_session = ledger_db(tmp_path)
    client = reconnecting_client()

    # Act
    ran = await FeeRecovery(settle_seconds=0).run(
        client, open_session, AsyncMock(), now=AFTER_CLOSE
    )

    # Assert
    assert not ran
    client.reconnect_trading.assert_not_awaited()


async def test_a_resync_alone_can_book_the_missing_fees(tmp_path):
    # Arrange: the live session already holds the reports.
    open_session = ledger_db(tmp_path)
    with_missing_fee(open_session)
    client = reconnecting_client()

    async def book_fees():
        session = open_session()
        for row in session.scalars(select(BrokerExecution)):
            row.fee = 1.04
        session.commit()
        session.close()

    # Act
    ran = await FeeRecovery(settle_seconds=0).run(
        client, open_session, book_fees, now=AFTER_CLOSE
    )

    # Assert
    assert not ran
    client.reconnect_trading.assert_not_awaited()


async def test_still_resyncs_when_the_reconnect_fails(tmp_path):
    # Arrange
    open_session = ledger_db(tmp_path)
    with_missing_fee(open_session)
    client = reconnecting_client()
    client.reconnect_trading.return_value = False
    resync = AsyncMock()

    # Act
    ran = await FeeRecovery(settle_seconds=0, retry_wait_seconds=0).run(
        client, open_session, resync, now=AFTER_CLOSE
    )

    # Assert
    assert ran
    assert resync.await_count == 2


async def test_never_reconnects_a_live_session(tmp_path):
    # Arrange
    open_session = ledger_db(tmp_path)
    with_missing_fee(open_session)
    client = reconnecting_client()

    # Act
    ran = await FeeRecovery(settle_seconds=0).run(
        client, open_session, AsyncMock(), now=AFTER_CLOSE, live=True
    )

    # Assert
    assert not ran
    client.reconnect_trading.assert_not_awaited()


async def test_ignores_fills_inside_the_grace_period(tmp_path):
    # Arrange: a fill two minutes ago whose report may still be on its way.
    open_session = ledger_db(tmp_path)
    session = open_session()
    intent = entry_intent(Ledger(session))
    intent.status, intent.terminal = "Filled", True
    session.add(execution(intent, at=AFTER_CLOSE - timedelta(minutes=2)))
    session.commit()
    session.close()
    client = reconnecting_client()

    # Act
    ran = await FeeRecovery(settle_seconds=0).run(
        client, open_session, AsyncMock(), now=AFTER_CLOSE
    )

    # Assert
    assert not ran
    client.reconnect_trading.assert_not_awaited()


async def test_reconnect_trading_reopens_the_sandbox_session(monkeypatch):
    # Arrange
    monkeypatch.setenv("TASTYAGENT_MODE", "sandbox")
    client = IBKRClient(Settings(ibkr_account="U123456"))
    client.trading_ib = MagicMock()
    client.trading_ib.isConnected.return_value = True
    client._try_connect_trading = AsyncMock(return_value=True)
    client._monitor_task = MagicMock(done=MagicMock(return_value=False))
    client.stop_connection_monitor = MagicMock()
    client.start_connection_monitor = MagicMock()

    # Act
    reconnected = await client.reconnect_trading()

    # Assert
    assert reconnected
    client.stop_connection_monitor.assert_called_once()
    client.trading_ib.disconnect.assert_called_once()
    client._try_connect_trading.assert_awaited_once()
    client.start_connection_monitor.assert_called_once()
