"""SQLAlchemy ORM models — the agent's own source-of-truth ledger.

We persist our own record of every decision and trade rather than relying on the
broker/sandbox (sandbox state resets every 24h). Money is stored in dollars.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TradeStatus(str, Enum):
    PLANNED = "planned"  # decided but not acted on (e.g. backtest record)
    PENDING_APPROVAL = "pending_approval"  # live-approval mode, awaiting click
    WORKING = "working"  # order submitted to broker, not yet filled
    OPEN = "open"  # filled, position is live
    CLOSED = "closed"  # position exited
    REJECTED = "rejected"  # failed post-LLM re-validation
    CANCELED = "canceled"  # approval denied or order pulled


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    mode: Mapped[str] = mapped_column(String(20))
    commentary: Mapped[str] = mapped_column(Text, default="")
    considered: Mapped[int] = mapped_column(Integer, default=0)

    trades: Mapped[list["Trade"]] = relationship(back_populates="decision")


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("decisions.id"), nullable=True
    )

    symbol: Mapped[str] = mapped_column(String(16), index=True)
    strategy: Mapped[str] = mapped_column(String(32))
    contracts: Mapped[int] = mapped_column(Integer)
    status: Mapped[TradeStatus] = mapped_column(SAEnum(TradeStatus), index=True)
    mode: Mapped[str] = mapped_column(String(20))
    rationale: Mapped[str] = mapped_column(Text, default="")

    # Entry economics (dollars).
    entry_credit: Mapped[float] = mapped_column(
        Float, default=0.0
    )  # total credit received
    buying_power: Mapped[float] = mapped_column(Float, default=0.0)
    dte_at_entry: Mapped[int] = mapped_column(Integer, default=0)
    entry_date: Mapped[date | None] = mapped_column(default=None)

    # Live mark + exit.
    current_cost_to_close: Mapped[float] = mapped_column(Float, default=0.0)
    exit_debit: Mapped[float | None] = mapped_column(Float, default=None)
    exit_reason: Mapped[str | None] = mapped_column(String(64), default=None)
    realized_pnl: Mapped[float | None] = mapped_column(Float, default=None)

    broker_order_id: Mapped[str | None] = mapped_column(String(64), default=None)
    tp_order_id: Mapped[str | None] = mapped_column(String(64), default=None)
    order_ref: Mapped[str | None] = mapped_column(String(64), default=None)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    decision: Mapped[Decision | None] = relationship(back_populates="trades")
    legs: Mapped[list["TradeLeg"]] = relationship(
        back_populates="trade", cascade="all, delete-orphan"
    )
    events: Mapped[list["TradeEvent"]] = relationship(
        back_populates="trade", cascade="all, delete-orphan", order_by="TradeEvent.ts"
    )

    @property
    def is_open(self) -> bool:
        return self.status in (TradeStatus.OPEN, TradeStatus.WORKING)

    @property
    def unrealized_pnl(self) -> float:
        """Credit kept so far if we closed at the current mark."""
        if not self.is_open:
            return 0.0
        return self.entry_credit - self.current_cost_to_close

    @property
    def is_win(self) -> bool | None:
        if self.status is not TradeStatus.CLOSED or self.realized_pnl is None:
            return None
        return self.realized_pnl > 0

    @property
    def probability_of_profit(self) -> float:
        from ..models import probability_of_profit as pop

        return pop([leg.delta for leg in self.legs if leg.action.startswith("sell")])

    @property
    def strike_width(self) -> float | None:
        from ..models import calculate_strike_width

        return calculate_strike_width(self.strategy, self.legs)


class TradeLeg(Base):
    __tablename__ = "trade_legs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"))
    option_type: Mapped[str] = mapped_column(String(8))
    strike: Mapped[float] = mapped_column(Float)
    expiration: Mapped[date] = mapped_column()
    action: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[int] = mapped_column(Integer)
    delta: Mapped[float] = mapped_column(Float, default=0.0)

    trade: Mapped[Trade] = relationship(back_populates="legs")


class TradeEvent(Base):
    """A timestamped lifecycle event for a trade (working -> open -> rolled -> closed).

    Drives the live position timeline and push/toast notifications. ``kind`` is a
    stable token; ``detail`` is a human-readable one-liner.
    """

    __tablename__ = "trade_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(
        String(24)
    )  # planned|working|open|managed|rolled|closed|rejected|canceled
    detail: Mapped[str] = mapped_column(Text, default="")

    trade: Mapped["Trade"] = relationship(back_populates="events")


class WatchlistEntry(Base):
    """A symbol the agent may analyze. The enabled set is the trading universe."""

    __tablename__ = "watchlist"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    source: Mapped[str] = mapped_column(
        String(48), default="custom"
    )  # custom | default | tt:<name>
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EquitySnapshot(Base):
    """A point on the equity curve, with the S&P close for benchmark comparison."""

    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    net_liq: Mapped[float] = mapped_column(Float)
    realized_pnl_cum: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    sp500_close: Mapped[float | None] = mapped_column(Float, default=None)
