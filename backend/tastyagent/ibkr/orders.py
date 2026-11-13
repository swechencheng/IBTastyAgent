"""Order construction and margin preflight for IBKR combo/BAG orders.

Constructs multi-leg BAG contracts (Strangles, Iron Condors, Credit Spreads)
and maps credit/debit limit orders with walk-the-book and take-profit support.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
import logging
import math
from typing import List, Optional, Tuple

from ib_async import ComboLeg, Contract, IB, LimitOrder, Option, Order

logger = logging.getLogger(__name__)

# IBKR C++/TWS represents unset floats as DBL_MAX (~1.7976931348623157e308).
_UNSET_DOUBLE_THRESHOLD = 1e100


def build_combo_contract(
    symbol: str,
    legs: List[
        Tuple[Option, str]
    ],  # (qualified Option contract, action: 'BUY' | 'SELL')
) -> Contract:
    """Build an IBKR BAG (combo) contract from qualified option legs."""
    combo_legs = [
        ComboLeg(
            conId=opt.conId,
            ratio=1,
            action=action.upper(),
            exchange="SMART",
        )
        for opt, action in legs
    ]
    return Contract(
        symbol=symbol,
        secType="BAG",
        exchange="SMART",
        currency="USD",
        comboLegs=combo_legs,
    )


def build_trade_contract(
    symbol: str,
    legs: List[Tuple[Option, str]],
) -> Contract:
    """Build an IBKR Contract for either a single Option leg or a multi-leg BAG combo.

    IBKR Paper/TWS rejects or silently ignores whatIfOrderAsync/orders on 1-leg BAG
    combos, so single-leg strategies (e.g. naked puts/calls) must route directly
    against the qualified Option contract.
    """
    if not legs:
        raise ValueError(f"Cannot build contract for {symbol} with zero legs")
    if len(legs) == 1:
        return legs[0][0]
    return build_combo_contract(symbol, legs)


def _single_leg_close_action(single_leg_open_action: str) -> str:
    return "BUY" if single_leg_open_action.upper() == "SELL" else "SELL"


def _make_limit_order(
    *,
    action: str,
    contracts: int,
    limit_price: float,
    tif: str,
    account: str = "",
    order_ref: str = "",
    parent_id: int = 0,
    transmit: bool = True,
) -> LimitOrder:
    order = LimitOrder(
        action=action,
        totalQuantity=contracts,
        lmtPrice=round(float(limit_price), 2),
        tif=tif,
    )
    order.overridePercentageConstraints = True
    order.transmit = transmit
    if parent_id > 0:
        order.parentId = parent_id
    if account:
        order.account = account
    if order_ref:
        order.orderRef = order_ref
    return order


def build_opening_credit_order(
    contracts: int,
    credit_per_share: float,
    account: str = "",
    order_ref: str = "",
    transmit: bool = True,
    *,
    is_combo: bool = True,
    single_leg_action: str = "SELL",
) -> LimitOrder:
    """Build an opening limit order for a credit trade.

    In IBKR BAG combo pricing:
      Action 'BUY' with a negative limit price means receiving net credit (cash received).
    For single-leg Option contracts (is_combo=False):
      Action matches the leg ('SELL' for short options) with a positive limit price.
    """
    credit = round(float(credit_per_share), 2)
    if is_combo:
        action = "BUY"
        limit_price = -credit
    else:
        action = single_leg_action.upper()
        limit_price = credit

    return _make_limit_order(
        action=action,
        contracts=contracts,
        limit_price=limit_price,
        tif="DAY",
        account=account,
        order_ref=order_ref,
        transmit=transmit,
    )


def build_closing_tp_order(
    contracts: int,
    tp_debit_per_share: float,
    account: str = "",
    order_ref: str = "",
    parent_id: int = 0,
    transmit: bool = True,
    *,
    is_combo: bool = True,
    single_leg_open_action: str = "SELL",
) -> LimitOrder:
    """Build a Take-Profit limit order to close the trade at target profit.

    Combo: opened via BUY at -credit -> close via SELL BAG at -debit.
    Single leg: opened via SELL at +credit -> close via BUY Option at +debit.
    """
    debit = round(float(tp_debit_per_share), 2)
    if is_combo:
        action = "SELL"
        limit_price = -debit
    else:
        action = _single_leg_close_action(single_leg_open_action)
        limit_price = debit

    return _make_limit_order(
        action=action,
        contracts=contracts,
        limit_price=limit_price,
        tif="GTC",  # Good-til-Cancelled so the TP remains working on IBKR servers
        account=account,
        order_ref=order_ref,
        parent_id=parent_id,
        transmit=transmit,
    )


def build_manual_close_order(
    contracts: int,
    debit_per_share: float,
    account: str = "",
    order_ref: str = "",
    *,
    is_combo: bool = True,
    single_leg_open_action: str = "SELL",
) -> LimitOrder:
    """Build a manual or defensive closing order (e.g. for rolls or stops)."""
    debit = round(float(debit_per_share), 2)
    if is_combo:
        action = "SELL"
        limit_price = -debit
    else:
        action = _single_leg_close_action(single_leg_open_action)
        limit_price = debit

    return _make_limit_order(
        action=action,
        contracts=contracts,
        limit_price=limit_price,
        tif="DAY",
        account=account,
        order_ref=order_ref,
        transmit=True,
    )


def _sanitize_ib_float(val: object) -> float:
    """Convert an IBKR OrderState numeric field to finite float, clamping UNSET_DOUBLE."""
    try:
        f = float(val or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(f) or abs(f) > _UNSET_DOUBLE_THRESHOLD:
        return 0.0
    return f


async def check_margin_preflight(
    ib: IB,
    contract: Contract,
    order: Order,
    *,
    timeout: float = 5.0,
) -> dict[str, float]:
    """Execute whatIfOrderAsync to inspect IBKR margin impact before live placement."""
    try:
        order_state = await asyncio.wait_for(
            ib.whatIfOrderAsync(contract, order),
            timeout=timeout,
        )
        if order_state is None:
            logger.warning("Margin preflight returned empty OrderState")
            return {
                "init_margin_change": 0.0,
                "maint_margin_change": 0.0,
                "commission": 0.0,
            }
        init_margin = _sanitize_ib_float(getattr(order_state, "initMarginChange", 0.0))
        maint_margin = _sanitize_ib_float(getattr(order_state, "maintMarginChange", 0.0))
        commission = _sanitize_ib_float(getattr(order_state, "commission", 0.0))
        logger.info(
            "Preflight margin: InitChange=$%.2f, MaintChange=$%.2f, EstCommission=$%.2f",
            init_margin,
            maint_margin,
            commission,
        )
        return {
            "init_margin_change": init_margin,
            "maint_margin_change": maint_margin,
            "commission": commission,
        }
    except asyncio.TimeoutError:
        logger.warning("Margin preflight timed out after %.1fs", timeout)
        return {
            "init_margin_change": 0.0,
            "maint_margin_change": 0.0,
            "commission": 0.0,
        }
    except Exception as e:
        logger.warning("Margin preflight failed: %s", e)
        return {
            "init_margin_change": 0.0,
            "maint_margin_change": 0.0,
            "commission": 0.0,
        }
