import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock
import pytest

from ib_async import Option, OrderStatus
from tastyagent.db.models import Trade, TradeLeg, TradeStatus
from tastyagent.ibkr.client import IBKRClient
from tastyagent.ibkr.placer import IBKRPlacer
from tastyagent.settings import Settings


@pytest.fixture
def mock_client():
    settings = Settings(ibkr_account="U123456")
    client = IBKRClient(settings)
    client.trading_ib = MagicMock()
    client.data_ib = MagicMock()
    client.trading_ib.managedAccounts.return_value = ["DU123456"]
    client.trading_ib.qualifyContractsAsync = AsyncMock()
    client.data_ib.qualifyContractsAsync = AsyncMock()
    client.trading_ib.whatIfOrderAsync = AsyncMock(
        return_value=MagicMock(
            initMarginChange=1000.0, maintMarginChange=800.0, commission=1.5
        )
    )
    return client


async def test_placer_immediate_fill_attaches_take_profit(mock_client):
    placer = IBKRPlacer(
        client=mock_client,
        walk_step=0.01,
        walk_interval=1,
        attach_tp=True,
        tp_pct=0.50,
    )

    trade = Trade(
        id=1,
        symbol="SPY",
        strategy="short_strangle",
        contracts=1,
        status=TradeStatus.PLANNED,
        mode="sandbox",
        entry_credit=200.0,  # $2.00/share
        legs=[
            TradeLeg(
                option_type="put",
                strike=550.0,
                expiration=date(2026, 11, 20),
                action="sell_to_open",
            ),
            TradeLeg(
                option_type="call",
                strike=600.0,
                expiration=date(2026, 11, 20),
                action="sell_to_open",
            ),
        ],
    )

    # Mock order placement
    parent_order = MagicMock(orderId=101)
    parent_trade = MagicMock(
        order=parent_order,
        orderStatus=OrderStatus(
            orderId=101, status="Filled", filled=1, avgFillPrice=-2.00
        ),
    )

    tp_order = MagicMock(orderId=102)
    tp_trade = MagicMock(order=tp_order)

    # First call places opening order, second call places TP order
    mock_client.trading_ib.placeOrder.side_effect = [parent_trade, tp_trade]

    # Run placer with 0 interval for instant test execution
    placer.walk_interval = 0
    order_id = await placer(trade)

    assert order_id == "101"
    assert trade.broker_order_id == "101"
    assert trade.tp_order_id == "102"
    assert trade.entry_credit == 200.0

    # Verify 2 orders were placed: opening order and closing TP order
    assert mock_client.trading_ib.placeOrder.call_count == 2
    # Verify closing TP order parameters
    placed_tp_order = mock_client.trading_ib.placeOrder.call_args_list[1][0][1]
    assert placed_tp_order.action == "SELL"
    assert placed_tp_order.lmtPrice == -1.00  # 50% of $2.00 fill credit is $1.00 debit
    assert placed_tp_order.tif == "GTC"
    assert placed_tp_order.orderRef == "TastyAgent_TP_1"


async def test_placer_close_uses_correct_combo_and_order(mock_client):
    placer = IBKRPlacer(client=mock_client)
    trade = Trade(
        id=2,
        symbol="AAPL",
        strategy="iron_condor",
        contracts=1,
        status=TradeStatus.OPEN,
        legs=[
            TradeLeg(
                option_type="put",
                strike=320.0,
                expiration=date(2026, 11, 20),
                action="sell_to_open",
            ),
            TradeLeg(
                option_type="put",
                strike=290.0,
                expiration=date(2026, 11, 20),
                action="buy_to_open",
            ),
            TradeLeg(
                option_type="call",
                strike=355.0,
                expiration=date(2026, 11, 20),
                action="sell_to_open",
            ),
            TradeLeg(
                option_type="call",
                strike=385.0,
                expiration=date(2026, 11, 20),
                action="buy_to_open",
            ),
        ],
    )
    mock_order = MagicMock(orderId=201)
    mock_trade = MagicMock(order=mock_order)
    mock_client.trading_ib.placeOrder.return_value = mock_trade

    order_id = await placer.close(trade, cost_to_close=850.0)
    assert order_id == "201"
    combo, order = mock_client.trading_ib.placeOrder.call_args[0]
    assert combo.comboLegs[0].action == "SELL"
    assert combo.comboLegs[1].action == "BUY"
    assert combo.comboLegs[2].action == "SELL"
    assert combo.comboLegs[3].action == "BUY"
    assert order.action == "SELL"
    assert order.lmtPrice == -8.50


