from unittest.mock import MagicMock
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


def factory():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    init_db(eng)
    return sessionmaker(bind=eng, expire_on_commit=False, future=True)


def client(sf=None):
    return TestClient(create_app(sf or factory(), Runtime(mode=TradingMode.SANDBOX)))


def test_settings_get_shape():
    j = client().get("/api/settings").json()
    assert j["mode"] == "sandbox"
    assert j["working_capital"] == 10000.0
    assert j["use_custom_working_capital"] is True
    assert "min_iv_rank" in j["strategy"] and "universe_top_n" in j["strategy"]
    assert "max_trade_bp_pct" in j["risk"]
    assert "kill_switch" not in j["risk"]  # kill switch is its own toggle
    assert j["scheduler"]["market_hours_only"] is True


def test_settings_put_partial_update():
    c = client()
    r = c.put(
        "/api/settings",
        json={
            "use_custom_working_capital": False,
            "working_capital": 25000,
            "scheduler_interval_seconds": 120,
            "strategy": {"min_iv_rank": 0.4, "universe_top_n": 20, "bogus": 1},
            "risk": {"max_trade_bp_pct": 0.08, "kill_switch": True},
        },
    ).json()
    assert r["use_custom_working_capital"] is False
    assert r["working_capital"] == 25000
    assert r["scheduler"]["interval_seconds"] == 120
    assert r["strategy"]["min_iv_rank"] == 0.4
    assert r["strategy"]["universe_top_n"] == 20  # coerced to int
    assert "bogus" not in r["strategy"]  # unknown key ignored
    assert r["risk"]["max_trade_bp_pct"] == 0.08
    # persisted on the runtime
    assert c.get("/api/settings").json()["strategy"]["min_iv_rank"] == 0.4
    assert c.get("/api/settings").json()["use_custom_working_capital"] is False


def test_settings_put_rejects_bad_capital():
    assert client().put("/api/settings", json={"working_capital": 0}).status_code == 400


def test_activity_feed():
    sf = factory()
    lg = Ledger(sf())
    d = lg.record_decision(TradingMode.SANDBOX, "sold a strangle in XLE", 3)
    lg.record_planned(
        make_candidate(symbol="XLE"), 1, "high IVR", TradingMode.SANDBOX, d
    )
    lg.record_rejected(
        make_candidate(symbol="SPY"), "risk: too big", TradingMode.SANDBOX, d
    )

    items = client(sf).get("/api/activity").json()
    assert len(items) == 1
    a = items[0]
    assert a["commentary"] == "sold a strangle in XLE"
    assert a["considered"] == 3
    assert a["placed"] == 1
    assert a["rejected"] == 1
    assert a["symbols"] == ["XLE"]


def test_trade_events_feed_and_timeline():
    sf = factory()
    lg = Ledger(sf())
    t = lg.record_planned(
        make_candidate(symbol="XLE"), 1, "high IVR", TradingMode.SANDBOX
    )
    lg.mark_working(t, "ORD-1")
    lg.mark_open(t)

    c = client(sf)
    # The position carries its lifecycle events in order.
    pos = c.get("/api/positions").json()
    assert pos and pos[0]["symbol"] == "XLE"
    kinds = [e["kind"] for e in pos[0]["events"]]
    assert kinds == ["planned", "working", "open"]

    # The global event feed exposes them with the symbol, newest-discoverable via `after`.
    feed = c.get("/api/events").json()
    assert [e["kind"] for e in feed] == ["planned", "working", "open"]
    assert all(e["symbol"] == "XLE" for e in feed)
    after = c.get(f"/api/events?after={feed[-2]['id']}").json()
    assert [e["kind"] for e in after] == ["open"]


def test_persist_settings_to_env_and_reload(tmp_path):
    from tastyagent.settings import persist_settings_to_env

    env_file = tmp_path / ".env"
    env_file.write_text("TASTYAGENT_MODE=sandbox\nTASTYAGENT_WORKING_CAPITAL=10000.0\n")

    persist_settings_to_env(
        {
            "mode": "live_approval",
            "use_custom_working_capital": False,
            "working_capital": 25000.0,
            "scheduler_interval_seconds": 120.0,
            "scheduler_market_hours_only": False,
            "strategy": {
                "min_iv_rank": 0.45,
                "target_short_delta": 0.20,
                "universe_top_n": 25,
                "use_hard_stop": True,
            },
            "risk": {"max_trade_bp_pct": 0.12, "max_total_bp_pct": 0.50},
        },
        env_file=env_file,
    )

    content = env_file.read_text()
    assert "TASTYAGENT_MODE=live_approval" in content
    assert "TASTYAGENT_USE_CUSTOM_WORKING_CAPITAL=false" in content
    assert "TASTYAGENT_WORKING_CAPITAL=25000.0" in content
    assert "TASTYAGENT_SCHEDULER_INTERVAL_SECONDS=120.0" in content
    assert "TASTYAGENT_SCHEDULER_MARKET_HOURS_ONLY=false" in content
    assert "TASTYAGENT_MIN_IV_RANK=0.45" in content
    assert "TASTYAGENT_TARGET_SHORT_DELTA=0.2" in content
    assert "TASTYAGENT_UNIVERSE_TOP_N=25" in content
    assert "TASTYAGENT_USE_HARD_STOP=true" in content
    assert "TASTYAGENT_MAX_TRADE_BP_PCT=0.12" in content


def test_put_settings_syncs_to_configured_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TASTYAGENT_MODE=sandbox\nTASTYAGENT_WORKING_CAPITAL=10000.0\n")

    app = create_app(factory(), Runtime(mode=TradingMode.SANDBOX), env_file=env_file)
    c = TestClient(app)

    c.put(
        "/api/settings",
        json={
            "use_custom_working_capital": False,
            "working_capital": 30000.0,
            "scheduler_interval_seconds": 180.0,
            "strategy": {"min_iv_rank": 0.35, "target_short_delta": 0.18},
            "risk": {"max_trade_bp_pct": 0.09},
        },
    )

    c.post("/api/mode", json={"mode": "live_approval"})

    content = env_file.read_text()
    assert "TASTYAGENT_WORKING_CAPITAL=30000.0" in content
    assert "TASTYAGENT_USE_CUSTOM_WORKING_CAPITAL=false" in content
    assert "TASTYAGENT_SCHEDULER_INTERVAL_SECONDS=180.0" in content
    assert "TASTYAGENT_TARGET_SHORT_DELTA=0.18" in content
    assert "TASTYAGENT_MAX_TRADE_BP_PCT=0.09" in content
    assert "TASTYAGENT_MODE=live_approval" in content


def test_scheduler_toggle_syncs_auto_start_to_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TASTYAGENT_MODE=sandbox\n")

    mock_client = MagicMock()
    app = create_app(
        factory(),
        Runtime(mode=TradingMode.SANDBOX),
        client=mock_client,
        env_file=env_file,
    )
    c = TestClient(app)

    c.post("/api/scheduler/start")
    content = env_file.read_text()
    assert "TASTYAGENT_AUTO_START_SCHEDULER=true" in content

    c.post("/api/scheduler/stop")
    content = env_file.read_text()
    assert "TASTYAGENT_AUTO_START_SCHEDULER=false" in content
