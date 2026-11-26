"""Persist order ownership before sending; derive lot changes only from broker executions."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import select, update

from ..db.models import (
    BrokerExecution,
    ExecutionOrder,
    Trade,
    TradeEvent,
    TradeLeg,
    TradeStatus,
)

CANCELLED = {"Cancelled", "Canceled", "ApiCancelled", "Inactive", "Rejected", "Expired"}


def broker_account(client) -> str:
    account = getattr(client, "account", "")
    if isinstance(account, str) and account.strip():
        return account.strip()
    ib = getattr(client, "active_trading_ib", None)
    accounts = (
        ib.managedAccounts()
        if ib and callable(getattr(ib, "managedAccounts", None))
        else []
    )
    return str(accounts[0]) if len(accounts) == 1 else ""


def _leg(leg, denominator):
    def val(v):
        return getattr(v, "value", v)

    return {
        "option_type": val(leg.option_type),
        "strike": float(leg.strike),
        "expiration": str(leg.expiration),
        "action": val(leg.action),
        "quantity": int(leg.quantity) // denominator,
        "delta": leg.delta,
    }


def _leg_key(leg):
    return f"{leg['option_type'][0].upper()}:{float(leg['strike']):.6f}:{str(leg['expiration']).replace('-', '')}"


def pending_order(ledger, trade_id):
    order = ledger.s.scalar(
        select(ExecutionOrder).where(ExecutionOrder.management_slot == trade_id)
    )
    if order is None:
        order = ledger.s.scalar(
            select(ExecutionOrder).where(
                ExecutionOrder.trade_id == trade_id,
                ExecutionOrder.kind == "entry",
                ExecutionOrder.terminal.is_(False),
                ExecutionOrder.applied > 0,
            )
        )
    return order


def outstanding_tp_ref(ledger, trade_id, account):
    return ledger.s.scalar(
        select(ExecutionOrder.order_ref)
        .where(
            ExecutionOrder.trade_id == trade_id,
            ExecutionOrder.account == account,
            ExecutionOrder.kind == "tp",
            ExecutionOrder.terminal.is_(False),
        )
        .limit(1)
    )


def prepare_order(
    ledger, trade, kind, *, account, replacement=None, reason="", order_ref=None
):
    """Claim an exit slot atomically and persist the lot before a network write."""
    if (
        kind not in {"entry", "tp", "close", "roll"}
        or not account
        or not trade.id
        or trade.contracts < 1
    ):
        raise ValueError(
            "An execution intent needs a valid kind, account and persisted, positive lot"
        )
    if (kind == "roll") != (replacement is not None):
        raise ValueError("Only an atomic roll accepts a replacement")
    # Serialize concurrent lot management and reject a prepared quote for a lot that
    # changed while its contracts were being qualified or its TP was canceled.
    expected_quantity = trade.contracts
    session = ledger.s
    session.execute(
        update(Trade).where(Trade.id == trade.id).values(updated_at=Trade.updated_at)
    )
    session.refresh(trade)
    valid_source = trade.status is TradeStatus.OPEN
    if kind == "entry":
        valid_source = trade.status in (
            TradeStatus.WORKING,
            TradeStatus.PENDING_APPROVAL,
        )
    elif kind == "close" and trade.status is TradeStatus.CLOSED:
        valid_source = (
            trade.execution_verified is False
            and order_ref == f"TastyAgent_CLOSE_{trade.id}"
        )
    if not valid_source or trade.contracts != expected_quantity:
        session.rollback()
        raise ValueError("Lot changed before submission; refresh the broker decision")
    spec = {
        "symbol": trade.symbol,
        "legs": [_leg(l, trade.contracts) for l in trade.legs],
        "strategy": trade.strategy,
        "credit": trade.entry_credit,
        "bp": trade.buying_power,
        "entry_fee": trade.entry_commission or 0.0,
        "dte": trade.dte_at_entry,
    }
    if any(l["quantity"] < 1 for l in spec["legs"]):
        raise ValueError("A broker lot requires integer leg ratios")
    if replacement is not None:
        if replacement.symbol != trade.symbol:
            raise ValueError("Atomic roll must preserve the underlying")
        spec["replacement"] = {
            "symbol": replacement.symbol,
            "strategy": replacement.strategy.value,
            "legs": [_leg(l, 1) for l in replacement.legs],
            "dte": replacement.dte,
            "bp": replacement.buying_power_reduction,
            "quote": replacement.net_credit,
        }
    intent = ExecutionOrder(
        trade_id=trade.id,
        kind=kind,
        account=account,
        reason=reason,
        requested=trade.contracts,
        spec=spec,
        order_ref=order_ref or f"TastyAgent_{kind.upper()}_{trade.id}_{uuid4().hex}",
        management_slot=trade.id if kind in {"close", "roll"} else None,
    )
    ledger.s.add(intent)
    ledger.s.commit()
    return intent


def bind_order(ledger, intent, *, order_id=None, perm_id=None, status=None):
    """Bind broker identity. Only Aborted denotes a proved pre-submit abort."""
    if order_id:
        intent.order_id = str(order_id)
    if perm_id:
        intent.perm_id = str(perm_id)
    if status and not intent.terminal:
        intent.status = status
        if status == "Aborted":
            intent.terminal = True
            intent.management_slot = None
    ledger.s.commit()


def _expected(intent):
    legs = Counter()

    def add(spec, direction):
        for leg in spec["legs"]:
            sign = -1 if leg["action"].startswith("sell") else 1
            legs[_leg_key(leg)] += direction * sign * leg["quantity"]

    add(intent.spec, 1 if intent.kind == "entry" else -1)
    if intent.kind == "roll":
        add(intent.spec["replacement"], 1)
    return {k: v for k, v in legs.items() if v}


def _execution_time(value):
    if not isinstance(value, datetime):
        return None
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def _capture(session, intent, fill):
    e, c = fill.execution, fill.contract
    if getattr(e, "acctNumber", None) != intent.account or not getattr(e, "execId", ""):
        return
    ts = _execution_time(getattr(e, "time", None) or getattr(fill, "time", None))
    if ts is None or getattr(c, "symbol", "") != intent.spec["symbol"]:
        return
    source = session.get(Trade, intent.trade_id)
    if source is None or ts < _execution_time(source.created_at).replace(microsecond=0):
        return
    if intent.perm_id and getattr(e, "permId", 0) and str(e.permId) != intent.perm_id:
        return
    side = {"BOT": 1, "BUY": 1, "SLD": -1, "SELL": -1}.get(
        getattr(e, "side", "").upper()
    )
    qty = float(getattr(e, "shares", 0) or 0)
    price = float(getattr(e, "price", float("nan")))
    if not side or not math.isfinite(price) or not qty.is_integer() or qty <= 0:
        return
    if c.secType == "BAG":
        default_action = "BUY" if intent.kind in {"entry", "roll"} else "SELL"
        expected_side = (
            1 if intent.spec.get("bag_action", default_action) == "BUY" else -1
        )
        if side != expected_side or len(intent.spec["legs"]) <= 1:
            return
        key = "BAG"
    elif c.secType == "OPT":
        key = _leg_key(
            {
                "option_type": c.right,
                "strike": c.strike,
                "expiration": c.lastTradeDateOrContractMonth,
            }
        )
        if _expected(intent).get(key, 0) * side <= 0 or price < 0:
            return
    else:
        return
    row = session.scalar(
        select(BrokerExecution).where(
            BrokerExecution.account == intent.account,
            BrokerExecution.exec_id == e.execId,
        )
    )
    if row is None:
        row = BrokerExecution(
            intent_id=intent.id,
            account=intent.account,
            exec_id=e.execId,
            leg=key,
            quantity=int(qty),
            cash=-side * qty * price * 100,
            ts=ts,
        )
        session.add(row)
    if row.intent_id != intent.id:
        return
    report = getattr(fill, "commissionReport", None)
    fee = getattr(report, "commission", None)
    if (
        getattr(report, "execId", None) == e.execId
        and getattr(report, "currency", "") == "USD"
        and fee is not None
        and math.isfinite(fee)
        and abs(fee) < 1e10
    ):
        row.fee = fee


def _economics(intent, executions):
    """Use BAG or its components once; unbalanced component fills stay pending."""
    sums, cash = Counter(), Counter()
    fees = defaultdict(list)
    # IB corrections share the first three portions of an execution ID and increment
    # the final revision number. Keep originals in the evidence table.
    latest = {}
    for row in executions:
        revision = re.fullmatch(r"([^.]+\.[^.]+\.\d+)\.(\d+)", row.exec_id)
        key, number = (revision[1], int(revision[2])) if revision else (row.exec_id, 0)
        if key not in latest or number > latest[key][0]:
            latest[key] = (number, row)
    for _, e in latest.values():
        sums[e.leg] += e.quantity
        cash[e.leg] += e.cash
        if e.fee is not None:
            fees[e.leg].append(e.fee)
    fee = sum(v for k, values in fees.items() if k != "BAG" for v in values)
    if not any(k != "BAG" for k in fees):
        fee = sum(fees.get("BAG", []))
    if sums["BAG"]:
        return sums["BAG"], cash["BAG"], fee
    expected = _expected(intent)
    if not expected:
        return 0, 0, 0
    ratios = {sums[k] / abs(v) for k, v in expected.items()}
    if len(ratios) != 1 or not next(iter(ratios)).is_integer():
        return None, 0, fee
    return int(ratios.pop()), sum(cash.values()), fee


def _new_lot(session, old, spec, units, status):
    lot = Trade(
        symbol=spec["symbol"],
        strategy=spec["strategy"],
        mode=old.mode,
        contracts=units,
        status=status,
        decision_id=old.decision_id,
        dte_at_entry=spec["dte"],
        execution_verified=True,
        rationale=f"Execution allocation from #{old.id}",
    )
    lot.legs = [
        TradeLeg(
            **{
                **leg,
                "expiration": date.fromisoformat(leg["expiration"]),
                "quantity": leg["quantity"] * units,
            }
        )
        for leg in spec["legs"]
    ]
    session.add(lot)
    session.flush()
    return lot


def _event(session, trade, kind, detail, ts):
    session.add(TradeEvent(trade_id=trade.id, kind=kind, detail=detail, ts=ts))


def _adjust_bp(trade, fallback):
    width = trade.strike_width
    return round(
        max(0.0, width * 100 * trade.contracts - trade.entry_credit)
        if width
        else fallback,
        2,
    )


def _apply_units(session, intent, units, cumulative_cash, ts):
    """Transfer completed units transactionally, leaving the unfilled old lot intact."""
    old = session.get(Trade, intent.trade_id)
    delta = units - intent.applied
    cash = round(cumulative_cash - intent.applied_cash, 8)
    if not delta:
        return
    if intent.kind == "entry":
        if intent.applied and old.status is not TradeStatus.OPEN:
            raise ValueError("Entry received another fill after management began")
        old.contracts = units
        for leg, template in zip(old.legs, intent.spec["legs"]):
            leg.quantity = units * template["quantity"]
        old.current_cost_to_close = (
            old.current_cost_to_close + cash if intent.applied else cumulative_cash
        )
        old.entry_credit = round(cumulative_cash, 6)
        old.buying_power = _adjust_bp(old, intent.spec["bp"] * units / intent.requested)
        old.status, old.execution_verified = TradeStatus.OPEN, True
        old.opened_at = old.opened_at or ts
        old.entry_date = old.opened_at.date()
        _event(
            session,
            old,
            "open",
            f"Broker executions verified {units}/{intent.requested} contracts",
            ts,
        )
        allocation = {"qty": delta, "lot": old.id, "fee": 0.0}
    else:
        quarantined = (
            old.status is TradeStatus.CLOSED and old.execution_verified is False
        )
        if old.status is not TradeStatus.OPEN and not quarantined:
            raise ValueError("Close execution exceeds the owned open lot")
        if delta > old.contracts or (quarantined and delta != old.contracts):
            raise ValueError("Confirmed close quantity does not match the owned lot")
        fraction = delta / old.contracts
        credit = old.entry_credit * fraction
        entry_fee = (old.entry_commission or 0) * fraction
        if delta < old.contracts:
            closed = _new_lot(session, old, intent.spec, delta, TradeStatus.CLOSED)
            closed.basis_parent_id = old.id
            closed.entry_credit, closed.entry_commission = credit, entry_fee
            closed.opened_at, closed.entry_date = old.opened_at, old.entry_date
            closed.buying_power = old.buying_power * fraction
            closed.current_cost_to_close = old.current_cost_to_close * fraction
            for field in (
                "entry_credit",
                "entry_commission",
                "commission",
                "buying_power",
                "current_cost_to_close",
            ):
                setattr(
                    old, field, round((getattr(old, field) or 0) * (1 - fraction), 8)
                )
            old.contracts -= delta
            for l, t in zip(old.legs, intent.spec["legs"]):
                l.quantity = old.contracts * t["quantity"]
        else:
            closed = old
        closed.status, closed.execution_verified, closed.closed_at = (
            TradeStatus.CLOSED,
            True,
            ts,
        )
        closed.commission = entry_fee
        if intent.kind == "roll":
            new = _new_lot(
                session, old, intent.spec["replacement"], delta, TradeStatus.OPEN
            )
            new.basis_parent_id = closed.id
            new.opened_at, new.entry_date = ts, ts.date()
            new.entry_credit = round(credit + cash, 6)
            new.entry_commission = new.commission = entry_fee
            new.current_cost_to_close = max(0.0, closed.current_cost_to_close + cash)
            new.buying_power = _adjust_bp(new, intent.spec["replacement"]["bp"] * delta)
            closed.basis_transferred, closed.exit_debit = True, None
            closed.exit_reason, closed.realized_pnl = (
                "Basis transferred on atomic roll",
                0.0,
            )
            closed.entry_commission = closed.commission = 0.0
            _event(
                session,
                new,
                "open",
                f"Rolled {delta} contracts from #{old.id}; net cash {cash:+.2f}",
                ts,
            )
            _event(session, closed, "rolled", f"Basis transferred to #{new.id}", ts)
            allocation = {"qty": delta, "lot": new.id, "fee": 0.0}
        else:
            closed.exit_reason = intent.reason or (
                "Take-profit order filled"
                if intent.kind == "tp"
                else "Closed via broker close order"
            )
            closed.exit_debit = round(-cash, 6)
            closed.realized_pnl = round(credit + cash - entry_fee, 6)
            _event(
                session,
                closed,
                "closed",
                f"Broker executions confirmed debit {-cash:.2f}",
                ts,
            )
            allocation = {"qty": delta, "lot": closed.id, "fee": 0.0}
    intent.allocations = [*(intent.allocations or []), allocation]
    intent.applied, intent.applied_cash = units, cumulative_cash


def _adjust_entry_fee(session, lot, amount):
    """Allocate late commissions across descendants whose original basis was divided."""
    descendants = [lot]
    for parent in descendants:
        descendants.extend(
            session.scalars(select(Trade).where(Trade.basis_parent_id == parent.id))
        )
    recipients = [trade for trade in descendants if not trade.basis_transferred]
    total = sum(trade.contracts for trade in recipients)
    for trade in recipients:
        adjustment = amount * trade.contracts / total
        trade.commission = (trade.commission or 0) + adjustment
        trade.entry_commission = (trade.entry_commission or 0) + adjustment
        if trade.status is TradeStatus.CLOSED:
            trade.realized_pnl -= adjustment


def _apply_fees(session, intent, fees):
    if not intent.applied or abs(intent.applied_fees - fees) < 1e-8:
        return
    allocations = [dict(a) for a in intent.allocations]
    for alloc in allocations:
        share = fees * alloc["qty"] / intent.applied
        adjustment = share - alloc["fee"]
        alloc["fee"] = share
        lot = session.get(Trade, alloc["lot"])
        if intent.kind in {"entry", "roll"}:
            _adjust_entry_fee(session, lot, adjustment)
        else:
            lot.commission = (lot.commission or 0) + adjustment
            if lot.status is TradeStatus.CLOSED:
                lot.realized_pnl -= adjustment
    intent.allocations, intent.applied_fees = allocations, fees


def _legacy(ledger, ref, account, fills, mode):
    """Old refs reused numeric IDs after resets; require an in-epoch owned execution."""
    match = re.fullmatch(r"TastyAgent_(?:(CLOSE|TP)_)?(\d+)", ref)
    if not match or not fills:
        return None
    old = ledger.get(int(match[2]))
    if old is None:
        return None
    # Legacy tickets did not store broker accounts. Admit their receipts only when the
    # current gateway mode agrees with their ledger mode; new intents carry an account.
    from ..config import TradingMode
    from ..risk.session import account_modes

    if mode is None:
        if account.startswith("DU") and account[2:].isdigit():
            mode = TradingMode.SANDBOX
        elif account.startswith("U") and account[1:].isdigit():
            mode = TradingMode.LIVE_AUTO
    if not mode or old.mode not in account_modes(TradingMode(mode)):
        return None
    kind = {"CLOSE": "close", "TP": "tp", None: "entry"}[match[1]]
    eligible = (
        old.status is TradeStatus.WORKING
        if kind == "entry"
        else old.status is TradeStatus.OPEN
        or (kind == "close" and old.execution_verified is False)
    )
    if not eligible:
        return None

    def matches(fill):
        e, c = fill.execution, fill.contract
        ts = _execution_time(getattr(e, "time", None) or getattr(fill, "time", None))
        return (
            getattr(e, "acctNumber", "") == account
            and getattr(e, "orderRef", "") == ref
            and getattr(c, "symbol", "") == old.symbol
            and ts is not None
            and ts
            >= _execution_time(
                old.opened_at if kind != "entry" and old.opened_at else old.created_at
            ).replace(microsecond=0)
        )

    if not any(matches(f) for f in fills):
        return None
    if kind == "close" and pending_order(ledger, old.id):
        return None
    return prepare_order(ledger, old, kind, account=account, order_ref=ref)


def reconcile_broker_activity(ledger, ib_trades, fills, *, account, mode=None):
    """Idempotent replay of complete broker snapshots or a single order callback."""
    if not account:
        return 0
    refs = defaultdict(list)
    orders = {}
    for t in ib_trades or []:
        order = getattr(t, "order", None)
        ref = getattr(order, "orderRef", "") or ""
        if not ref.startswith("TastyAgent") or getattr(order, "account", "") not in (
            "",
            account,
        ):
            continue
        orders[ref] = t
        refs[ref].extend(getattr(t, "fills", []) or [])
    for f in fills or []:
        ref = getattr(f.execution, "orderRef", "")
        if ref and ref.startswith("TastyAgent"):
            refs[ref].append(f)
    count = 0
    session = ledger.s
    for ref, observed in refs.items():
        intent = session.scalar(
            select(ExecutionOrder).where(
                ExecutionOrder.order_ref == ref, ExecutionOrder.account == account
            )
        )
        if intent is None:
            intent = _legacy(ledger, ref, account, observed, mode)
        if intent is None:
            continue
        try:
            # Acquire the database write lock before reading settlement counters.
            session.execute(
                update(ExecutionOrder)
                .where(ExecutionOrder.id == intent.id)
                .values(status=ExecutionOrder.status)
            )
            session.refresh(intent)
            broker = orders.get(ref)
            status = getattr(broker, "orderStatus", None)
            actual_status = getattr(status, "status", "")
            if broker:
                oid = getattr(broker.order, "orderId", None)
                pid = getattr(broker.order, "permId", None) or getattr(
                    status, "permId", None
                )
                if intent.perm_id and pid and intent.perm_id != str(pid):
                    session.rollback()
                    continue
                if oid:
                    intent.order_id = str(oid)
                if pid:
                    intent.perm_id = str(pid)
            for fill in observed:
                if getattr(fill.execution, "orderRef", ref) in (ref, ""):
                    _capture(session, intent, fill)
            records = list(
                session.scalars(
                    select(BrokerExecution).where(
                        BrokerExecution.intent_id == intent.id
                    )
                )
            )
            units, cash, fees = _economics(intent, records)
            revised_applied_cash = (
                units == intent.applied
                and units
                and abs(cash - intent.applied_cash) > 0.005
            )
            if (
                units is None
                or units > intent.requested
                or units < intent.applied
                or revised_applied_cash
            ):
                intent.status = "NeedsReview"
                if revised_applied_cash or (
                    units is not None
                    and (units < intent.applied or units > intent.requested)
                ):
                    # Do not repeat lot transitions on a correcting execution. Preserve both
                    # receipts and withhold a definitive P&L until the correction is reviewed.
                    for tid in {
                        intent.trade_id,
                        *(a["lot"] for a in intent.allocations),
                    }:
                        trade = session.get(Trade, tid)
                        trade.execution_verified = False
                session.commit()
                continue
            if units > intent.applied:
                _apply_units(session, intent, units, cash, max(r.ts for r in records))
                count += 1
            _apply_fees(session, intent, fees)
            # Broker status may precede execution details. Preserve the slot until reported
            # fills are all accounted for, including partial cancellation.
            reported = getattr(status, "filled", None)
            canceled = (
                actual_status in CANCELLED
                and reported is not None
                and float(reported) == units
            )
            if units == intent.requested or canceled:
                if (
                    canceled
                    and not intent.terminal
                    and intent.kind in {"roll", "close"}
                ):
                    lot = session.get(Trade, intent.trade_id)
                    messages = [
                        str(item.message)
                        for item in getattr(broker, "log", ()) or ()
                        if getattr(item, "errorCode", 0)
                        and getattr(item, "message", "")
                    ]
                    detail = messages[-1] if messages else actual_status
                    _event(
                        session,
                        lot,
                        "order_ended",
                        f"{intent.kind} {intent.order_ref} ended with {units}/{intent.requested} "
                        f"confirmed contracts ({detail})",
                        _execution_time(datetime.now(UTC)),
                    )
                intent.status = (
                    "Filled"
                    if units == intent.requested
                    else ("PartialCanceled" if units else "Cancelled")
                )
                intent.terminal = True
                intent.management_slot = None
                lot = session.get(Trade, intent.trade_id)
                if (
                    intent.kind == "tp"
                    and intent.order_id
                    and str(lot.tp_order_id) == intent.order_id
                ):
                    lot.tp_order_id = None
                if (
                    intent.kind == "entry"
                    and not units
                    and lot.status is TradeStatus.WORKING
                    and str(lot.broker_order_id) == intent.order_id
                ):
                    lot.broker_order_id = None
            elif not intent.terminal:
                intent.status = "Partial" if units else (actual_status or intent.status)
            session.commit()
        except Exception:
            session.rollback()
            raise
    return count
