"""Rebuild entry halts from the strategy ledger, using New York trading dates."""

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import RiskLimits, TradingMode
from ..db.models import Trade, TradeStatus

NEW_YORK = ZoneInfo("America/New_York")


def account_modes(mode: TradingMode) -> tuple[str, ...]:
    if mode.is_live:
        return TradingMode.LIVE_APPROVAL.value, TradingMode.LIVE_AUTO.value
    return (mode.value,)


@dataclass(frozen=True)
class SessionLosses:
    realized: float
    streak: int
    halt_reason: str


def session_losses(
    session: Session,
    mode: TradingMode,
    capital: float,
    limits: RiskLimits,
    now: datetime | None = None,
) -> SessionLosses:
    """A reached limit latches until the next New York date, including after restart.

    An unfinished streak carries across days. After a streak halt the next trading
    day starts at zero. A later win does not unlock the rest of the halted day.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now.astimezone(timezone.utc).replace(tzinfo=None)
    today = now.astimezone(NEW_YORK).date()
    stmt = (
        select(Trade.closed_at, Trade.realized_pnl)
        .where(
            Trade.mode.in_(account_modes(mode)),
            Trade.status == TradeStatus.CLOSED,
            Trade.execution_verified.is_not(False),
            Trade.basis_transferred.is_not(True),
            Trade.closed_at <= cutoff,
            Trade.realized_pnl.is_not(None),
        )
        .order_by(Trade.closed_at, Trade.id)
    )
    last_day = None
    realized = 0.0
    streak = 0
    streak_halted = False
    reason = ""
    for closed, amount in session.execute(stmt):
        day = closed.replace(tzinfo=timezone.utc).astimezone(NEW_YORK).date()
        if day != last_day:
            if streak_halted:
                streak = 0
            realized, streak_halted, reason = 0.0, False, ""
            last_day = day
        realized = round(realized + amount, 2)
        streak = streak + 1 if amount < 0 else 0
        if streak >= limits.consecutive_loss_halt:
            streak_halted = True
            reason = f"{streak} consecutive losses; entries halted for this session"
        if capital > 0 and -realized >= capital * limits.max_daily_loss_pct:
            reason = (
                f"Daily realized loss ${-realized:.2f}; entries halted for this session"
            )
    if last_day != today:
        return SessionLosses(0.0, 0 if streak_halted else streak, "")
    return SessionLosses(realized, streak, reason)


def unverified_trades(session: Session, mode: TradingMode) -> list[Trade]:
    return list(
        session.scalars(
            select(Trade).where(
                Trade.mode.in_(account_modes(mode)), Trade.execution_verified.is_(False)
            )
        )
    )


def unverified_symbols(session: Session, mode: TradingMode) -> set[str]:
    return {trade.symbol for trade in unverified_trades(session, mode)}