async def test_placer_attaches_tp_for_defined_risk_spread_normal_credit(mock_client):
    placer = IBKRPlacer(
        client=mock_client,
        walk_step=0.01,
        walk_interval=0,
        attach_tp=True,
        tp_pct=0.50,
        min_credit_width_ratio=0.333,
    )
    # $5.00 width spread, fill credit $2.00.
    # 33% width = $1.665 > 50% credit ($1.00) -> TP profit target is $1.00, TP debit is $1.00
    trade = Trade(
        id=3,
        symbol="SPY",
        strategy="put_credit_spread",
        contracts=1,
        status=TradeStatus.PLANNED,
        mode="sandbox",
        entry_credit=200.0,
        legs=[
            TradeLeg(option_type="put", strike=550.0, expiration=date(2026, 11, 20), action="sell_to_open"),
            TradeLeg(option_type="put", strike=545.0, expiration=date(2026, 11, 20), action="buy_to_open"),
        ],
    )
    parent_order = MagicMock(orderId=301)
    parent_trade = MagicMock(order=parent_order, orderStatus=OrderStatus(orderId=301, status="Filled", avgFillPrice=-2.00))
    tp_order = MagicMock(orderId=302)
    tp_trade = MagicMock(order=tp_order)
    mock_client.trading_ib.placeOrder.side_effect = [parent_trade, tp_trade]

    await placer(trade)
    placed_tp = mock_client.trading_ib.placeOrder.call_args_list[1][0][1]
    assert placed_tp.action == "SELL"
    assert placed_tp.lmtPrice == -1.00


async def test_placer_attaches_tp_for_defined_risk_spread_deep_credit(mock_client):
    placer = IBKRPlacer(
        client=mock_client,
        walk_step=0.01,
        walk_interval=0,
        attach_tp=True,
        tp_pct=0.50,
        min_credit_width_ratio=0.333,
    )
    # $5.00 width spread, fill credit $3.80.
    # 33% width = $1.665 <= 50% credit ($1.90) -> TP profit target is $1.665, TP debit is $3.80 - $1.665 = $2.14
    trade = Trade(
        id=4,
        symbol="SPY",
        strategy="put_credit_spread",
        contracts=1,
        status=TradeStatus.PLANNED,
        mode="sandbox",
        entry_credit=380.0,
        legs=[
            TradeLeg(option_type="put", strike=550.0, expiration=date(2026, 11, 20), action="sell_to_open"),
            TradeLeg(option_type="put", strike=545.0, expiration=date(2026, 11, 20), action="buy_to_open"),
        ],
    )
    parent_order = MagicMock(orderId=401)
    parent_trade = MagicMock(order=parent_order, orderStatus=OrderStatus(orderId=401, status="Filled", avgFillPrice=-3.80))
    tp_order = MagicMock(orderId=402)
    tp_trade = MagicMock(order=tp_order)
    mock_client.trading_ib.placeOrder.side_effect = [parent_trade, tp_trade]

    await placer(trade)
    placed_tp = mock_client.trading_ib.placeOrder.call_args_list[1][0][1]
    assert placed_tp.action == "SELL"
    assert placed_tp.lmtPrice == -2.13


import asyncio as _asyncio
from types import SimpleNamespace as _SimpleNamespace
from unittest.mock import AsyncMock as _AsyncMock, MagicMock as _MagicMock
import pytest as _pytest
from tastyagent.config import TradingMode as _TradingMode
from tastyagent.db.models import (
    Trade as _Trade,
    TradeLeg as _TradeLeg,
    TradeStatus as _TradeStatus,
)
from tastyagent.ibkr.placer import IBKRPlacer as _IBKRPlacer


class _DummyErrorEvent:
    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def __isub__(self, fn):
        if fn in self.handlers:
            self.handlers.remove(fn)
        return self

    def emit(self, req_id: int, code: int, msg: str, contract=None) -> None:
        for fn in list(self.handlers):
            fn(req_id, code, msg, contract)


def _make_single_leg_trade(trade_id: int = 190) -> _Trade:
    return _Trade(
        id=trade_id,
        symbol="IGV",
        strategy="naked_put",
        contracts=1,
        status=_TradeStatus.WORKING,
        mode=_TradingMode.SANDBOX,
        entry_credit=620.0,
        legs=[
            _TradeLeg(
                option_type="put",
                strike=94.0,
                expiration="2026-12-18",
                action="sell_to_open",
                delta=-0.30,
            )
        ],
    )


