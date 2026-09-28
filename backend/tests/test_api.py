from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tastyagent.api.app import create_app
from tastyagent.api.runtime import Runtime
from tastyagent.config import TradingMode
from tastyagent.db.session import init_db
from tastyagent.portfolio.ledger import Ledger

from .conftest import make_candidate


def shared_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    init_db(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


class FakePlacer:
    async def __call__(self, trade):
        return f"ORD{trade.id}"


def seed_open_and_closed(sf):
    lg = Ledger(sf())
    d = lg.record_decision(TradingMode.SANDBOX, "seeded", 2)
    c = make_candidate(max_profit=250.0)
    t_open = lg.record_planned(c, 1, "open it", TradingMode.SANDBOX, d)
    lg.mark_open(t_open)
    lg.update_mark(t_open, 150.0)  # unrealized +100
    t_win = lg.record_planned(c, 1, "winner", TradingMode.SANDBOX, d)
    lg.mark_open(t_win)
    lg.close_trade(t_win, 100.0, "50% target")  # realized +150


def sandbox_client():
    sf = shared_factory()
    seed_open_and_closed(sf)
    return TestClient(
        create_app(sf, Runtime(mode=TradingMode.SANDBOX, starting_capital=1_000_000))
    )


def test_status():
    r = sandbox_client().get("/api/status")
    assert r.status_code == 200
    assert r.json()["mode"] == "sandbox"


def test_trades_positions_closed():
    c = sandbox_client()
    assert len(c.get("/api/trades").json()) == 2
    assert len(c.get("/api/positions").json()) == 1
    assert len(c.get("/api/trades/closed").json()) == 1


def test_pnl():
    j = sandbox_client().get("/api/pnl").json()
    assert j["unrealized_pnl"] == 100.0
    assert j["realized_pnl"] == 150.0
    assert j["wins"] == 1
    assert j["starting_capital"] == 1_000_000


def test_benchmark_empty_ok():
    j = sandbox_client().get("/api/benchmark").json()
    assert j["strategy_return_pct"] == 0.0  # no equity snapshots seeded


def test_benchmark_with_snapshots():
    from datetime import datetime, timedelta, timezone

    from tastyagent.db.models import EquitySnapshot

    sf = shared_factory()
    s = sf()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    s.add(EquitySnapshot(ts=base, net_liq=1_000_000.0, sp500_close=500.0))
    s.add(
        EquitySnapshot(
            ts=base + timedelta(days=1), net_liq=1_050_000.0, sp500_close=510.0
        )
    )
    s.commit()
    client = TestClient(create_app(sf, Runtime(mode=TradingMode.SANDBOX)))

    j = client.get("/api/benchmark").json()
    assert round(j["strategy_return_pct"], 4) == 0.05  # 1.00M -> 1.05M
    assert round(j["sp500_return_pct"], 4) == 0.02  # 500 -> 510
    assert len(j["strategy_curve"]) == 2 and len(j["sp500_curve"]) == 2
    assert j["sp500_curve"][0]["value"] == 1_000_000.0  # S&P rebased to start capital


def test_mode_and_kill_switch():
    c = sandbox_client()
    assert (
        c.post("/api/kill-switch", json={"engaged": True}).json()["kill_switch"] is True
    )
    j = c.post("/api/mode", json={"mode": "live_approval"}).json()
    assert j["mode"] == "live_approval" and j["requires_approval"] is True
    assert c.post("/api/mode", json={"mode": "bogus"}).status_code == 400


def test_approval_flow():
    sf = shared_factory()
    lg = Ledger(sf())
    d = lg.record_decision(TradingMode.LIVE_APPROVAL, "", 1)
    lg.record_planned(make_candidate(), 1, "pending", TradingMode.LIVE_APPROVAL, d)
    client = TestClient(
        create_app(sf, Runtime(mode=TradingMode.LIVE_APPROVAL, placer=FakePlacer()))
    )

    pending = client.get("/api/approvals").json()
    assert len(pending) == 1
    tid = pending[0]["id"]
    res = client.post(f"/api/approvals/{tid}/approve").json()
    assert res["action"] == "placed"
    assert client.get("/api/approvals").json() == []  # no longer pending


def test_connection_endpoint():
    c = sandbox_client()
    r = c.get("/api/connection")
    assert r.status_code == 200
    assert r.json()["status"] == "disconnected"


def test_status_with_client_connection():
    from unittest.mock import MagicMock
    from tastyagent.ibkr.client import ConnectionStatus

    sf = shared_factory()
    mock_client = MagicMock()
    mock_client.connection_status = ConnectionStatus(
        real_connected=True,
        paper_connected=True,
        status="connected",
        detail="All gateways online (Paper + Live Data)",
    )
    app = create_app(
        sf,
        Runtime(mode=TradingMode.SANDBOX, starting_capital=1_000_000),
        client=mock_client,
    )
    client = TestClient(app)
    r = client.get("/api/status")
    assert r.status_code == 200
    conn = r.json()["connection"]
    assert conn is not None
    assert conn["status"] == "connected"
    assert conn["real_connected"] is True
    assert conn["paper_connected"] is True

    r_conn = client.get("/api/connection")
    assert r_conn.status_code == 200
    assert r_conn.json()["status"] == "connected"
