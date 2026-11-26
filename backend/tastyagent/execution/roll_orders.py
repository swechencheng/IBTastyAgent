"""Prepare defensive orders without side effects, then submit after broker interlocks."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from time import monotonic
from typing import Callable

from ib_async import Contract, Order
from ib_async import Trade as BrokerTrade

from ..db.models import Trade
from ..ibkr.orders import build_opening_credit_order, build_trade_contract
from ..ibkr.placer import IBKRPlacer
from ..models import Action, CandidateTrade
from .reconciliation import broker_account


class RollBlocked(RuntimeError):
    """No position-changing order was submitted by this invocation."""


class UnsupportedRoll(RollBlocked):
    """The requested adjustment cannot be represented by a supported native combo."""


@dataclass(frozen=True)
class PreparedOrder:
    contract: Contract
    order: Order
    credit_per_share: float


def _money(value: float) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise RollBlocked("Expected a finite nonnegative option price")
    return amount


async def _difference(placer, trade, candidate):
    """One BAG unit transforms one strategy contract. Persisted leg sizes are totals."""
    if trade.contracts <= 0 or not trade.legs:
        raise RollBlocked("The original position has no tradable quantity")
    quantities = Counter()
    options = {}
    qualified = {}
    for legs, original in (
        (trade.legs, True),
        (candidate.legs if candidate else (), False),
    ):
        for leg in legs:
            key = (str(leg.expiration), leg.option_type.lower(), leg.strike)
            if key not in qualified:
                opt = await placer._qualify_leg(trade.symbol, leg)
                if not opt or not opt.conId:
                    raise RollBlocked(f"Could not qualify {trade.symbol} {key}")
                qualified[key] = opt
            opt = qualified[key]
            size = leg.quantity
            if not isinstance(size, int) or size <= 0:
                raise RollBlocked("Invalid option leg quantity")
            if original:
                size, remainder = divmod(size, trade.contracts)
                if remainder:
                    raise RollBlocked(
                        "Original leg quantity is not a whole strategy ratio"
                    )
            if leg.action not in (Action.BUY_TO_OPEN, Action.SELL_TO_OPEN):
                raise RollBlocked(f"Unexpected position leg action {leg.action}")
            sign = 1 if leg.action == Action.BUY_TO_OPEN else -1
            quantities[opt.conId] += (-1 if original else 1) * sign * size
            options[opt.conId] = opt

    changes = [(options[key], n) for key, n in quantities.items() if n]
    if not changes:
        raise RollBlocked("The position and target are identical")
    if len(changes) > 4:
        raise UnsupportedRoll(f"SMART adjustment requires {len(changes)} option legs")
    if len(changes) == 1 and abs(changes[0][1]) != 1:
        raise UnsupportedRoll("Single-leg adjustment has a non-unit strategy ratio")

    contract = build_trade_contract(
        trade.symbol, [(opt, "BUY" if n > 0 else "SELL") for opt, n in changes]
    )
    if len(changes) > 1:
        for item, (_, n) in zip(contract.comboLegs, changes, strict=True):
            item.ratio = abs(n)
    return contract, changes[0][1]


def _priced_order(placer, trade, contract, single_change, cash, order_ref):
    # Positive cash is credit. The BAG leg actions describe the result of BUY 1 BAG.
    account = broker_account(placer.client)
    if not order_ref or not account:
        raise RollBlocked("A broker account and persisted order reference are required")
    combo = contract.secType == "BAG"
    single_action = "BUY" if single_change > 0 else "SELL"
    if not combo and (
        (single_change > 0 and cash > 0) or (single_change < 0 and cash < 0)
    ):
        raise RollBlocked("Option order direction is inconsistent with its price")
    order = build_opening_credit_order(
        trade.contracts,
        float(abs(cash)),
        account=account,
        order_ref=order_ref,
        is_combo=combo,
        single_leg_action=single_action,
    )
    if combo:
        order.lmtPrice = -float(cash)
    order.orderId = placer.ib.client.getReqId()
    return PreparedOrder(contract, order, float(cash))


async def prepare_roll_order(
    placer: IBKRPlacer,
    trade: Trade,
    candidate: CandidateTrade,
    exit_debit_total: float,
    *,
    order_ref: str,
    min_credit_per_share: float = 0.0,
) -> PreparedOrder:
    if candidate.symbol != trade.symbol or trade.contracts <= 0:
        raise RollBlocked(
            "Original and target must have the same symbol and positive size"
        )
    mid = (
        _money(candidate.net_credit) - _money(exit_debit_total) / trade.contracts
    ) / 100
    if mid <= 0:
        raise RollBlocked("The adjustment has no positive net credit")
    floor = max(mid * Decimal("0.90"), _money(min_credit_per_share))
    limit = floor.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    contract, change = await _difference(placer, trade, candidate)
    return _priced_order(placer, trade, contract, change, limit, order_ref)


async def prepare_close_order(
    placer: IBKRPlacer,
    trade: Trade,
    cost_to_close: float,
    *,
    order_ref: str,
) -> PreparedOrder:
    contract, change = await _difference(placer, trade, None)
    debit = (_money(cost_to_close) / (100 * trade.contracts)).quantize(Decimal("0.01"))
    return _priced_order(placer, trade, contract, change, -debit, order_ref)


def _directions(contract, order):
    """IBKR interprets every BAG leg relative to the parent order's direction."""
    if order.action not in ("BUY", "SELL"):
        raise RollBlocked("Broker returned an unknown order direction")
    buy = order.action == "BUY"
    if contract.secType == "BAG":
        return {
            leg.conId: (leg.action == "BUY") == buy for leg in contract.comboLegs or ()
        }
    return {contract.conId: buy}


