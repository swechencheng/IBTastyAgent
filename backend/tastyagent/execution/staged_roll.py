"""Reopen confirmed units of a staged time-extension roll, with a defended credit."""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, time
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import select, update

from ..config import TradingMode
from ..db.models import ExecutionOrder, Trade, TradeEvent, TradeStatus
from ..ibkr.marketdata import snapshot_options
from ..models import Action, CandidateTrade, Leg, Liquidity, OptionType, Strategy
from ..risk.session import NEW_YORK
from ..strategy.candidates import _liquidity_from_snaps
from ..strategy.sizing import entry_bp_per_contract
from .reconciliation import _new_lot, broker_account

logger = logging.getLogger(__name__)


def stage_target(ledger, intent, candidate):
    """Attach exact replacement legs before transmission of the staged closing order."""
    if (
        intent.kind != "close"
        or intent.order_id
        or intent.terminal
        or intent.spec["symbol"] != candidate.symbol
        or "staged_roll" in intent.spec
    ):
        raise ValueError("Staging requires a new close intent on the same underlying")
    orig_exp = max(
        date.fromisoformat(str(leg["expiration"])) for leg in intent.spec["legs"]
    )
    if not candidate.legs or min(leg.expiration for leg in candidate.legs) <= orig_exp:
        raise ValueError("A staged roll must extend expiration")
    target = asdict(candidate)
    target["legs"] = [
        {**asdict(leg), "expiration": leg.expiration.isoformat()}
        for leg in candidate.legs
    ]
    intent.spec = {
        **intent.spec,
        "staged_roll": {"target": target, "claimed": 0, "cash": 0.0, "lots": []},
    }
    ledger.s.commit()


def _candidate(target):
    legs = tuple(
        Leg(
            **{
                **leg,
                "option_type": OptionType(leg["option_type"]),
                "action": Action(leg["action"]),
                "expiration": date.fromisoformat(leg["expiration"]),
            }
        )
        for leg in target["legs"]
    )
    return CandidateTrade(
        **{
            **target,
            "strategy": Strategy(target["strategy"]),
            "liquidity": Liquidity(**target["liquidity"]),
            "legs": legs,
        }
    )


async def _requote(placer, candidate):
    contracts = [
        await placer._qualify_leg(candidate.symbol, leg) for leg in candidate.legs
    ]
    quotes = await snapshot_options(placer.client.data_ib, contracts, timeout=6.0)
    legs, snaps = [], []
    credit = Decimal(0)
    for leg, contract in zip(candidate.legs, contracts, strict=True):
        q = quotes.get(contract.conId)
        if (
            q is None
            or not q.complete
            or q.bid <= 0
            or q.ask < q.bid
            or not q.bid.is_finite()
            or not q.ask.is_finite()
            or not math.isfinite(q.delta)
        ):
            raise ValueError("Target leg has no current two-sided option quote")
        credit += q.mid * (1 if leg.is_short else -1) * leg.quantity * 100
        snaps.append(q)
        legs.append(replace(leg, delta=q.delta))
    liquidity = _liquidity_from_snaps(*snaps)
    if credit <= 0 or liquidity.bid_ask_width_pct >= 0.50:
        raise ValueError("Repriced staged roll exceeds the defensive quote limits")
    credit = float(credit)
    width = candidate.strike_width
    risk = (
        width * 100 - credit if width is not None else candidate.buying_power_reduction
    )
    if risk <= 0:
        raise ValueError("Invalid current credit or spread width")
    return replace(
        candidate,
        legs=tuple(legs),
        net_credit=credit,
        max_profit=credit,
        max_loss=risk if width is not None else candidate.max_loss,
        buying_power_reduction=risk,
        liquidity=liquidity,
    )


def _write_stage(session, intent, stage):
    intent.spec = {**intent.spec, "staged_roll": stage}


def _lock(session, intent):
    session.execute(
        update(ExecutionOrder)
        .where(ExecutionOrder.id == intent.id)
        .values(status=ExecutionOrder.status)
    )
    session.refresh(intent)
    return intent.spec["staged_roll"].copy()


def _next_tranche(session, intent, stage):
    units = intent.applied - stage["claimed"]
    if units <= 0 or stage.get("finished") or intent.status == "NeedsReview":
        return None
    if stage["lots"]:
        last = stage["lots"][-1]
        entries = list(
            session.scalars(
                select(ExecutionOrder).where(
                    ExecutionOrder.trade_id == last, ExecutionOrder.kind == "entry"
                )
            )
        )
        if (
            not entries
            or not all(x.terminal for x in entries)
            or sum(x.applied for x in entries) != stage["last_size"]
        ):
            return None
    cash = Decimal(str(intent.applied_cash)) - Decimal(str(stage["cash"]))
    if cash >= 0:
        return None
    close_floor = -cash / (100 * units) + Decimal(".01")
    original_quote_floor = (
        Decimal(str(stage["target"]["net_credit"])) * Decimal(".90") / 100
    )
    floor = max(close_floor, original_quote_floor).quantize(
        Decimal(".01"), rounding=ROUND_CEILING
    )
    return units, float(floor)