@_pytest.mark.asyncio
async def test_placer_opens_and_closes_single_leg_naked_put_as_direct_option():
    placed: list[tuple] = []
    preflight_contracts: list = []
    canceled_orders: list = []

    ib = _MagicMock()
    ib.errorEvent = _DummyErrorEvent()

    async def _qualify(opt):
        opt.conId = 8675309
        return [opt]

    async def _what_if(contract, order):
        preflight_contracts.append((contract, order))
        return _SimpleNamespace(
            initMarginChange="10626.53",
            maintMarginChange="8500.0",
            commission=1.7976931348623157e308,
        )

    next_order_id = [100]

    def _place_order(contract, order):
        next_order_id[0] += 1
        order.orderId = next_order_id[0]
        placed.append((contract, order))
        if len(placed) == 1:
            return _SimpleNamespace(
                order=order,
                orderStatus=_SimpleNamespace(
                    status="Filled", filled=1, avgFillPrice=6.20
                ),
            )
        return _SimpleNamespace(
            order=order,
            orderStatus=_SimpleNamespace(
                status="Submitted", filled=0, avgFillPrice=0.0
            ),
        )

    ib.qualifyContractsAsync = _qualify
    ib.whatIfOrderAsync = _what_if
    ib.placeOrder = _place_order
    ib.cancelOrder = lambda o: canceled_orders.append(o)

    tp_order_holder: list = []

    def _open_trades():
        return [
            _SimpleNamespace(
                order=o,
                orderStatus=_SimpleNamespace(status="Submitted"),
            )
            for _, o in placed[1:2]
        ]

    ib.openTrades = _open_trades

    client = _MagicMock()
    client.account = "DUP462684"
    client.settings.mode.is_live = False
    client.active_trading_ib = ib

    placer = _IBKRPlacer(client=client, walk_interval=0)
    trade = _make_single_leg_trade(190)

    order_id = await placer(trade)
    assert order_id == "101"
    assert trade.tp_order_id == "102"

    # Preflight used direct Option, not BAG
    assert len(preflight_contracts) == 1
    assert preflight_contracts[0][0].secType == "OPT"
    assert preflight_contracts[0][1].action == "SELL"
    assert preflight_contracts[0][1].lmtPrice == 6.20

    # Opening + TP orders used direct Option with positive prices
    open_contract, open_order = placed[0]
    assert open_contract.secType == "OPT"
    assert open_order.action == "SELL"
    assert open_order.lmtPrice == 6.20

    tp_contract, tp_order = placed[1]
    assert tp_contract.secType == "OPT"
    assert tp_order.action == "BUY"
    assert tp_order.lmtPrice == 3.10
    assert tp_order.tif == "GTC"

    # Close uses direct Option with BUY @ +debit and cancels prior TP order
    close_id = await placer.close(trade, cost_to_close=400.0)
    assert close_id == "103"
    assert any(o.orderId == 102 for o in canceled_orders)
    close_contract, close_order = placed[2]
    assert close_contract.secType == "OPT"
    assert close_order.action == "BUY"
    assert close_order.lmtPrice == 4.00


@_pytest.mark.asyncio
async def test_placer_qualify_timeout_and_failure_raise_runtime_error():
    ib = _MagicMock()
    ib.qualifyContractsAsync = _AsyncMock(return_value=[None])
    client = _MagicMock()
    client.active_trading_ib = ib
    placer = _IBKRPlacer(client=client, walk_interval=0)
    leg = _make_single_leg_trade().legs[0]

    with _pytest.raises(RuntimeError, match="Failed to qualify"):
        await placer._qualify_leg("IGV", leg)

    async def _hang(_opt):
        await _asyncio.sleep(5.0)

    ib.qualifyContractsAsync = _hang
    with _pytest.raises(RuntimeError, match="Timed out qualifying"):
        await placer._qualify_leg("IGV", leg, timeout=0.02)


@_pytest.mark.asyncio
async def test_placer_error_202_scoped_and_cancellation_cancels_open_order():
    canceled_orders: list = []
    ib = _MagicMock()
    ib.errorEvent = _DummyErrorEvent()

    async def _qualify(opt):
        opt.conId = 555
        return [opt]

    async def _what_if(contract, order):
        return None

    def _place_order(contract, order):
        order.orderId = 201
        return _SimpleNamespace(
            order=order,
            orderStatus=_SimpleNamespace(
                status="Submitted", filled=0, avgFillPrice=0.0
            ),
        )

    ib.qualifyContractsAsync = _qualify
    ib.whatIfOrderAsync = _what_if
    ib.placeOrder = _place_order
    ib.cancelOrder = lambda o: canceled_orders.append(o)

    client = _MagicMock()
    client.account = "DUP462684"
    client.settings.mode.is_live = False
    client.active_trading_ib = ib

    placer = _IBKRPlacer(client=client, walk_interval=5)
    trade = _make_single_leg_trade(191)

    task = _asyncio.create_task(placer(trade))
    await _asyncio.sleep(0.05)

    # Mismatched reqId should NOT disturb current order state
    ib.errorEvent.emit(9999, 202, "current market price of 5.50", None)
    assert len(ib.errorEvent.handlers) == 1

    task.cancel()
    with _pytest.raises(_asyncio.CancelledError):
        await task

    assert any(o.orderId == 201 for o in canceled_orders)
    assert len(ib.errorEvent.handlers) == 0
