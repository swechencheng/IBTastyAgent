from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from ib_async import ComboLeg, Contract, LimitOrder, Option, OrderStatus, Trade

from tastyagent.execution.roll_orders import (
    RollBlocked,
    UnsupportedRoll,
    prepare_close_order,
    prepare_roll_order,
    submit_prepared_order,
)
from tastyagent.models import Action, Leg, OptionType


def position(symbol, strikes, units=1, trade_id=6, expiry=date(2026, 12, 18)):
    legs = [
        Leg(kind, strike, expiry, action, units)
        for (kind, action), strike in zip(
            [
                (OptionType.PUT, Action.SELL_TO_OPEN),
                (OptionType.PUT, Action.BUY_TO_OPEN),
                (OptionType.CALL, Action.SELL_TO_OPEN),
                (OptionType.CALL, Action.BUY_TO_OPEN),
            ],
            strikes,
            strict=True,
        )
    ]
    return SimpleNamespace(
        id=trade_id, symbol=symbol, legs=legs, contracts=units, tp_order_id=None
    )


def fake_placer():
    options = {}

    async def qualify(symbol, leg):
        key = (symbol, leg.expiration, leg.option_type, leg.strike)
        if key not in options:
            options[key] = Option(
                symbol,
                leg.expiration.strftime("%Y%m%d"),
                leg.strike,
                leg.option_type[0].upper(),
                "SMART",
                conId=len(options) + 1,
            )
        return options[key]

    ib = Mock()
    ib.client.clientId = 55
    ib.client.getReqId.return_value = 1234
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
    ib.trades.return_value = []
    p = SimpleNamespace(
        ib=ib, client=SimpleNamespace(account="paper"), _qualify_leg=qualify
    )
    return p, options


@pytest.mark.parametrize(
    "symbol,trade_id,units,old,new,changes",
    [
        (
            "AVGO",
            6,
            1,
            [320, 310, 380, 390],
            [330, 320, 380, 390],
            {310: -1, 320: 2, 330: -1},
        ),
        (
            "NVDA",
            467,
            2,
            [130, 125, 155, 160],
            [140, 135, 155, 160],
            {125: -1, 130: 1, 135: 1, 140: -1},
        ),
    ],
)
async def test_defense_adjusts_only_changed_legs(
    symbol, trade_id, units, old, new, changes
):
    placer, opts = fake_placer()
    original = position(symbol, old, units, trade_id)
    target = position(symbol, new)
    target.net_credit = 350
    prepared = await prepare_roll_order(
        placer, original, target, 275 * units, order_ref="unique"
    )
    strikes = {c.conId: c.strike for c in opts.values()}
    assert {
        strikes[x.conId]: x.ratio * (1 if x.action == "BUY" else -1)
        for x in prepared.contract.comboLegs
    } == changes
    assert prepared.order.action == "BUY"
    assert prepared.order.totalQuantity == units
    assert prepared.order.orderId == 1234
    assert prepared.order.lmtPrice == -0.68  # ceil(0.90 * .75)
    placer.ib.placeOrder.assert_not_called()
    result = await submit_prepared_order(placer, original, prepared)
    assert result is placer.ib.placeOrder.return_value
    placer.ib.placeOrder.assert_called_once_with(prepared.contract, prepared.order)


def native_tp(placer, old, ref="TastyAgent_TP_6", status="Submitted", filled=0):
    order = LimitOrder(
        "SELL", old.contracts, -1, orderId=9, clientId=55, orderRef=ref, account="paper"
    )
    t = Trade(
        Contract(secType="BAG", symbol=old.symbol),
        order,
        OrderStatus(status=status, filled=filled),
    )
    return t


async def close_fixture():
    placer, _ = fake_placer()
    old = position("AVGO", [320, 310, 380, 390])
    old.tp_order_id = "9"
    p = await prepare_close_order(placer, old, 310, order_ref="new-close")
    return placer, old, p


async def test_cancel_acknowledged_then_recheck_then_place():
    placer, old, p = await close_fixture()
    tp = native_tp(placer, old)
    placer.ib.reqAllOpenOrdersAsync.side_effect = [[tp], []]
    placer.ib.cancelOrder.side_effect = lambda _: setattr(
        tp.orderStatus, "status", "Cancelled"
    )
    await submit_prepared_order(placer, old, p)
    placer.ib.cancelOrder.assert_called_once_with(tp.order)
    assert placer.ib.reqAllOpenOrdersAsync.await_count == 2
    assert (p.order.action, p.order.lmtPrice) == ("BUY", 3.1)


