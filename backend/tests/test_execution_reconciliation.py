from datetime import UTC, datetime
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tastyagent.config import TradingMode
from tastyagent.db.models import BrokerExecution, TradeStatus
from tastyagent.db.session import init_db, make_engine, session_factory
from tastyagent.execution.reconciliation import (
    bind_order,
    pending_order,
    prepare_order,
    reconcile_broker_activity,
)
from tastyagent.portfolio.ledger import Ledger

from .conftest import make_candidate

ACCOUNT = "paper-test"


@pytest.fixture
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(engine)
    yield session_factory(engine)
    engine.dispose()


def position(ledger, qty=2):
    lot = ledger.record_planned(make_candidate(), qty, "original", TradingMode.SANDBOX)
    ledger.mark_open(lot)
    return lot


def fill(ref, identity, qty, price, side, *, fee=None, account=ACCOUNT, leg=None):
    contract = NS(secType="BAG", symbol="SPY")
    if leg:
        contract = NS(
            secType="OPT",
            symbol="SPY",
            right=leg.option_type[0].upper(),
            strike=leg.strike,
            lastTradeDateOrContractMonth=str(leg.expiration),
        )
    return NS(
        contract=contract,
        execution=NS(
            execId=identity,
            acctNumber=account,
            shares=qty,
            price=price,
            side=side,
            time=datetime.now(UTC),
            orderRef=ref,
        ),
        commissionReport=NS(
            execId=identity if fee is not None else "", currency="USD", commission=fee
        ),
    )


def order(ref, status, units, fills=()):
    return NS(
        order=NS(orderRef=ref, orderId=42, account=ACCOUNT),
        orderStatus=NS(status=status, filled=units),
        fills=fills,
    )


def test_close_only_on_exec_with_fee_and_replay_after_restart(db):
    with db() as s:
        ledger = Ledger(s)
        lot = position(ledger)
        i = prepare_order(ledger, lot, "close", account=ACCOUNT)
        ref, lot_id = i.order_ref, lot.id
        foreign = fill(ref, "foreign", 2, -1, "SLD", account="other")
        reconcile_broker_activity(
            ledger, [order(ref, "Filled", 2)], [foreign], account=ACCOUNT
        )
        assert lot.status is TradeStatus.OPEN
        assert pending_order(ledger, lot_id)
        x = fill(ref, "x", 2, -1.25, "SLD")
        reconcile_broker_activity(ledger, [], [x, x], account=ACCOUNT)
        assert lot.realized_pnl == 250
        assert i.terminal
    with db() as s:
        ledger = Ledger(s)
        x.commissionReport = NS(execId="x", currency="USD", commission=1.7)
        for _ in range(2):
            reconcile_broker_activity(ledger, [], [x], account=ACCOUNT)
        lot = ledger.get(lot_id)
        assert lot.realized_pnl == pytest.approx(248.30)
        assert lot.commission == pytest.approx(1.70)
        assert len(list(s.scalars(select(BrokerExecution)))) == 1


def test_partial_roll_carries_basis_and_leaves_old_remainder(db):
    with db() as s:
        ledger = Ledger(s)
        old = position(ledger, 3)
        i = prepare_order(
            ledger, old, "roll", account=ACCOUNT, replacement=make_candidate()
        )
        ref = i.order_ref
        with pytest.raises(IntegrityError):
            prepare_order(ledger, old, "close", account=ACCOUNT)
        s.rollback()
        x = fill(ref, "one", 1, -0.35, "BOT", fee=1.25)
        reconcile_broker_activity(
            ledger, [order(ref, "Submitted", 1, [x])], [], account=ACCOUNT
        )
        assert old.contracts == 2 and old.entry_credit == 500
        assert pending_order(ledger, old.id).id == i.id
        rolled = next(t for t in ledger.open_trades() if t.id != old.id)
        assert (
            rolled.contracts == 1
            and rolled.entry_credit == 285
            and rolled.entry_commission == 1.25
        )
        assert ledger.closed_trades()[0].basis_transferred
        assert ledger.closed_trades()[0].realized_pnl == 0
        reconcile_broker_activity(
            ledger, [order(ref, "Cancelled", 1, [x])], [], account=ACCOUNT
        )
        assert pending_order(ledger, old.id) is None
        assert i.terminal and i.applied == 1


def test_balanced_leg_prices_and_no_bag_double_counting(db):
    with db() as s:
        lg = Ledger(s)
        old = position(lg, 2)
        i = prepare_order(lg, old, "tp", account=ACCOUNT)
        old.tp_order_id = "42"
        ref = i.order_ref
        f1 = fill(ref, "a", 1, 0.6, "BOT", leg=old.legs[0], fee=0.4)
        f2 = fill(ref, "b", 1, 0.4, "BOT", leg=old.legs[1], fee=0.4)
        top = fill(ref, "bag", 1, -1, "SLD")
        reconcile_broker_activity(
            lg, [order(ref, "Submitted", 1, [f1])], [], account=ACCOUNT
        )
        assert old.contracts == 2 and len(lg.closed_trades()) == 0
        reconcile_broker_activity(
            lg, [order(ref, "Cancelled", 1, [f1, f2, top])], [], account=ACCOUNT
        )
        assert old.tp_order_id is None
        assert old.contracts == 1 and old.entry_credit == 250
        closed = lg.closed_trades()[0]
        assert closed.realized_pnl == pytest.approx(149.2)
        assert closed.exit_debit == 100 and i.applied == 1