def _check_foreign_orders(working, prepared, own_tps):
    wish = _directions(prepared.contract, prepared.order)
    for broker_trade in working:
        if broker_trade in own_tps:
            continue
        order = broker_trade.order
        if (
            order.account
            and prepared.order.account
            and order.account != prepared.order.account
        ):
            continue
        if order.orderRef == prepared.order.orderRef:
            raise RollBlocked(f"Order already submitted under {order.orderRef}")
        contract = broker_trade.contract
        if contract.secType not in ("BAG", "OPT", "FOP"):
            continue
        if contract.secType == "BAG" and not contract.comboLegs:
            if contract.symbol == prepared.contract.symbol:
                raise RollBlocked("Cannot establish legs of an outstanding combination")
            continue
        existing = _directions(contract, order)
        if any(wish[key] != existing[key] for key in wish.keys() & existing.keys()):
            raise RollBlocked(
                f"Conflicting working order {order.orderId} on an adjustment leg"
            )


def _is_own_tp(broker_trade, trade, account, client_id, tp_order_ref):
    order = broker_trade.order
    if order.account != account:
        return False
    ref = order.orderRef or ""
    legacy = f"TastyAgent_TP_{trade.id}"
    if tp_order_ref:
        if ref != tp_order_ref:
            return False
    elif (
        ref != legacy
        and not ref.startswith(legacy + "_")
        and not (trade.tp_order_id and ref.startswith("TastyAgent_tp_"))
    ):
        return False
    if getattr(order, "clientId", None) != client_id:
        return False
    return not trade.tp_order_id or str(order.orderId) == str(trade.tp_order_id)


def _filled(tp):
    return bool(tp.fills or tp.orderStatus.filled or tp.orderStatus.status == "Filled")


async def submit_prepared_order(
    placer: IBKRPlacer,
    trade: Trade,
    prepared: PreparedOrder,
    *,
    tp_cancelled_id: str | None = None,
    tp_order_ref: str | None = None,
    timeout: float = 8.0,
    on_tp_cancelled: Callable[[BrokerTrade], None] | None = None,
) -> BrokerTrade:
    """Cancel only our own TP. Cancellation, timeout and partial fills cannot imply a close.

    The caller persists the intent and reserved broker ID before invoking this method.
    A broker placement exception can mean delivery was uncertain; reconcile by reference.
    """
    ib = placer.ib
    account = prepared.order.account
    client_id = ib.client.clientId

    async def working_orders():
        try:
            return await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), timeout)
        except (TimeoutError, ConnectionError, OSError) as err:
            raise RollBlocked("Could not verify outstanding broker orders") from err

    working = await working_orders()
    cached = list(ib.trades())
    if any(t.order.orderRef == prepared.order.orderRef for t in cached):
        raise RollBlocked(f"Broker already knows order {prepared.order.orderRef}")
    own_tps = [
        t for t in working if _is_own_tp(t, trade, account, client_id, tp_order_ref)
    ]
    _check_foreign_orders(working, prepared, own_tps)
    known = own_tps or [
        t for t in cached if _is_own_tp(t, trade, account, client_id, tp_order_ref)
    ]
    if len(known) > 1:
        raise RollBlocked("Multiple take-profit orders require reconciliation")
    if known:
        tp = known[0]
        if _filled(tp):
            raise RollBlocked(
                "Take-profit has executions; reconcile remaining position"
            )
        cancelled = {"Cancelled", "ApiCancelled"}
        if tp.orderStatus.status not in cancelled:
            if not own_tps:
                raise RollBlocked("Take-profit cancellation is not confirmed")
            if tp.orderStatus.status != "PendingCancel":
                ib.cancelOrder(tp.order)
            deadline = monotonic() + timeout
            while tp.orderStatus.status not in cancelled:
                if _filled(tp) or monotonic() >= deadline:
                    raise RollBlocked(
                        "Take-profit cancellation unconfirmed or partially filled"
                    )
                await asyncio.sleep(min(0.05, timeout))
        if _filled(tp):
            raise RollBlocked("Take-profit filled during cancellation")
    elif (trade.tp_order_id or tp_order_ref) and (
        not trade.tp_order_id or str(tp_cancelled_id) != str(trade.tp_order_id)
    ):
        raise RollBlocked(
            f"No broker acknowledgement for take-profit {trade.tp_order_id}"
        )

    if own_tps:
        working = await working_orders()
        if any(_is_own_tp(t, trade, account, client_id, tp_order_ref) for t in working):
            raise RollBlocked("Broker still reports the take-profit as working")
        if _filled(own_tps[0]):
            raise RollBlocked("Take-profit filled during cancellation")
        _check_foreign_orders(working, prepared, [])
    if known and on_tp_cancelled is not None:
        on_tp_cancelled(known[0])
    return ib.placeOrder(prepared.contract, prepared.order)
