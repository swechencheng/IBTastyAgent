from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tastyagent.api.app import create_app
from tastyagent.api.runtime import Runtime
from tastyagent.config import TradingMode
from tastyagent.db.session import in_memory_session, init_db
from tastyagent.decision.context import rank_universe
from tastyagent.portfolio.watchlist import WatchlistRepo
from tastyagent.ibkr.metrics import IVMetrics


def repo() -> WatchlistRepo:
    return WatchlistRepo(in_memory_session())


def test_seed_is_idempotent():
    r = repo()
    r.seed_default_if_empty(["SPY", "QQQ"])
    assert set(r.symbols()) == {"SPY", "QQQ"}
    r.seed_default_if_empty(["X"])  # already seeded -> no-op
    assert set(r.symbols()) == {"SPY", "QQQ"}


def test_add_normalizes_and_toggle_affects_universe():
    r = repo()
    r.add("aapl")
    assert "AAPL" in r.symbols()
    r.set_enabled("AAPL", False)
    assert "AAPL" not in r.symbols()  # disabled drops out of the universe
    assert any(e.symbol == "AAPL" and not e.enabled for e in r.all())


def test_import_dedups_and_remove():
    r = repo()
    assert r.import_symbols(["SPY", "QQQ", "spy"], "tt:Liquid") == 2
    assert set(r.symbols()) == {"SPY", "QQQ"}
    assert r.remove("SPY") is True
    assert r.remove("SPY") is False


def test_rank_universe_by_ivr_then_liquidity():
    m = {
        "A": IVMetrics("A", 0.60, 0.60, 0.40, 0.20, 0.80, liquidity_rating=4),
        "B": IVMetrics("B", 0.20, 0.20, 0.25, 0.15, 0.65, liquidity_rating=5),
        "C": IVMetrics(
            "C", None, None, None, None, None, liquidity_rating=3
        ),  # no IVR -> excluded
        "D": IVMetrics(
            "D", 0.60, 0.60, 0.40, 0.20, 0.80, liquidity_rating=5
        ),  # ties A on IVR, higher liquidity
    }
    assert rank_universe(m, top_n=2) == ["D", "A"]


def test_rank_universe_fallback_when_no_ivr():
    # If no symbols have valid IV rank, rank_universe should still backfill to prevent starving candidate generation
    m = {
        "SPY": IVMetrics("SPY", None, None, None, None, None),
        "QQQ": IVMetrics("QQQ", None, None, None, None, None),
    }
    assert rank_universe(m, top_n=2) == ["SPY", "QQQ"]


def _client():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    init_db(eng)
    sf = sessionmaker(bind=eng, expire_on_commit=False, future=True)
    return TestClient(create_app(sf, Runtime(mode=TradingMode.SANDBOX)))


def test_watchlist_api_crud():
    c = _client()
    assert len(c.get("/api/watchlist").json()) > 0  # seeded default

    item = c.post("/api/watchlist", json={"symbol": "nflx"}).json()
    assert item["symbol"] == "NFLX" and item["enabled"] is True

    c.post("/api/watchlist/NFLX/toggle", json={"enabled": False})
    nflx = next(w for w in c.get("/api/watchlist").json() if w["symbol"] == "NFLX")
    assert nflx["enabled"] is False

    assert (
        c.post("/api/watchlist/import", json={"symbols": ["MSFT", "NVDA"]}).json()[
            "added"
        ]
        == 2
    )
    assert c.delete("/api/watchlist/NFLX").status_code == 200
    assert c.delete("/api/watchlist/NFLX").status_code == 404


def test_ranked_and_tt_watchlists_need_sessions():
    c = _client()  # no metrics/broker session configured
    assert c.get("/api/watchlist/ranked").status_code == 503
    assert c.get("/api/tastytrade-watchlists").status_code == 503
