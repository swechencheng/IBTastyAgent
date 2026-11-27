"""IBKR Placer: executes trades with walk-the-book and pre-attached 50% Take Profit orders.

Implements the Placer interface used by the agent runner and executor:
- Opens positions for net credit via walk-the-book limit repricing.
- Automatically attaches a 50% Take Profit GTC limit order upon fill.
- Employs cancel-and-replace repricing to avoid IBKR Warning 105 on combo orders.
- Features auto-healing for IBKR Error 202 (price too aggressive).
- Isolates positions using distinct orderRef tags (e.g. 'TastyAgent_{trade_id}').
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Callable

from ib_async import IB, Contract, LimitOrder, Option
from sqlalchemy import select, update
from sqlalchemy.orm import object_session

from ..db.models import ExecutionOrder, Trade, TradeLeg, TradeStatus
from ..execution.executor import EntryUnfilledError
from ..execution.reconciliation import (
    bind_order,
    broker_account,
    prepare_order,
    reconcile_broker_activity,
)
from ..models import Strategy
from ..portfolio.ledger import Ledger
from ..scheduler import is_market_open
from ..strategy.guardrails import entry_width_ratio
from ..strategy.sizing import minimum_entry_credit
from .client import IBKRClient
from .orders import (
    build_closing_tp_order,
    build_manual_close_order,
    build_opening_credit_order,
    build_trade_contract,
    check_margin_preflight,
)

logger = logging.getLogger(__name__)

# IB can report an order Filled seconds before it delivers the execution details
# that the journal needs to verify the lot. Poll for them before giving up.
EXECUTION_WAIT_SECONDS = 15.0
EXECUTION_POLL_SECONDS = 0.5

_LEG_ACTION_MAP = {
    "sell_to_open": "SELL",
    "buy_to_open": "BUY",
    "sell_to_close": "BUY",
    "buy_to_close": "SELL",
}


def _normalize_strike(strike: float) -> float:
    return round(float(strike), 2)


def _format_expiration(exp: str) -> str:
    """Format expiration as YYYYMMDD for IBKR."""
    clean = exp.replace("-", "")
    return clean


class IBKRPlacer:
    """Submits and manages multi-leg options orders against IBKR."""

    def __init__(
        self,
        client: IBKRClient,
        walk_step: float = 0.01,
        walk_interval: int = 5,
        attach_tp: bool = True,
        tp_pct: float = 0.50,
        min_credit_width_ratio: float = 0.25,
        min_cushion_pct: float = 0.30,
        index_call_min_credit_width_ratio: float = 0.20,
        entry_guard: Callable[[Trade], None] | None = None,
        additional_min_credit: float = 0.0,
        execution_wait: float = EXECUTION_WAIT_SECONDS,
    ) -> None:
        if not math.isfinite(execution_wait) or execution_wait < 0:
            raise ValueError("execution_wait must be finite, non-negative seconds")
        self.client = client
        self.walk_step = walk_step
        self.walk_interval = walk_interval
        self.attach_tp = attach_tp
        self.tp_pct = tp_pct
        self.min_credit_width_ratio = min_credit_width_ratio
        self.min_cushion_pct = min_cushion_pct
        self.index_call_min_credit_width_ratio = index_call_min_credit_width_ratio
        self.entry_guard = entry_guard
        self.additional_min_credit = additional_min_credit
        self.execution_wait = execution_wait

    @property
    def ib(self) -> IB:
        return self.client.active_trading_ib

    async def _qualify_leg(
        self, symbol: str, leg: TradeLeg, *, timeout: float = 10.0
    ) -> Option:
        """Create and qualify an Option contract for a trade leg."""
        right = "P" if leg.option_type.lower() == "put" else "C"
        exp = _format_expiration(str(leg.expiration))
        opt = Option(
            symbol=symbol,
            lastTradeDateOrContractMonth=exp,
            strike=_normalize_strike(leg.strike),
            right=right,
            exchange="SMART",
            currency="USD",
        )
        try:
            qualified = await asyncio.wait_for(
                self.ib.qualifyContractsAsync(opt),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"Timed out qualifying option contract {symbol} {exp} {leg.strike}{right}"
            ) from exc
        if isinstance(qualified, list) and qualified and qualified[0] is None:
            raise RuntimeError(
                f"Failed to qualify option contract {symbol} {exp} {leg.strike}{right}"
            )
        return opt

    async def _prepare_contract(
        self, trade: Trade, *, for_close: bool = False
    ) -> tuple[Contract, bool, str]:
        """Qualify all trade legs and return (contract, is_combo, single_leg_open_action)."""
        if not trade.legs:
            raise RuntimeError(
                f"Trade #{trade.id} ({trade.symbol}) has no option legs"
            )
        leg_pairs: list[tuple[Option, str]] = []
        for leg in trade.legs:
            opt = await self._qualify_leg(trade.symbol, leg)
            if for_close:
                # Match the opening leg directions so combo closing (SELL BAG) and
                # single-leg closing (reverse of opening action) align cleanly.
                action = "SELL" if "sell" in leg.action.lower() else "BUY"
            else:
                action = _LEG_ACTION_MAP.get(leg.action, "SELL")
            leg_pairs.append((opt, action))
            logger.info(
                "    Leg: %s %s Strike=%.2f Exp=%s -> %s",
                opt.symbol,
                opt.right,
                opt.strike,
                opt.lastTradeDateOrContractMonth,
                action,
            )
        contract = build_trade_contract(trade.symbol, leg_pairs)
        is_combo = len(leg_pairs) > 1
        single_leg_open_action = leg_pairs[0][1]
        return contract, is_combo, single_leg_open_action

    async def _await_verified_entry(self, ledger, intent, trade, broker_trade, account) -> bool:
        """Settle a reported fill from its executions, which may trail the status."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.execution_wait
        while True:
            # The background sync settles lots in its own session; never apply a
            # fill on top of a stale cached row.
            ledger.s.refresh(trade)
            reconcile_broker_activity(
                ledger, [broker_trade], list(broker_trade.fills), account=account
            )
            ledger.s.refresh(intent)
            ledger.s.refresh(trade)
            if intent.terminal and intent.applied >= 1 and trade.status == TradeStatus.OPEN:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(EXECUTION_POLL_SECONDS)

    async def __call__(self, trade: Trade) -> str:
        """Open a position for net credit with walk-the-book and attach Take Profit."""
        logger.info("\n" + "=" * 60)
        logger.info(
            "  🚀 IBTastyAgent IBKR Placer — Opening Trade #%s (%s %s)",
            trade.id,
            trade.symbol,
            trade.strategy,
        )
        logger.info("=" * 60)

        # 1-2. Qualify legs & build Contract (direct Option for 1-leg, BAG for combos)
        contract, is_combo, single_leg_open_action = await self._prepare_contract(trade)
        contract_kind = "combo" if is_combo else "option"

        # 3. Calculate initial credit & floor
        requested_qty = trade.contracts
        if not requested_qty or requested_qty < 1:
            raise ValueError("Opening quantity must be positive")
        target_credit = trade.entry_credit / (100 * trade.contracts)
        width = trade.strike_width
        if Strategy(trade.strategy).is_defined_risk and (width is None or width <= 0):
            raise ValueError("Cannot price a defined-risk entry without its strike width")
        ratio = entry_width_ratio(
            trade.symbol, trade.strategy, self.min_credit_width_ratio,
            self.index_call_min_credit_width_ratio,
        )
        if not math.isfinite(target_credit) or target_credit <= 0:
            raise ValueError("Opening midpoint must be positive and finite")
        if not math.isfinite(self.walk_step) or self.walk_step < 0.01:
            raise ValueError("Opening walk step must be at least one penny")
        min_credit = minimum_entry_credit(target_credit, width, ratio, self.additional_min_credit)
        current_credit = max(min_credit, round(target_credit, 2))
        session = object_session(trade)
        ledger = Ledger(session) if session is not None else None
        intent = None
        account = broker_account(self.client)

        logger.info(
            "  Target Credit: $%.2f | Min Floor: $%.2f | Walk Step: $%.2f | Interval: %ds",
            current_credit,
            min_credit,
            self.walk_step,
            self.walk_interval,
        )

        order_ref = (
            f"TastyAgent_{trade.id}"
            if trade.id is not None
            else f"TastyAgent_ROLL_{trade.symbol}"
        )

        def _make_open_order(credit: float) -> LimitOrder:
            return build_opening_credit_order(
                trade.contracts,
                credit,
                account=account,
                order_ref=order_ref,
                is_combo=is_combo,
                single_leg_action=single_leg_open_action,
            )

        def submit(credit: float, *, replacement: bool = False):
            nonlocal order_ref, intent
            if replacement and not is_market_open():
                raise EntryUnfilledError("Market closed during the entry walk")
            if ledger:
                # Serialize the risk read and preparation across simultaneous entry
                # requests. Preserve a submitted intent even if its ACK was lost.
                session.execute(update(Trade).where(Trade.id == trade.id).values(status=Trade.status))
                session.refresh(trade)
                active_intent = session.scalar(
                    select(ExecutionOrder.id).where(
                        ExecutionOrder.trade_id == trade.id,
                        ExecutionOrder.kind == "entry",
                        ExecutionOrder.terminal.is_(False),
                    ).limit(1)
                )
                if active_intent or trade.status not in (TradeStatus.WORKING, TradeStatus.PENDING_APPROVAL):
                    session.commit()
                    raise RuntimeError("Entry already managed or awaiting broker reconciliation")
            if self.entry_guard is not None:
                try:
                    self.entry_guard(trade)
                except Exception as exc:
                    if ledger:
                        session.rollback()
                    if replacement:
                        raise EntryUnfilledError(str(exc)) from exc
                    raise
            if ledger:
                intent = prepare_order(ledger, trade, "entry", account=account)
                order_ref = intent.order_ref
                trade.order_ref = order_ref
                ledger.s.commit()
            next_order = _make_open_order(credit)
            placed = self.ib.placeOrder(contract, next_order)
            trade.broker_order_id = str(placed.order.orderId)
            if ledger:
                bind_order(
                    ledger, intent, order_id=placed.order.orderId, perm_id=placed.order.permId,
                    status=placed.orderStatus.status,
                )
            return next_order, placed

        # 4. Optional Margin Preflight
        preflight_order = _make_open_order(min_credit)
        await check_margin_preflight(self.ib, contract, preflight_order)

        # 4b. Live Cushion Guard check
        is_live = getattr(self.client.settings.mode, "is_live", False)
        if is_live:
            cash = self.client.cached_cash_summary() or await self.client.get_account_cash_summary()
            if cash and cash.cushion is not None and cash.cushion < self.min_cushion_pct:
                logger.error(
                    "❌ [LIVE CUSHION GUARD] Blocked opening Trade #%s: IBKR account cushion %.1f%% < min %.1f%%",
                    trade.id,
                    cash.cushion * 100,
                    self.min_cushion_pct * 100,
                )
                raise RuntimeError(
                    f"Blocked by Live Cushion Guard: IBKR account cushion {cash.cushion:.1%} is below minimum {self.min_cushion_pct:.0%}"
                )

        # 5. Walk-the-book Execution Loop
        rejected_market_price: float | None = None
        auto_heal_attempts = 0
        max_auto_heals = 3
        order = _make_open_order(current_credit)

        def _on_error(reqId, errorCode, errorString, err_contract):
            nonlocal rejected_market_price
            current_order_id = getattr(order, "orderId", None)
            if current_order_id and reqId != current_order_id:
                return
            if errorCode == 202:
                # Error 202: order too aggressive
                m = re.search(r"current market price of (-?\d+\.?\d*)", errorString)
                if m:
                    rejected_market_price = abs(float(m.group(1)))
                    logger.info(
                        "  [Auto-Healing] Detected market price suggestion from IBKR: $%.2f",
                        rejected_market_price,
                    )

        self.ib.errorEvent += _on_error

        try:
            order, parent_trade = submit(current_credit)
        except BaseException:
            self.ib.errorEvent -= _on_error
            raise
        logger.info(
            "  📤 Order #%s submitted: %s %s @ $%.2f credit",
            getattr(order, "orderId", None),
            order.action,
            contract_kind,
            current_credit,
        )

        cancel_deadline = None
        try:
            while True:
                await asyncio.sleep(self.walk_interval)
                await asyncio.sleep(0.1)

                status = parent_trade.orderStatus.status
                filled_qty = parent_trade.orderStatus.filled
                logger.info(
                    "  ⏱  Order status: %s (Filled: %s/%s)",
                    status,
                    filled_qty,
                    trade.contracts,
                )

                # Check for fill (status 'Filled' or filled quantity reached)
                ended = status in ("Cancelled", "ApiCancelled", "Inactive")
                if status == "Filled" or filled_qty >= requested_qty or (ended and filled_qty > 0):
                    fill_credit = abs(parent_trade.orderStatus.avgFillPrice)
                    if ledger:
                        if not await self._await_verified_entry(
                            ledger, intent, trade, parent_trade, account
                        ):
                            raise RuntimeError("Entry awaiting verified broker executions")
                        fill_credit = trade.entry_credit / (100 * trade.contracts)
                    elif ended and filled_qty < requested_qty:
                        raise RuntimeError("Partial fill requires ledger reconciliation")
                    elif not fill_credit or not math.isfinite(fill_credit):
                        raise RuntimeError("Entry filled but average credit unavailable; awaiting executions")
                    logger.info(
                        "  ✅ Filled! Received credit: $%.2f/share ($%.2f total)",
                        fill_credit,
                        fill_credit * 100 * trade.contracts,
                    )

                    # Update trade entry credit to actual fill
                    if not ledger:
                        trade.entry_credit = round(fill_credit * 100 * trade.contracts, 2)
                        trade.current_cost_to_close = trade.entry_credit
                        if width is not None:
                            trade.buying_power = max(0.0, round(width * 100 * trade.contracts - trade.entry_credit, 2))

                    await self.attach_take_profit(trade)

                    return str(parent_trade.order.orderId)

                # Check if order was rejected / cancelled
                if ended:
                    if ledger:
                        reconcile_broker_activity(ledger, [parent_trade], list(parent_trade.fills), account=account)
                        ledger.s.refresh(intent)
                        if not intent.terminal or intent.applied:
                            raise RuntimeError("Entry cancellation awaiting verified broker quantity")
                    if cancel_deadline is not None:
                        if current_credit <= min_credit:
                            raise EntryUnfilledError(f"No fill at minimum entry credit ${min_credit:.2f}")
                        current_credit = max(min_credit, round(current_credit - self.walk_step, 2))
                        cancel_deadline = None
                        rejected_market_price = None
                        order, parent_trade = submit(current_credit, replacement=True)
                        continue
                    if (
                        rejected_market_price is not None
                        and rejected_market_price >= min_credit
                        and auto_heal_attempts < max_auto_heals
                    ):
                        auto_heal_attempts += 1
                        current_credit = round(rejected_market_price, 2)
                        rejected_market_price = None
                        logger.info(
                            "  🔄 Auto-healing (%d/%d): restarting walk-the-book from market price $%.2f",
                            auto_heal_attempts,
                            max_auto_heals,
                            current_credit,
                        )
                        order, parent_trade = submit(current_credit, replacement=True)
                        continue

                    logger.error(
                        "  ❌ Order was cancelled or rejected by IBKR (Status: %s)",
                        status,
                    )
                    raise EntryUnfilledError(f"IBKR order rejected with status {status}")

                # Reprice step down towards min_credit
                if cancel_deadline is not None:
                    if asyncio.get_running_loop().time() >= cancel_deadline:
                        raise RuntimeError("Entry cancellation unconfirmed; awaiting broker reconciliation")
                    continue
                self.ib.cancelOrder(order)
                cancel_deadline = asyncio.get_running_loop().time() + 15

        except asyncio.CancelledError:
            logger.warning(
                "Placement of Trade #%s (%s) interrupted; canceling open broker order #%s",
                trade.id,
                trade.symbol,
                getattr(order, "orderId", None),
            )
            try:
                cur_status = getattr(
                    getattr(parent_trade, "orderStatus", None), "status", ""
                )
                if cur_status not in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                    self.ib.cancelOrder(order)
            except Exception as cancel_err:
                logger.debug("Best-effort cancelOrder failed during cancellation: %s", cancel_err)
            raise
        finally:
            self.ib.errorEvent -= _on_error

    async def attach_take_profit(self, trade: Trade) -> str | None:
        """Attach one GTC exit to a confirmed lot, reserving it before submission."""
        if not self.attach_tp or not trade.contracts or trade.entry_credit <= 0:
            return None
        if trade.tp_order_id:
            return trade.tp_order_id
        session = object_session(trade)
        if session is not None and (trade.status != TradeStatus.OPEN or not trade.execution_verified):
            return None
        account = broker_account(self.client)
        if session is not None and not account:
            raise RuntimeError("Cannot attach a TP without a broker account")
        contract, is_combo, single_leg_action = await self._prepare_contract(trade)
        tp_ref = f"TastyAgent_TP_{trade.id}"
        ledger = Ledger(session) if session is not None else None
        tp_intent = None
        if ledger:
            # Serialize protection against concurrent repair and management, rechecking
            # lot size/status after the network await. Prepared intents survive a lost ACK.
            session.execute(update(Trade).where(Trade.id == trade.id).values(status=Trade.status))
            session.refresh(trade)
            existing = session.scalar(
                select(ExecutionOrder.id).where(
                    ExecutionOrder.trade_id == trade.id,
                    ExecutionOrder.terminal.is_(False),
                ).limit(1)
            )
            if existing or trade.tp_order_id or trade.status != TradeStatus.OPEN or not trade.execution_verified:
                session.commit()
                return trade.tp_order_id
            tp_intent = prepare_order(ledger, trade, "tp", account=account)
            tp_ref = tp_intent.order_ref

        credit = trade.entry_credit / (100 * trade.contracts)
        target = credit * self.tp_pct
        if trade.strike_width:
            target = min(target, trade.strike_width * self.min_credit_width_ratio)
        debit = max(0.01, round(credit - target, 2))
        order = build_closing_tp_order(
            contracts=trade.contracts, tp_debit_per_share=debit, account=account,
            order_ref=tp_ref, transmit=True, is_combo=is_combo, single_leg_open_action=single_leg_action,
        )
        placed = self.ib.placeOrder(contract, order)
        order_id = placed.order.orderId or order.orderId
        trade.tp_order_id = str(order_id)
        if ledger:
            bind_order(ledger, tp_intent, order_id=order_id, perm_id=placed.order.permId,
                       status=placed.orderStatus.status)
        logger.info("Attached TP %s for trade %s at $%.2f debit", order_id, trade.id, debit)
        return trade.tp_order_id

    async def open_candidate(self, candidate, contracts: int) -> str:
        """Open a position directly from CandidateTrade (used during rolling)."""
        logger.info(
            "Opening roll candidate %s %s (%d contracts)...",
            candidate.symbol,
            candidate.strategy.value,
            contracts,
        )
        # Create a transient trade object to reuse the placer flow
        trade = Trade(
            symbol=candidate.symbol,
            strategy=candidate.strategy.value,
            contracts=contracts,
            entry_credit=candidate.max_profit * contracts,
            legs=[
                TradeLeg(
                    option_type=leg.option_type.value,
                    strike=leg.strike,
                    expiration=str(leg.expiration),
                    action=leg.action.value,
                    delta=leg.delta,
                )
                for leg in candidate.legs
            ],
        )
        return await self(trade)

    async def close(self, trade: Trade, cost_to_close: float) -> str:
        """Close an open position for net debit (for rolls, manual exits, or defense)."""
        logger.info("\n" + "=" * 60)
        logger.info(
            "  🛡 IBTastyAgent IBKR Placer — Closing Trade #%s (%s)",
            trade.id,
            trade.symbol,
        )
        logger.info("=" * 60)

        # 1. Qualify all legs & build Contract (Option for 1-leg, BAG for combos)
        contract, is_combo, single_leg_open_action = await self._prepare_contract(
            trade, for_close=True
        )
        contract_kind = "combo" if is_combo else "option"
        debit_per_share = round(float(cost_to_close) / (100 * trade.contracts), 2)
        order_ref = f"TastyAgent_CLOSE_{trade.id}"

        # Cancel any active TP order on broker for this trade
        if trade.tp_order_id:
            try:
                for ot in self.ib.openTrades():
                    if str(ot.order.orderId) == str(trade.tp_order_id) or getattr(ot.order, "orderRef", "") == f"TastyAgent_TP_{trade.id}":
                        self.ib.cancelOrder(ot.order)
                        logger.info("  Canceled existing TP order #%s for Trade #%s", trade.tp_order_id, trade.id)
            except Exception as e:
                logger.warning("  Failed to cancel TP order #%s for Trade #%s: %s", trade.tp_order_id, trade.id, e)

        order = build_manual_close_order(
            trade.contracts,
            debit_per_share,
            account=self.client.account,
            order_ref=order_ref,
            is_combo=is_combo,
            single_leg_open_action=single_leg_open_action,
        )
        close_trade = self.ib.placeOrder(contract, order)
        logger.info(
            "  📤 Close order submitted: %s %s @ $%.2f debit",
            order.action,
            contract_kind,
            debit_per_share,
        )
        return str(close_trade.order.orderId)
