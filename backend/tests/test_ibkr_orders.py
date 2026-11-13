from types import SimpleNamespace
import pytest

from ib_async import Option
from tastyagent.ibkr.orders import (
    build_closing_tp_order,
    build_combo_contract,
    build_manual_close_order,
    build_opening_credit_order,
)


def test_build_combo_contract():
    opt1 = Option("TSLA", "20261120", 350.0, "P", "SMART", conId=101)
    opt2 = Option("TSLA", "20261120", 400.0, "C", "SMART", conId=102)

    combo = build_combo_contract("TSLA", [(opt1, "SELL"), (opt2, "SELL")])
    assert combo.secType == "BAG"
    assert combo.symbol == "TSLA"
    assert len(combo.comboLegs) == 2
    assert combo.comboLegs[0].conId == 101
    assert combo.comboLegs[0].action == "SELL"
    assert combo.comboLegs[1].conId == 102
    assert combo.comboLegs[1].action == "SELL"


def test_build_opening_credit_order():
    order = build_opening_credit_order(
        contracts=2,
        credit_per_share=1.50,
        account="U123456",
        order_ref="TastyAgent_1",
    )
    assert order.action == "BUY"  # Buying credit combo
    assert order.lmtPrice == -1.50  # Negative price = net credit in IBKR
    assert order.totalQuantity == 2
    assert order.account == "U123456"
    assert order.orderRef == "TastyAgent_1"
    assert order.overridePercentageConstraints is True


def test_build_closing_tp_order():
    order = build_closing_tp_order(
        contracts=2,
        tp_debit_per_share=0.75,
        account="U123456",
        order_ref="TastyAgent_TP_1",
        parent_id=5001,
    )
    assert order.action == "SELL"  # Selling to close credit combo
    assert order.lmtPrice == -0.75  # Negative price = net debit paid to close
    assert order.totalQuantity == 2
    assert order.tif == "GTC"
    assert order.parentId == 5001
    assert order.orderRef == "TastyAgent_TP_1"
    assert order.overridePercentageConstraints is True


def test_build_manual_close_order():
    order = build_manual_close_order(
        contracts=1,
        debit_per_share=2.10,
        account="U123456",
        order_ref="TastyAgent_CLOSE_1",
    )
    assert order.action == "SELL"
    assert order.lmtPrice == -2.10
    assert order.totalQuantity == 1
    assert order.orderRef == "TastyAgent_CLOSE_1"


import asyncio as _asyncio
from types import SimpleNamespace as _SimpleNamespace
import pytest as _pytest
from ib_async import Option as _Option
from tastyagent.ibkr.orders import (
    build_closing_tp_order as _build_closing_tp_order,
    build_manual_close_order as _build_manual_close_order,
    build_opening_credit_order as _build_opening_credit_order,
    build_trade_contract as _build_trade_contract,
    check_margin_preflight as _check_margin_preflight,
)


def test_build_trade_contract_single_leg_returns_option_directly():
    opt1 = _Option("IGV", "20261218", 94.0, "P", "SMART", currency="USD", conId=111)
    opt2 = _Option("IGV", "20261218", 89.0, "P", "SMART", currency="USD", conId=222)

    single = _build_trade_contract("IGV", [(opt1, "SELL")])
    assert single is opt1
    assert single.secType == "OPT"

    combo = _build_trade_contract("IGV", [(opt1, "SELL"), (opt2, "BUY")])
    assert combo.secType == "BAG"
    assert len(combo.comboLegs) == 2

    with _pytest.raises(ValueError, match="zero legs"):
        _build_trade_contract("IGV", [])


def test_single_leg_opening_tp_and_close_order_semantics():
    open_short = _build_opening_credit_order(
        2, 6.20, is_combo=False, single_leg_action="SELL"
    )
    assert open_short.action == "SELL"
    assert open_short.lmtPrice == 6.20

    tp_short = _build_closing_tp_order(
        2, 3.10, is_combo=False, single_leg_open_action="SELL"
    )
    assert tp_short.action == "BUY"
    assert tp_short.lmtPrice == 3.10
    assert tp_short.tif == "GTC"

    close_short = _build_manual_close_order(
        2, 4.55, is_combo=False, single_leg_open_action="SELL"
    )
    assert close_short.action == "BUY"
    assert close_short.lmtPrice == 4.55
    assert close_short.tif == "DAY"

    open_long = _build_opening_credit_order(
        1, 2.50, is_combo=False, single_leg_action="BUY"
    )
    assert open_long.action == "BUY"
    assert open_long.lmtPrice == 2.50

    close_long = _build_manual_close_order(
        1, 3.75, is_combo=False, single_leg_open_action="BUY"
    )
    assert close_long.action == "SELL"
    assert close_long.lmtPrice == 3.75


@_pytest.mark.asyncio
async def test_check_margin_preflight_timeout_and_unset_double_sanitization():
    class _SlowIB:
        async def whatIfOrderAsync(self, contract, order):
            await _asyncio.sleep(5.0)
            return None

    res_timeout = await _check_margin_preflight(
        _SlowIB(),
        _Option("IGV", "20261218", 94.0, "P", "SMART"),
        _build_opening_credit_order(1, 6.20, is_combo=False),
        timeout=0.05,
    )
    assert res_timeout == {
        "init_margin_change": 0.0,
        "maint_margin_change": 0.0,
        "commission": 0.0,
    }

    class _UnsetCommissionIB:
        async def whatIfOrderAsync(self, contract, order):
            return _SimpleNamespace(
                initMarginChange="10626.53",
                maintMarginChange="8500.0",
                commission=1.7976931348623157e308,
            )

    res_unset = await _check_margin_preflight(
        _UnsetCommissionIB(),
        _Option("IGV", "20261218", 94.0, "P", "SMART"),
        _build_opening_credit_order(1, 6.20, is_combo=False),
        timeout=1.0,
    )
    assert res_unset == {
        "init_margin_change": 10626.53,
        "maint_margin_change": 8500.0,
        "commission": 0.0,
    }