@pytest.mark.parametrize(
    "status,shares,unknown",
    [
        ("Submitted", 0, False),
        ("Filled", 1, False),
        ("Cancelled", 0.5, False),
        ("Cancelled", 0, True),
    ],
)
async def test_block_missing_partial_and_unconfirmed_tp(status, shares, unknown):
    placer, old, p = await close_fixture()
    tp = native_tp(placer, old, status=status, filled=shares)
    placer.ib.reqAllOpenOrdersAsync.return_value = [] if unknown else [tp]
    with pytest.raises(RollBlocked):
        await submit_prepared_order(placer, old, p, timeout=0.005)
    placer.ib.placeOrder.assert_not_called()


async def test_fill_while_cancel_pending_blocks_next_order():
    placer, old, p = await close_fixture()
    tp = native_tp(placer, old)
    placer.ib.reqAllOpenOrdersAsync.return_value = [tp]

    def crossed(_):
        tp.orderStatus.status = "Cancelled"
        tp.orderStatus.filled = 1

    placer.ib.cancelOrder.side_effect = crossed
    with pytest.raises(RollBlocked, match="filled"):
        await submit_prepared_order(placer, old, p)
    placer.ib.placeOrder.assert_not_called()


async def test_prior_confirmed_cancel_allows_submit_but_foreign_order_cannot_be_cancelled():
    placer, old, p = await close_fixture()
    foreign = native_tp(placer, old, ref="External")
    # BUY original short put in our close, SELL the same option in external order.
    foreign.contract.comboLegs = [
        ComboLeg(conId=p.contract.comboLegs[0].conId, ratio=1, action="BUY")
    ]
    placer.ib.reqAllOpenOrdersAsync.return_value = [foreign]
    with pytest.raises(RollBlocked, match="Conflicting"):
        await submit_prepared_order(placer, old, p, tp_cancelled_id="9")
    placer.ib.cancelOrder.assert_not_called()
    placer.ib.placeOrder.assert_not_called()
    placer.ib.reqAllOpenOrdersAsync.return_value = []
    await submit_prepared_order(placer, old, p, tp_cancelled_id="9")
    placer.ib.placeOrder.assert_called_once()


async def test_invalid_price_and_noop_and_out_with_too_many_legs_are_rejected_before_cancel():
    placer, _ = fake_placer()
    old = position("NVDA", [130, 125, 155, 160])
    target = position("NVDA", [130, 125, 155, 160])
    target.net_credit = 500
    with pytest.raises(RollBlocked, match="positive net credit"):
        await prepare_roll_order(placer, old, target, 501, order_ref="r1")
    with pytest.raises(RollBlocked, match="identical"):
        await prepare_roll_order(placer, old, target, 490, order_ref="r2")
    target.legs = position("NVDA", [130, 125, 155, 160], expiry=date(2027, 1, 15)).legs
    with pytest.raises(UnsupportedRoll, match="8 option legs"):
        await prepare_roll_order(placer, old, target, 490, order_ref="r3")
    old.legs = old.legs[:2]
    target.legs = target.legs[:2]
    prepared = await prepare_roll_order(placer, old, target, 490, order_ref="r4")
    assert len(prepared.contract.comboLegs) == 4
    placer.ib.cancelOrder.assert_not_called()


async def test_new_tp_identity_rejects_same_client_id_with_another_reference():
    placer, old, prepared = await close_fixture()
    tp = native_tp(placer, old, ref="TastyAgent_tp_abc")
    placer.ib.reqAllOpenOrdersAsync.return_value = [tp]
    with pytest.raises(RollBlocked):
        await submit_prepared_order(
            placer, old, prepared, tp_order_ref="TastyAgent_tp_def"
        )
    placer.ib.cancelOrder.assert_not_called()
    placer.ib.reqAllOpenOrdersAsync.side_effect = [[tp], []]
    placer.ib.cancelOrder.side_effect = lambda _: setattr(
        tp.orderStatus, "status", "Cancelled"
    )
    await submit_prepared_order(placer, old, prepared, tp_order_ref="TastyAgent_tp_abc")
    placer.ib.cancelOrder.assert_called_once_with(tp.order)


async def test_acknowledged_close_is_a_positive_debit_for_short_single_leg():
    placer, _ = fake_placer()
    old = position("IBIT", [35, 34, 39, 40], units=3)
    old.legs = old.legs[:1]
    prepared = await prepare_close_order(placer, old, 150, order_ref="close")
    assert prepared.contract.secType == "OPT"
    assert (
        prepared.order.action,
        prepared.order.totalQuantity,
        prepared.order.lmtPrice,
    ) == ("BUY", 3, 0.50)
