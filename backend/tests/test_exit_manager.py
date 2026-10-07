from datetime import date, timedelta

from tastyagent.config import StrategyParams, TradingMode
from tastyagent.db.models import TradeStatus
from tastyagent.db.session import in_memory_session
from tastyagent.execution.exit_manager import PositionMark, RollResult, manage_exits
from tastyagent.models import Action, Leg, OptionType
from tastyagent.portfolio.ledger import Ledger
from tastyagent.strategy.exits import RollKind

from .conftest import make_candidate

PARAMS = StrategyParams()


def mark(cost, delta=0.16, side=OptionType.PUT):
    async def _fn(_trade):
        return PositionMark(cost_to_close=cost, max_short_delta=delta, tested_side=side)

    return _fn


class FakeClose:
    def __init__(self):
        self.calls = []

    async def __call__(self, trade, debit):
        self.calls.append((trade.id, debit))
        return f"C{trade.id}"


class FakeRoll:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    async def __call__(self, trade, roll_kind, m):
        self.calls.append((trade.id, roll_kind))
        if not self.result:
            return None
        return RollResult(
            new_candidate=make_candidate(symbol=trade.symbol),
            contracts=trade.contracts,
            exit_debit=m.cost_to_close,
            new_order_id=f"R{trade.id}",
        )


def make_open_trade(lg, *, exp_days, entry_days_ago, max_profit=250.0, contracts=1):
    today = date.today()
    exp = today + timedelta(days=exp_days)
    legs = (
        Leg(
            option_type=OptionType.PUT,
            strike=90,
            expiration=exp,
            action=Action.SELL_TO_OPEN,
            delta=-0.16,
        ),
        Leg(
            option_type=OptionType.CALL,
            strike=110,
            expiration=exp,
            action=Action.SELL_TO_OPEN,
            delta=0.15,
        ),
    )
    c = make_candidate(legs=legs, max_profit=max_profit)
    d = lg.record_decision(TradingMode.SANDBOX, "", 0)
    t = lg.record_planned(c, contracts, "r", TradingMode.SANDBOX, d)
    lg.mark_open(t)
    t.entry_date = today - timedelta(days=entry_days_ago)
    lg.s.commit()
    return t


async def test_hold_when_no_trigger():
    lg = Ledger(in_memory_session())
    t = make_open_trade(lg, exp_days=40, entry_days_ago=3)
    closer, roller = FakeClose(), FakeRoll()
    out = await manage_exits(
        lg, PARAMS, mark_fn=mark(230.0, delta=0.16), close_fn=closer, roll_fn=roller
    )
    assert out[0].action == "hold"
    assert closer.calls == [] and roller.calls == []
    assert t.status is TradeStatus.OPEN


async def test_take_profit_closes():
    lg = Ledger(in_memory_session())
    t = make_open_trade(lg, exp_days=40, entry_days_ago=30)
    closer = FakeClose()
    out = await manage_exits(
        lg, PARAMS, mark_fn=mark(125.0), close_fn=closer, roll_fn=FakeRoll()
    )
    assert out[0].action == "closed"
    assert t.status is TradeStatus.CLOSED and t.realized_pnl == 125.0
    assert closer.calls == [(t.id, 125.0)]


async def test_roll_out_at_21_dte_records_new_trade():
    lg = Ledger(in_memory_session())
    t = make_open_trade(
        lg, exp_days=15, entry_days_ago=30, max_profit=250.0
    )  # 15 DTE, ~20% profit
    closer, roller = FakeClose(), FakeRoll()
    out = await manage_exits(
        lg, PARAMS, mark_fn=mark(200.0), close_fn=closer, roll_fn=roller
    )
    assert out[0].action == "rolled"
    assert roller.calls == [(t.id, RollKind.OUT)]
    assert t.status is TradeStatus.CLOSED  # old cycle closed
    assert t.realized_pnl == 50.0  # 250 - 200
    # a new WORKING trade was opened from the roll
    working = [x for x in lg.all_trades() if x.status is TradeStatus.WORKING]
    assert len(working) == 1 and working[0].broker_order_id == f"R{t.id}"


async def test_roll_untested_when_tested():
    lg = Ledger(in_memory_session())
    t = make_open_trade(lg, exp_days=40, entry_days_ago=12)  # not at DTE
    roller = FakeRoll()
    out = await manage_exits(
        lg,
        PARAMS,
        mark_fn=mark(300.0, delta=0.50),
        close_fn=FakeClose(),
        roll_fn=roller,
    )
    assert out[0].action == "rolled"
    assert roller.calls == [(t.id, RollKind.UNTESTED)]


async def test_roll_falls_back_to_close_without_credit():
    lg = Ledger(in_memory_session())
    t = make_open_trade(lg, exp_days=15, entry_days_ago=30)
    closer = FakeClose()
    out = await manage_exits(
        lg, PARAMS, mark_fn=mark(200.0), close_fn=closer, roll_fn=FakeRoll(result=None)
    )
    assert out[0].action == "closed"
    assert "no credit roll" in out[0].reason
    assert closer.calls == [(t.id, 200.0)]
    assert t.status is TradeStatus.CLOSED


async def test_roll_untested_without_credit_holds():
    lg = Ledger(in_memory_session())
    t = make_open_trade(lg, exp_days=40, entry_days_ago=12)  # > 21 DTE, not DTE trigger
    closer = FakeClose()
    out = await manage_exits(
        lg,
        PARAMS,
        mark_fn=mark(300.0, delta=0.50),
        close_fn=closer,
        roll_fn=FakeRoll(result=None),
    )
    assert out[0].action == "hold"
    assert "no credit roll; holding" in out[0].reason
    assert closer.calls == []
    assert t.status is TradeStatus.OPEN


async def test_only_manages_filled_positions():
    lg = Ledger(in_memory_session())
    d = lg.record_decision(TradingMode.SANDBOX, "", 0)
    lg.record_planned(make_candidate(), 1, "r", TradingMode.SANDBOX, d)  # WORKING
    out = await manage_exits(
        lg, PARAMS, mark_fn=mark(0.0), close_fn=FakeClose(), roll_fn=FakeRoll()
    )
    assert out == []


async def test_mark_failure_skipped():
    lg = Ledger(in_memory_session())
    make_open_trade(lg, exp_days=40, entry_days_ago=3)

    async def boom(_):
        raise RuntimeError("no quote")

    out = await manage_exits(
        lg, PARAMS, mark_fn=boom, close_fn=FakeClose(), roll_fn=FakeRoll()
    )
    assert out[0].action == "error"