def test_partial_entry_and_late_fee_after_subsequent_split(db):
    with db() as s:
        lg = Ledger(s)
        t = lg.record_planned(make_candidate(), 3, "entry", TradingMode.SANDBOX)
        i = prepare_order(lg, t, "entry", account=ACCOUNT)
        x = fill(i.order_ref, "entry", 2, -2.3, "BOT")
        reconcile_broker_activity(
            lg, [order(i.order_ref, "Cancelled", 2, [x])], [], account=ACCOUNT
        )
        assert t.contracts == 2 and t.entry_credit == 460 and i.terminal
        c = prepare_order(lg, t, "close", account=ACCOUNT)
        y = fill(c.order_ref, "close", 1, -2.1, "SLD")
        reconcile_broker_activity(
            lg, [order(c.order_ref, "Cancelled", 1, [y])], [], account=ACCOUNT
        )
        closed = lg.closed_trades()[0]
        assert closed.realized_pnl == pytest.approx(20)
        x.commissionReport = NS(execId="entry", currency="USD", commission=2)
        reconcile_broker_activity(lg, [], [x], account=ACCOUNT)
        assert t.entry_commission == 1 and closed.entry_commission == 1
        assert closed.realized_pnl == pytest.approx(19)


def test_quarantine_migration_preserves_historical_pnl_and_requires_fills(db):
    with db() as s:
        lg = Ledger(s)
        t = position(lg, 1)
        lg.close_trade(t, 350, "Roll close filled; replacement open failed: rejected")
        before = t.closed_at
        init_db(s.get_bind())
        s.refresh(t)
        assert (
            t.execution_verified is False
            and t.realized_pnl == -100
            and t.closed_at == before.replace(tzinfo=None)
        )
        ref = f"TastyAgent_CLOSE_{t.id}"
        reconcile_broker_activity(lg, [order(ref, "Filled", 1)], [], account=ACCOUNT)
        assert t.execution_verified is False
        x = fill(ref, "receipt", 1, -2.9, "SLD", fee=1)
        reconcile_broker_activity(
            lg, [], [x], account=ACCOUNT, mode=TradingMode.SANDBOX
        )
        assert t.execution_verified is True and t.realized_pnl == pytest.approx(-41)
        init_db(s.get_bind())
        s.refresh(t)
        assert t.execution_verified is True


def test_aborted_management_releases_slot(db):
    with db() as s:
        lg = Ledger(s)
        t = position(lg, 1)
        i = prepare_order(lg, t, "close", account=ACCOUNT)
        bind_order(lg, i, status="Aborted")
        assert pending_order(lg, t.id) is None and t.status is TradeStatus.OPEN


def test_reversed_new_bag_close_uses_signed_cash_and_correction_is_quarantined(db):
    with db() as s:
        lg = Ledger(s)
        t = position(lg, 1)
        i = prepare_order(lg, t, "close", account=ACCOUNT)
        i.spec = {**i.spec, "bag_action": "BUY"}
        s.commit()
        wrong = fill(i.order_ref, "inverted", 1, 2, "SLD")
        reconcile_broker_activity(lg, [], [wrong], account=ACCOUNT)
        assert t.status is TradeStatus.OPEN
        x = fill(i.order_ref, "000abc.123abc.01.01", 1, 2, "BOT", fee=1)
        reconcile_broker_activity(lg, [], [x], account=ACCOUNT)
        assert t.exit_debit == 200 and t.realized_pnl == 49
        correction = fill(i.order_ref, "000abc.123abc.01.02", 1, 2.1, "BOT", fee=1)
        reconcile_broker_activity(lg, [], [correction, x], account=ACCOUNT)
        assert (
            i.status == "NeedsReview"
            and i.applied == 1
            and t.execution_verified is False
        )
        assert len(lg.closed_trades()) == 1


def test_definitive_zero_fill_tp_cancel_clears_only_matching_id(db):
    with db() as s:
        lg = Ledger(s)
        t = position(lg, 1)
        i = prepare_order(lg, t, "tp", account=ACCOUNT)
        t.tp_order_id = "43"
        reconcile_broker_activity(
            lg, [order(i.order_ref, "Cancelled", 0)], [], account=ACCOUNT
        )
        assert i.terminal and t.tp_order_id == "43"
        j = prepare_order(lg, t, "tp", account=ACCOUNT)
        t.tp_order_id = "42"
        reconcile_broker_activity(
            lg, [order(j.order_ref, "Cancelled", 0)], [], account=ACCOUNT
        )
        assert t.tp_order_id is None and t.status is TradeStatus.OPEN
