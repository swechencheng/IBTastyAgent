"""Engine + session factory. Defaults to a SQLite file; supports in-memory for tests."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

DEFAULT_DB_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "tastyagent.db"
)


def make_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    if url is None:
        DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{DEFAULT_DB_PATH}"
    return create_engine(url, echo=echo, future=True)


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    # Additive SQLite migrations preserve the original evidence on legacy rows.
    columns = {col["name"] for col in inspect(engine).get_columns("trades")}
    additions = {
        "tp_order_id": "TEXT",
        "order_ref": "TEXT",
        "execution_verified": "BOOLEAN",
        "entry_commission": "FLOAT NOT NULL DEFAULT 0",
        "commission": "FLOAT NOT NULL DEFAULT 0",
        "basis_parent_id": "INTEGER",
        "basis_transferred": "BOOLEAN NOT NULL DEFAULT 0",
    }
    with engine.begin() as conn:
        for name, sql_type in additions.items():
            if name not in columns:
                conn.execute(text(f"ALTER TABLE trades ADD COLUMN {name} {sql_type}"))
        conn.execute(
            text(
                "UPDATE trades SET execution_verified = false WHERE execution_verified IS NULL "
                "AND status='CLOSED' AND exit_reason LIKE 'Roll close filled; replacement open failed:%'"
            )
        )


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def in_memory_session() -> Session:
    """A ready-to-use session backed by a fresh in-memory DB (tests/dev)."""
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return session_factory(engine)()
