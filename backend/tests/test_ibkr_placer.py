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
