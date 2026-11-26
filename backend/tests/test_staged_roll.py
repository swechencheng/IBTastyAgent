from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tastyagent.config import StrategyParams, TradingMode
from tastyagent.db.models import ExecutionOrder, TradeStatus
from tastyagent.db.session import init_db, make_engine, session_factory
from tastyagent.execution.reconciliation import prepare_order
from tastyagent.execution.staged_roll import resume_staged_rolls, stage_target
from tastyagent.models import Action, OptionType, Strategy
from tastyagent.portfolio.ledger import Ledger

from .conftest import make_candidate, make_leg

NOW = datetime(2026, 11, 24, 17, 2, tzinfo=UTC)
MODE = TradingMode.SANDBOX


def condor(expiry):
    legs = tuple(
        replace(make_leg(typ, k, side), expiration=expiry)
        for typ, k, side in [
            (OptionType.PUT, 100, Action.SELL_TO_OPEN),
            (OptionType.PUT, 95, Action.BUY_TO_OPEN),
            (OptionType.CALL, 120, Action.SELL_TO_OPEN),
            (OptionType.CALL, 125, Action.BUY_TO_OPEN),
        ]
    )
    return make_candidate(
        strategy=Strategy.IRON_CONDOR,
        legs=legs,
        net_credit=200,
        max_profit=200,
        max_loss=300,
        buying_power_reduction=300,
    )


@pytest.fixture
def setup(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'paper.db'}")
    init_db(engine)
    sf = session_factory(engine)
    ledger = Ledger(sf())
    old = ledger.record_planned(condor(date(2026, 12, 4)), 3, "old", MODE)
    ledger.mark_open(old)
    intent = prepare_order(ledger, old, "close", account="paper")
    intent.created_at = NOW - timedelta(minutes=3)
    target = replace(condor(date(2027, 1, 15)), net_credit=125.0, max_profit=125.0)
    stage_target(ledger, intent, target)
    yield sf, ledger, intent
    ledger.s.close()
    engine.dispose()


class Placer:
    def __init__(self, ledger, floor, recorder, outcome):
        self.ledger = ledger
        self.additional_min_credit = floor
        self.recorder = recorder
        self.client = SimpleNamespace(account="paper")
        self.entry_guard = lambda trade: None
        self.outcome = outcome

    async def __call__(self, lot):
        self.recorder.append((lot.id, lot.contracts, self.additional_min_credit))
        if self.outcome == "blocked":
            raise ValueError("Daily drawdown guard")
        e = prepare_order(self.ledger, lot, "entry", account="paper")
        if self.outcome == "unknown":
            raise ConnectionError("Transport lost")
        e.status = "Filled"
        e.terminal = True
        e.applied = lot.contracts
        lot.status = TradeStatus.OPEN
        self.ledger.s.commit()


async def run(ledger, book, outcome="filled", *, now=NOW, quote=200, account="paper"):
    async def requote(_placer, candidate):
        return replace(candidate, net_credit=quote, max_profit=quote)

    return await resume_staged_rolls(
        ledger,
        lambda floor: Placer(ledger, floor, book, outcome),
        account=account,
        mode=MODE,
        params=StrategyParams(),
        now=now,
        requote=requote,
    )


async def test_only_confirmed_quantity_reopens_incrementally_and_survives_restart(
    setup,
):
    factory, ledger, close = setup
    book = []
    assert await run(ledger, book) == []
    close.applied, close.applied_cash = 1, -101.01
    ledger.s.commit()
    assert await run(ledger, book, account="external") == []
    assert await run(ledger, book, quote=110) == []
    first = await run(ledger, book)
    assert book == [(first[0], 1, 1.13)]
    assert await run(ledger, book) == []
    restarted = Ledger(factory())
    try:
        same = restarted.s.get(ExecutionOrder, close.id)
        same.applied, same.applied_cash, same.terminal = 3, -401.01, True
        restarted.s.commit()
        second = await run(restarted, book)
        assert book[-1] == (second[0], 2, 1.51)
        assert first != second and await run(restarted, book) == []
    finally:
        restarted.s.close()


@pytest.mark.parametrize("outcome", ["unknown", "blocked"])
async def test_ambiguous_or_guard_rejected_child_is_never_retried(setup, outcome):
    _, ledger, close = setup
    close.applied, close.applied_cash = 1, -100
    ledger.s.commit()
    book = []
    assert len(await run(ledger, book, outcome)) == 1
    close.applied, close.applied_cash = 2, -200
    ledger.s.commit()
    assert await run(ledger, book, outcome) == []
    assert len(book) == 1
    assert len(
        list(
            ledger.s.scalars(
                select(ExecutionOrder).where(ExecutionOrder.kind == "entry")
            )
        )
    ) == (1 if outcome == "unknown" else 0)


async def test_expired_target_is_retired_without_an_order(setup):
    _, ledger, close = setup
    close.applied, close.applied_cash = 1, -100
    ledger.s.commit()
    assert await run(ledger, [], now=NOW + timedelta(days=1)) == []
    assert "finished" in close.spec["staged_roll"]
    assert close.spec["staged_roll"]["lots"] == []


async def test_simultaneous_requoting_can_claim_once_only(setup):
    import asyncio

    factory, ledger, close = setup
    close.applied, close.applied_cash = 1, -100
    ledger.s.commit()
    book = []

    async def run_overlap(lg):
        async def quote(_p, c):
            await asyncio.sleep(0.01)
            return c

        return await resume_staged_rolls(
            lg,
            lambda floor: Placer(lg, floor, book, "filled"),
            mode=MODE,
            account="paper",
            params=StrategyParams(),
            now=NOW,
            requote=quote,
        )

    other = Ledger(factory())
    try:
        results = await asyncio.gather(run_overlap(ledger), run_overlap(other))
        assert sorted(map(len, results)) == [0, 1]
        assert len(book) == 1
    finally:
        other.s.close()