def _session_open(created, now):
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    local = now.astimezone(NEW_YORK)
    return (
        created.astimezone(NEW_YORK).date() == local.date()
        and local.weekday() < 5
        and time(9, 30) <= local.time() < time(16)
    )


def _claim(ledger, intent, expected, fresh, params, now):
    session = ledger.s
    stage = _lock(session, intent)
    if _next_tranche(session, intent, stage) != expected or not _session_open(
        intent.created_at, now
    ):
        session.rollback()
        return None
    units, _ = expected
    old = session.get(Trade, intent.trade_id)
    target = stage["target"]
    spec = {
        "symbol": fresh.symbol,
        "strategy": fresh.strategy.value,
        "dte": fresh.dte,
        "legs": [
            {**leg, "delta": item.delta}
            for leg, item in zip(target["legs"], fresh.legs, strict=True)
        ],
    }
    lot = _new_lot(session, old, spec, units, TradeStatus.WORKING)
    lot.execution_verified = None
    lot.entry_credit = lot.current_cost_to_close = round(fresh.net_credit * units, 6)
    lot.buying_power = entry_bp_per_contract(fresh, params) * units
    lot.rationale = f"Staged roll from #{old.id}, close intent {intent.order_ref}"
    stage.update(
        claimed=intent.applied,
        cash=intent.applied_cash,
        last_size=units,
        lots=[*stage["lots"], lot.id],
    )
    _write_stage(session, intent, stage)
    session.add(TradeEvent(trade_id=lot.id, kind="planned", detail=lot.rationale))
    # The child and consumed close quantity must survive a crash at the network boundary together.
    session.commit()
    return lot


async def resume_staged_rolls(
    ledger, placer_factory, *, account, mode, params, now=None, requote=None
):
    """Attempt each newly confirmed close tranche once in its original NY session.

    placer_factory(minimum_credit_per_share) supplies the normal entry placer with its
    account-aware entry guard. A network exception after submission leaves its intent
    available for reconciliation. No previous ambiguous or rejected entry is retried.
    """
    if mode not in (TradingMode.SANDBOX, TradingMode.LIVE_AUTO) or not account:
        return []
    session = ledger.s
    ids = list(
        session.scalars(
            select(ExecutionOrder.id)
            .join(Trade)
            .where(
                ExecutionOrder.kind == "close",
                ExecutionOrder.account == account,
                Trade.mode == mode.value,
                ExecutionOrder.applied > 0,
                ExecutionOrder.spec["staged_roll"].as_string().is_not(None),
            )
        )
    )
    result = []
    for id_ in ids:
        intent = session.get(ExecutionOrder, id_)
        stage = intent.spec["staged_roll"]
        current = now or datetime.now(UTC)
        expected = _next_tranche(session, intent, stage)
        if not expected:
            continue
        if not _session_open(intent.created_at, current):
            locked = _lock(session, intent)
            locked["finished"] = (
                "Expired staged target outside original regular session"
            )
            _write_stage(session, intent, locked)
            session.add(
                TradeEvent(
                    trade_id=intent.trade_id,
                    kind="roll_expired",
                    detail=locked["finished"],
                )
            )
            session.commit()
            continue
        _, floor = expected
        placer = placer_factory(floor)
        if (
            placer.entry_guard is None
            or broker_account(placer.client) != account
            or placer.additional_min_credit < floor
        ):
            raise ValueError(
                "Staged entry requires an account-bound guarded placer and minimum net credit"
            )
        try:
            target = _candidate(stage["target"])
            fresh = await (requote or _requote)(placer, target)
            if fresh.net_credit < floor * 100 - 1e-8:
                continue
            if (
                fresh.symbol != target.symbol
                or fresh.strategy != target.strategy
                or any(
                    replace(a, delta=b.delta) != b
                    for a, b in zip(fresh.legs, target.legs, strict=True)
                )
            ):
                raise ValueError("Staged roll target legs changed while repricing")
        except Exception as exc:  # noqa: BLE001 - isolate quote and execution failures by lot
            logger.warning(
                "Deferred staged roll %s awaiting fresh quotes: %s",
                intent.order_ref,
                exc,
            )
            continue
        lot = _claim(ledger, intent, expected, fresh, params, now or datetime.now(UTC))
        if lot is None:
            continue
        try:
            await placer(lot)
        except Exception as exc:  # noqa: BLE001 - isolate quote and execution failures by lot
            # The ordinary placer persists its entry intent before calling placeOrder.
            # Absence of a tagged intent proves that a preflight/guard stopped submission.
            entries = list(
                session.scalars(
                    select(ExecutionOrder).where(
                        ExecutionOrder.trade_id == lot.id,
                        ExecutionOrder.kind == "entry",
                    )
                )
            )
            if (
                not entries or all(e.terminal and not e.applied for e in entries)
            ) and lot.status is TradeStatus.WORKING:
                lot.status = TradeStatus.CANCELED
                lot.exit_reason = str(exc)
            session.add(
                TradeEvent(trade_id=lot.id, kind="entry_error", detail=str(exc))
            )
            session.commit()
            logger.warning("Staged replacement #%s: %s", lot.id, exc)
        result.append(lot.id)
    return result
