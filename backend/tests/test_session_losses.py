from datetime import datetime, timezone

from tastyagent.api.runtime import Runtime
from tastyagent.config import RiskLimits, TradingMode
from tastyagent.db.models import TradeStatus
from tastyagent.db.session import in_memory_session
from tastyagent.portfolio.ledger import Ledger
from tastyagent.portfolio.pnl import summarize
from tastyagent.risk.entry import check_order_entry
from tastyagent.risk.session import session_losses

from .conftest import make_candidate
import pytest

MODE = TradingMode.SANDBOX
LIMITS = RiskLimits(max_daily_loss_pct=0.03, consecutive_loss_halt=5)


def record(lg, symbol, ts, pnl, *, mode=MODE, verified=True, transfer=False):
    trade = lg.record_planned(make_candidate(symbol=symbol), 1, "replay", mode)
    lg.mark_open(trade)
    lg.close_trade(
        trade,
        exit_debit=trade.entry_credit - pnl,
        exit_reason="execution",
        closed_at=datetime.fromisoformat(ts),
    )
    trade.execution_verified = verified
    trade.basis_transferred = transfer
    lg.s.commit()
    return trade


def read(lg, ts):
    return session_losses(
        lg.s,
        MODE,
        15_000,
        LIMITS,
        datetime.fromisoformat(ts).replace(tzinfo=timezone.utc),
    )


def test_november_roll_losses_replay_and_next_session():
    lg = Ledger(in_memory_session())
    for day, symbol, pnl in [
        ("19T14:52:57", "AVGO", -97.85),
        ("20T14:49:08", "TLT", -18.87),
        ("20T14:49:30", "TSM", -161.97),
        ("20T14:49:44", "IBIT", -13.10),
        ("20T14:50:06", "DRAM", -58.77),
        ("20T14:50:30", "NVDA", -111.09),
        ("20T14:50:43", "NVDA", -46.88),
        ("20T18:31:51", "TSLA", -50.71),
    ]:
        record(lg, symbol, "2026-11-" + day, pnl)
    assert not read(lg, "2026-11-20T14:50:05").halt_reason
    assert read(lg, "2026-11-20T14:50:07").streak == 5
    assert read(lg, "2026-11-20T15:01:26").halt_reason
    late = read(lg, "2026-11-20T18:38:04")
    assert late.realized == -461.39 and "Daily" in late.halt_reason
    record(lg, "X", "2026-11-20T20:00:00", 500)
    assert read(lg, "2026-11-21T01:00:00").halt_reason  # still Nov 20 in New York
    assert not read(lg, "2026-11-23T14:30:00").halt_reason


def test_unverified_and_transfers_do_not_trip_limits_or_enter_confirmed_pnl():
    lg = Ledger(in_memory_session())
    old = record(lg, "AVGO", "2026-11-20T14:49:08", -500, verified=False)
    record(lg, "TSM", "2026-11-20T14:49:30", 0, transfer=True)
    record(lg, "X", "2026-11-20T14:49:44", -900, mode=TradingMode.LIVE_AUTO)
    assert read(lg, "2026-11-20T20:00:00").realized == 0
    total = summarize([old])
    assert total.unverified_count == 1 and total.closed_count == total.realized_pnl == 0
    pending = lg.record_planned(make_candidate(symbol="AVGO"), 1, "new", MODE)
    with pytest.raises(ValueError, match="unverified"):
        check_order_entry(lg, pending, Runtime(mode=MODE), 15000)
    assert pending.status == TradeStatus.WORKING
