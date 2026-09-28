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
from datetime import datetime
from decimal import Decimal
import logging
import re
from typing import Optional, Tuple

from ib_async import Contract, IB, LimitOrder, Option, Trade as IBTrade

from ..db.models import Trade, TradeLeg
from .client import IBKRClient
from .orders import (
    build_closing_tp_order,
    build_combo_contract,
    build_manual_close_order,
    build_opening_credit_order,
    check_margin_preflight,
)

logger = logging.getLogger(__name__)

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
    ) -> None:
        self.client = client
        self.walk_step = walk_step
        self.walk_interval = walk_interval
        self.attach_tp = attach_tp
        self.tp_pct = tp_pct

    @property
    def ib(self) -> IB:
        return self.client.active_trading_ib

    async def _qualify_leg(self, symbol: str, leg: TradeLeg) -> Option:
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
        await self.ib.qualifyContractsAsync(opt)
        return opt

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

        # 1. Qualify all legs
        leg_pairs: list[tuple[Option, str]] = []
        for leg in trade.legs:
            opt = await self._qualify_leg(trade.symbol, leg)
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

        # 2. Build BAG contract
        combo = build_combo_contract(trade.symbol, leg_pairs)

        # 3. Calculate initial credit & floor
        target_credit = trade.entry_credit / (100 * trade.contracts)
        current_credit = round(float(target_credit), 2)
        min_credit = max(
            0.05, round(current_credit * 0.70, 2)
        )  # Walk floor: collect at least 70% of initial estimate

        logger.info(
            "  Target Credit: $%.2f | Min Floor: $%.2f | Walk Step: $%.2f | Interval: %ds",
            current_credit,
            min_credit,
            self.walk_step,
            self.walk_interval,
        )

        order_ref = f"TastyAgent_{trade.id}"

        # 4. Optional Margin Preflight
        preflight_order = build_opening_credit_order(
            trade.contracts,
            current_credit,
            account=self.client.account,
            order_ref=order_ref,
        )
        await check_margin_preflight(self.ib, combo, preflight_order)

        # 5. Walk-the-book Execution Loop
        rejected_market_price: Optional[float] = None

        def _on_error(reqId, errorCode, errorString, err_contract):
            nonlocal rejected_market_price
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

        order = build_opening_credit_order(
            trade.contracts,
            current_credit,
            account=self.client.account,
            order_ref=order_ref,
        )
        parent_trade = self.ib.placeOrder(combo, order)
        logger.info(
            "  📤 Order #%d submitted: BUY combo @ -$%.2f credit",
            order.orderId,
            current_credit,
        )

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
                if status == "Filled" or filled_qty >= trade.contracts:
                    fill_credit = abs(parent_trade.orderStatus.avgFillPrice)
                    if fill_credit == 0.0:
                        fill_credit = current_credit
                    logger.info(
                        "  ✅ Filled! Received credit: $%.2f/share ($%.2f total)",
                        fill_credit,
                        fill_credit * 100 * trade.contracts,
                    )

                    # Update trade entry credit to actual fill
                    trade.entry_credit = round(fill_credit * 100 * trade.contracts, 2)
                    trade.broker_order_id = str(parent_trade.order.orderId)

                    # Pre-attach Take Profit limit order at 50% max profit
                    if self.attach_tp:
                        tp_debit = round(fill_credit * self.tp_pct, 2)
                        tp_order = build_closing_tp_order(
                            contracts=trade.contracts,
                            tp_debit_per_share=tp_debit,
                            account=self.client.account,
                            order_ref=f"TastyAgent_TP_{trade.id}",
                            transmit=True,
                        )
                        tp_trade = self.ib.placeOrder(combo, tp_order)
                        assigned_tp_id = (
                            getattr(getattr(tp_trade, "order", None), "orderId", None)
                            or tp_order.orderId
                        )
                        trade.tp_order_id = str(assigned_tp_id)
                        logger.info(
                            "  🎯 Attached Take-Profit order #%s submitted: SELL combo @ -$%.2f debit (50%% profit target)",
                            trade.tp_order_id,
                            tp_debit,
                        )

                    return str(parent_trade.order.orderId)

                # Check if order was rejected / cancelled
                if status in ("Cancelled", "ApiCancelled", "Inactive"):
                    if (
                        rejected_market_price is not None
                        and rejected_market_price >= min_credit
                    ):
                        current_credit = round(rejected_market_price, 2)
                        rejected_market_price = None
                        logger.info(
                            "  🔄 Auto-healing: restarting walk-the-book from market price $%.2f",
                            current_credit,
                        )
                        order = build_opening_credit_order(
                            trade.contracts,
                            current_credit,
                            account=self.client.account,
                            order_ref=order_ref,
                        )
                        parent_trade = self.ib.placeOrder(combo, order)
                        continue

                    logger.error(
                        "  ❌ Order was cancelled or rejected by IBKR (Status: %s)",
                        status,
                    )
                    raise RuntimeError(f"IBKR order rejected with status {status}")

                # Reprice step down towards min_credit
                current_credit = round(current_credit - self.walk_step, 2)
                if current_credit < min_credit:
                    logger.warning(
                        "  ⛔ Credit $%.2f reached floor $%.2f without filling. Cancelling order.",
                        current_credit,
                        min_credit,
                    )
                    self.ib.cancelOrder(order)
                    await asyncio.sleep(1.0)
                    raise RuntimeError(
                        f"Walk-the-book timed out at min credit {min_credit}"
                    )

                # Cancel and Replace to avoid Warning 105
                self.ib.cancelOrder(order)
                await asyncio.sleep(0.5)

                order = build_opening_credit_order(
                    trade.contracts,
                    current_credit,
                    account=self.client.account,
                    order_ref=order_ref,
                )
                parent_trade = self.ib.placeOrder(combo, order)
                logger.info(
                    "  🔄 Repricing (Cancel/Replace): Limit Credit -> $%.2f",
                    current_credit,
                )

        finally:
            self.ib.errorEvent -= _on_error

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

        # 1. Qualify all legs
        leg_pairs: list[tuple[Option, str]] = []
        for leg in trade.legs:
            opt = await self._qualify_leg(trade.symbol, leg)
            # Inverse of opening action
            close_action = "BUY" if "sell" in leg.action.lower() else "SELL"
            leg_pairs.append((opt, close_action))

        combo = build_combo_contract(trade.symbol, leg_pairs)
        debit_per_share = round(float(cost_to_close) / (100 * trade.contracts), 2)
        order_ref = f"TastyAgent_CLOSE_{trade.id}"

        order = build_manual_close_order(
            trade.contracts,
            debit_per_share,
            account=self.client.account,
            order_ref=order_ref,
        )
        close_trade = self.ib.placeOrder(combo, order)
        logger.info(
            "  📤 Close order submitted: SELL combo @ -$%.2f debit", debit_per_share
        )
        return str(close_trade.order.orderId)
