"""Cycle replay of the AVGO #6 and NVDA #467 sandbox defenses."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from ib_async import Option
from sqlalchemy import select

from tastyagent import runner
from tastyagent.api.runtime import Runtime
from tastyagent.config import TradingMode
from tastyagent.db.models import ExecutionOrder, TradeStatus
from tastyagent.db.session import in_memory_session
from tastyagent.execution.exit_manager import PositionMark
from tastyagent.execution.reconciliation import reconcile_broker_activity
from tastyagent.ibkr.placer import IBKRPlacer
from tastyagent.models import Action, Leg, OptionType, Strategy
from tastyagent.portfolio.ledger import Ledger

from .conftest import make_candidate
from .test_execution_reconciliation import fill


@pytest.mark.parametrize(
    "symbol,old_strikes,new_strikes,quantity,entry_credit,mark_cost,executed",
    [
        ("AVGO", (320, 310, 380, 390), (330, 320, 380, 390), 1, 475, 572.85, 0),
        ("NVDA", (130, 125, 155, 160), (140, 135, 155, 160), 2, 430, 676.88, 1),
    ],
)
async def test_defensive_roll_cycle_with_reject_and_partial_fill(
    monkeypatch,
    symbol,
    old_strikes,
    new_strikes,
    quantity,
    entry_credit,
    mark_cost,
    executed,
):
    session = in_memory_session()
    ledger = Ledger(session)
    expiration = date.today() + timedelta(days=38)

    def legs(strikes):
        kinds = (OptionType.PUT, OptionType.PUT, OptionType.CALL, OptionType.CALL)
        actions = (Action.SELL_TO_OPEN, Action.BUY_TO_OPEN) * 2
        return tuple(
            Leg(kind, strike, expiration, action)
            for kind, strike, action in zip(kinds, strikes, actions, strict=True)
        )

    original = make_candidate(
        symbol=symbol,
        strategy=Strategy.IRON_CONDOR,
        legs=legs(old_strikes),
        max_profit=entry_credit / quantity,
    )
    replacement = replace(
        original,
        legs=legs(new_strikes),
        net_credit=mark_cost / quantity + 40,
    )
    trade = ledger.record_planned(
        original, quantity, "historical replay", TradingMode.SANDBOX
    )
    ledger.mark_open(trade)

    ib = Mock()
    ib.client.clientId = 55
    ib.client.getReqId.return_value = 801
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    ib.fills.return_value = []
    ib.trades.return_value = []
    client = SimpleNamespace(
        account="paper-test",
        active_trading_ib=ib,
        data_ib=ib,
        get_account_cash_summary=AsyncMock(return_value=SimpleNamespace(cushion=0.85)),
    )
    qualified = {}

    async def qualify(placer, underlying, leg):
        key = (leg.option_type, leg.strike)
        if key not in qualified:
            qualified[key] = Option(
                underlying,
                expiration.strftime("%Y%m%d"),
                leg.strike,
                leg.option_type[0].upper(),
                "SMART",
                conId=len(qualified) + 1,
            )
        return qualified[key]

    def place_order(contract, order):
        intent = session.scalar(
            select(ExecutionOrder).where(ExecutionOrder.order_ref == order.orderRef)
        )
        assert intent.kind == "roll" and intent.order_id == "801"
        assert intent.status == "Submitting"
        assert trade.status is TradeStatus.OPEN
        calls = {
            value.conId for key, value in qualified.items() if key[0] is OptionType.CALL
        }
        assert calls.isdisjoint({leg.conId for leg in contract.comboLegs})
        executions = []
        if executed:
            execution = fill(
                order.orderRef, "replay-fill", executed, -0.40, "BOT", fee=1.20
            )
            execution.contract.symbol = symbol
            executions.append(execution)
        return SimpleNamespace(
            order=order,
            contract=contract,
            log=[]
            if executed
            else [
                SimpleNamespace(errorCode=201, message="Opposing working option order")
            ],
            fills=executions,
            orderStatus=SimpleNamespace(
                status="Cancelled" if executed else "Inactive", filled=executed
            ),
        )

    ib.placeOrder.side_effect = place_order
    monkeypatch.setattr(IBKRPlacer, "_qualify_leg", qualify)
    monkeypatch.setattr(runner, "scan_high_options_volume", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        runner,
        "_live_mark",
        AsyncMock(return_value=PositionMark(mark_cost, 0.55, OptionType.CALL)),
    )
    monkeypatch.setattr(
        runner, "_build_roll_candidate", AsyncMock(return_value=replacement)
    )
    monkeypatch.setattr(runner, "get_vix_level", AsyncMock(return_value=1))
    monkeypatch.setattr(
        "tastyagent.execution.tracker.sync_positions_and_marks", AsyncMock()
    )

    out = await runner.run_one_cycle(
        client=client,
        session=session,
        runtime=Runtime(ibkr_attach_tp=False),
    )
    assert out["exits"][0]["action"] == ("submitted" if executed else "error")
    ib.placeOrder.assert_called_once()
    intent = session.scalar(select(ExecutionOrder))
    assert intent.terminal and intent.applied == executed
    assert trade.status is TradeStatus.OPEN and trade.contracts == quantity - executed
    assert trade.realized_pnl is None
    open_lots = ledger.open_trades()
    assert sum(lot.contracts for lot in open_lots) == quantity
    if executed:
        rolled = next(lot for lot in open_lots if lot.id != trade.id)
        assert rolled.entry_credit == entry_credit / quantity + 40
        assert rolled.entry_commission == 1.20
        assert ledger.closed_trades()[0].basis_transferred
        assert reconcile_broker_activity(ledger, [], [], account=client.account) == 0
    else:
        assert not ledger.closed_trades()
        assert any(
            "Opposing working option order" in event.detail for event in trade.events
        )
