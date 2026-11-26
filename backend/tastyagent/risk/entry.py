"""Order-time entry checks against this strategy's current account ledger."""

from collections import Counter

from sqlalchemy import select

from ..db.models import Trade, TradeStatus
from ..models import Strategy
from ..strategy.sizing import effective_buying_power
from .limits import PortfolioState, check_new_entry
from .session import account_modes, session_losses, unverified_symbols


def account_exposure(ledger, mode, params, *, exclude_id=None):
    stmt = select(Trade).where(
        Trade.mode.in_(account_modes(mode)),
        Trade.status.in_((TradeStatus.OPEN, TradeStatus.WORKING)),
    )
    if exclude_id is not None:
        stmt = stmt.where(Trade.id != exclude_id)
    positions = list(ledger.s.scalars(stmt))
    spread = undefined = 0.0
    for trade in positions:
        bp = effective_buying_power(trade, params)
        if Strategy(trade.strategy).is_defined_risk:
            spread += bp
        else:
            undefined += bp
    counts = Counter(t.symbol for t in positions)
    losing = {t.symbol for t in positions if t.unrealized_pnl < 0}
    return counts, losing, spread, undefined


def check_order_entry(ledger, trade, runtime, capital, cushion=None):
    """Recheck after candidate selection, including after waiting for approval."""
    mode = runtime.mode
    if trade.mode not in account_modes(mode):
        raise ValueError("Pending trade belongs to a different trading environment")
    if capital <= 0:
        raise ValueError("No working capital available")
    limits = runtime.risk_limits()
    losses = session_losses(ledger.s, mode, capital, limits)
    if losses.halt_reason:
        raise ValueError(losses.halt_reason)
    if trade.symbol in unverified_symbols(ledger.s, mode):
        raise ValueError(f"{trade.symbol} has an unverified historical execution")
    counts, losing, spread, undefined = account_exposure(
        ledger,
        mode,
        runtime.strategy,
        exclude_id=trade.id,
    )
    reserved = effective_buying_power(trade, runtime.strategy)
    if reserved > capital * limits.max_trade_bp_pct:
        raise ValueError("Entry exceeds single-trade buying-power limit")
    state = PortfolioState(
        net_liq=capital,
        bp_used=spread + undefined,
        open_positions=sum(counts.values()),
        positions_for_symbol=counts[trade.symbol],
        realized_pnl_today=losses.realized,
        consecutive_losses=losses.streak,
        cushion=cushion,
        is_live=mode.is_live,
        losing_symbols=frozenset(losing),
        spread_bp_used=spread,
        undefined_bp_used=undefined,
    )
    verdict = check_new_entry(
        trade.symbol,
        reserved,
        state,
        limits,
        is_defined_risk=Strategy(trade.strategy).is_defined_risk,
    )
    if not verdict.ok:
        raise ValueError("; ".join(verdict.violations))
