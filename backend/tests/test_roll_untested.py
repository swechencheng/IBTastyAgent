from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from ib_async import ContractDetails, Option
from tastyagent.config import StrategyParams
from tastyagent.db.models import Trade, TradeLeg, TradeStatus
from tastyagent.execution.exit_manager import PositionMark
from tastyagent.ibkr.client import IBKRClient
from tastyagent.ibkr.marketdata import OptionSnapshot
from tastyagent.models import OptionType, Strategy
from tastyagent.runner import _build_roll_candidate
from tastyagent.settings import Settings
from tastyagent.strategy.exits import RollKind


@pytest.fixture
def mock_client():
    settings = Settings(ibkr_account="U123456")
    client = IBKRClient(settings)
    client.trading_ib = MagicMock()
    client.data_ib = MagicMock()
    return client


def make_ic_trade():
    return Trade(
        id=270,
        symbol="AAPL",
        strategy="iron_condor",
        contracts=1,
        status=TradeStatus.OPEN,
        legs=[
            TradeLeg(
                option_type="put",
                strike=320.0,
                expiration=date(2026, 11, 20),
                action="sell_to_open",
                delta=-0.27,
            ),
            TradeLeg(
                option_type="put",
                strike=290.0,
                expiration=date(2026, 11, 20),
                action="buy_to_open",
                delta=-0.07,
            ),
            TradeLeg(
                option_type="call",
                strike=355.0,
                expiration=date(2026, 11, 20),
                action="sell_to_open",
                delta=0.29,
            ),
            TradeLeg(
                option_type="call",
                strike=385.0,
                expiration=date(2026, 11, 20),
                action="buy_to_open",
                delta=0.06,
            ),
        ],
    )


async def test_build_roll_candidate_untested_call_tested(mock_client):
    trade = make_ic_trade()
    params = StrategyParams(target_short_delta=0.16)
    mark = PositionMark(
        cost_to_close=860.0, max_short_delta=0.32, tested_side=OptionType.CALL
    )

    strikes = [
        280.0,
        290.0,
        300.0,
        310.0,
        320.0,
        330.0,
        340.0,
        350.0,
        355.0,
        360.0,
        385.0,
    ]
    raw_exps = ["20261120"]

    # Mock contracts returned by reqContractDetailsAsync
    contracts = []
    snaps = {}
    con_id = 1000
    for s in strikes:
        for right in ("P", "C"):
            con_id += 1
            opt = Option("AAPL", "20261120", s, right, "SMART", currency="USD")
            opt.conId = con_id
            contracts.append(opt)
            # Assign synthetic delta and prices
            if right == "P":
                # Delta increases as strike gets closer to spot (underlying ~345)
                # 320 -> -0.15, 330 -> -0.25, 340 -> -0.40, 300 -> -0.05
                delta_map = {
                    300.0: -0.05,
                    310.0: -0.09,
                    320.0: -0.15,
                    330.0: -0.22,
                    340.0: -0.40,
                }
                delta = delta_map.get(s, -0.10)
                mid = max(0.50, (s - 300) * 0.3)
            else:
                delta_map = {355.0: 0.32, 385.0: 0.06}
                delta = delta_map.get(s, 0.20)
                mid = max(0.50, (360 - s) * 0.3) if s < 360 else 1.0
            snaps[con_id] = OptionSnapshot(
                con_id=con_id,
                bid=Decimal(str(round(mid - 0.05, 2))),
                ask=Decimal(str(round(mid + 0.05, 2))),
                delta=delta,
            )

    cds = [MagicMock(contract=c) for c in contracts]
    mock_client.data_ib.reqContractDetailsAsync = AsyncMock(return_value=cds)

    with (
        patch(
            "tastyagent.runner.get_option_chain_parameters",
            AsyncMock(return_value=(raw_exps, strikes)),
        ),
        patch("tastyagent.runner.get_underlying_price", AsyncMock(return_value=345.0)),
        patch("tastyagent.runner.snapshot_options", AsyncMock(return_value=snaps)),
    ):
        cand = await _build_roll_candidate(
            mock_client, trade, RollKind.UNTESTED, mark, params
        )

    assert cand is not None
    assert cand.strategy == Strategy.IRON_CONDOR
    # Call side should stay at 355 / 385
    short_calls = [
        l
        for l in cand.legs
        if l.option_type == OptionType.CALL and "sell" in l.action.value.lower()
    ]
    assert short_calls[0].strike == 355.0
    # Put short should have rolled up from 320 towards spot (e.g. 330)
    short_puts = [
        l
        for l in cand.legs
        if l.option_type == OptionType.PUT and "sell" in l.action.value.lower()
    ]
    assert short_puts[0].strike > 320.0
    assert short_puts[0].strike < 355.0
    # Put long wing width should be maintained (30 points wide)
    long_puts = [
        l
        for l in cand.legs
        if l.option_type == OptionType.PUT and "buy" in l.action.value.lower()
    ]
    assert abs(short_puts[0].strike - long_puts[0].strike) == 30.0


async def test_build_roll_candidate_untested_put_tested(mock_client):
    trade = make_ic_trade()
    params = StrategyParams(target_short_delta=0.16)
    mark = PositionMark(
        cost_to_close=860.0, max_short_delta=0.35, tested_side=OptionType.PUT
    )

    strikes = [
        280.0,
        290.0,
        300.0,
        310.0,
        320.0,
        330.0,
        340.0,
        350.0,
        355.0,
        360.0,
        370.0,
        380.0,
        385.0,
    ]
    raw_exps = ["20261120"]

    contracts = []
    snaps = {}
    con_id = 2000
    for s in strikes:
        for right in ("P", "C"):
            con_id += 1
            opt = Option("AAPL", "20261120", s, right, "SMART", currency="USD")
            opt.conId = con_id
            contracts.append(opt)
            if right == "C":
                # Delta increases as strike decreases towards spot (underlying ~315)
                # 355 -> 0.10, 340 -> 0.18, 330 -> 0.30
                delta_map = {355.0: 0.10, 340.0: 0.18, 330.0: 0.30, 360.0: 0.08}
                delta = delta_map.get(s, 0.15)
                mid = max(0.50, (360 - s) * 0.3)
            else:
                delta_map = {320.0: -0.35, 290.0: -0.07}
                delta = delta_map.get(s, -0.20)
                mid = max(0.50, (s - 290) * 0.3)
            snaps[con_id] = OptionSnapshot(
                con_id=con_id,
                bid=Decimal(str(round(mid - 0.05, 2))),
                ask=Decimal(str(round(mid + 0.05, 2))),
                delta=delta,
            )

    cds = [MagicMock(contract=c) for c in contracts]
    mock_client.data_ib.reqContractDetailsAsync = AsyncMock(return_value=cds)

    with (
        patch(
            "tastyagent.runner.get_option_chain_parameters",
            AsyncMock(return_value=(raw_exps, strikes)),
        ),
        patch("tastyagent.runner.get_underlying_price", AsyncMock(return_value=315.0)),
        patch("tastyagent.runner.snapshot_options", AsyncMock(return_value=snaps)),
    ):
        cand = await _build_roll_candidate(
            mock_client, trade, RollKind.UNTESTED, mark, params
        )

    assert cand is not None
    assert cand.strategy == Strategy.IRON_CONDOR
    # Put side should stay at 320 / 290
    short_puts = [
        l
        for l in cand.legs
        if l.option_type == OptionType.PUT and "sell" in l.action.value.lower()
    ]
    assert short_puts[0].strike == 320.0
    # Call short should have rolled down from 355 towards spot (e.g. 340)
    short_calls = [
        l
        for l in cand.legs
        if l.option_type == OptionType.CALL and "sell" in l.action.value.lower()
    ]
    assert short_calls[0].strike < 355.0
    assert short_calls[0].strike > 320.0
    # Call long wing width should be maintained (30 points wide)
    long_calls = [
        l
        for l in cand.legs
        if l.option_type == OptionType.CALL and "buy" in l.action.value.lower()
    ]
    assert abs(short_calls[0].strike - long_calls[0].strike) == 30.0


@pytest.mark.asyncio
async def test_roll_out_preserves_put_credit_spread(mock_client):
    from datetime import timedelta

    exp_dt = date.today() + timedelta(days=45)
    exp_str = exp_dt.strftime("%Y%m%d")
    trade = Trade(
        id=280,
        symbol="SPY",
        strategy="put_credit_spread",
        contracts=1,
        status=TradeStatus.OPEN,
        legs=[
            TradeLeg(
                option_type="put",
                strike=500.0,
                expiration=date.today() + timedelta(days=10),
                action="sell_to_open",
                delta=-0.24,
            ),
            TradeLeg(
                option_type="put",
                strike=490.0,
                expiration=date.today() + timedelta(days=10),
                action="buy_to_open",
                delta=-0.07,
            ),
        ],
    )
    mark = PositionMark(cost_to_close=100.0)
    params = StrategyParams(
        min_dte=20, max_dte=60, target_dte=45, prefer_monthly_expirations=False
    )

    raw_exps = [exp_str]
    strikes = [480.0, 490.0, 500.0, 510.0, 520.0]
    contracts = [
        Option(
            "SPY",
            exp_str,
            s,
            r,
            "SMART",
            conId=int(s * 10 + (1 if r == "C" else 2)),
        )
        for s in strikes
        for r in ("P", "C")
    ]
    snaps = {}
    for c in contracts:
        mid = (
            4.00
            if c.strike == 500.0 and c.right == "P"
            else (1.25 if c.strike == 490.0 and c.right == "P" else 0.50)
        )
        delta = (
            -0.24
            if c.strike == 500.0 and c.right == "P"
            else (-0.07 if c.strike == 490.0 and c.right == "P" else -0.02)
        )
        snaps[c.conId] = OptionSnapshot(
            con_id=c.conId,
            bid=Decimal(str(round(mid - 0.05, 2))),
            ask=Decimal(str(round(mid + 0.05, 2))),
            delta=delta,
        )

    cds = [MagicMock(contract=c) for c in contracts]
    mock_client.data_ib.reqContractDetailsAsync = AsyncMock(return_value=cds)

    with (
        patch(
            "tastyagent.runner.get_option_chain_parameters",
            AsyncMock(return_value=(raw_exps, strikes)),
        ),
        patch("tastyagent.runner.get_underlying_price", AsyncMock(return_value=515.0)),
        patch("tastyagent.runner.snapshot_options", AsyncMock(return_value=snaps)),
        patch(
            "tastyagent.runner.get_iv_metrics",
            AsyncMock(return_value=MagicMock(iv_rank=0.45)),
        ),
    ):
        cand = await _build_roll_candidate(
            mock_client, trade, RollKind.OUT, mark, params
        )

    assert cand is not None
    assert cand.strategy == Strategy.PUT_CREDIT_SPREAD
    assert cand.iv_rank == 0.45
    assert len(cand.legs) == 2
    short_put = [l for l in cand.legs if l.action.value == "sell_to_open"][0]
    long_put = [l for l in cand.legs if l.action.value == "buy_to_open"][0]
    assert short_put.strike == 500.0
    assert long_put.strike == 490.0


@pytest.mark.asyncio
async def test_roll_out_same_strike_fallback_when_underwater(mock_client):
    from datetime import timedelta
    from tastyagent.strategy.guardrails import validate_roll_candidate

    exp_dt = date.today() + timedelta(days=45)
    exp_str = exp_dt.strftime("%Y%m%d")
    trade = Trade(
        id=281,
        symbol="XLE",
        strategy="put_credit_spread",
        contracts=1,
        status=TradeStatus.OPEN,
        legs=[
            TradeLeg(
                option_type="put",
                strike=50.0,
                expiration=date.today() + timedelta(days=12),
                action="sell_to_open",
                delta=-0.62,
            ),
            TradeLeg(
                option_type="put",
                strike=45.0,
                expiration=date.today() + timedelta(days=12),
                action="buy_to_open",
                delta=-0.35,
            ),
        ],
    )
    # Underwater spread: costs $300 to close; fresh ~0.30Δ spread (44/39) only pays $140
    # while preserving 50/45 strikes in the next cycle pays $340 credit with non-expanding risk
    mark = PositionMark(cost_to_close=300.0, max_short_delta=0.62, tested_side=OptionType.PUT)
    params = StrategyParams(
        min_dte=20, max_dte=60, target_dte=45, prefer_monthly_expirations=False
    )

    strikes = [39.0, 44.0, 45.0, 50.0]
    contracts = [
        Option("XLE", exp_str, s, r, "SMART", conId=int(s * 10 + (1 if r == "C" else 2)))
        for s in strikes
        for r in ("P", "C")
    ]
    price_map = {
        (50.0, "P"): (5.20, -0.60),
        (45.0, "P"): (1.80, -0.35),
        (44.0, "P"): (1.70, -0.30),
        (39.0, "P"): (0.30, -0.09),
    }
    snaps = {}
    for c in contracts:
        mid, delta = price_map.get((c.strike, c.right), (0.10, 0.05))
        snaps[c.conId] = OptionSnapshot(
            con_id=c.conId,
            bid=Decimal(str(round(mid - 0.05, 2))),
            ask=Decimal(str(round(mid + 0.05, 2))),
            delta=delta,
        )

    cds = [MagicMock(contract=c) for c in contracts]
    mock_client.data_ib.reqContractDetailsAsync = AsyncMock(return_value=cds)

    with (
        patch(
            "tastyagent.runner.get_option_chain_parameters",
            AsyncMock(return_value=([exp_str], strikes)),
        ),
        patch("tastyagent.runner.get_underlying_price", AsyncMock(return_value=48.0)),
        patch("tastyagent.runner.snapshot_options", AsyncMock(return_value=snaps)),
        patch(
            "tastyagent.runner.get_iv_metrics",
            AsyncMock(return_value=MagicMock(iv_rank=0.15)),
        ),
    ):
        cand = await _build_roll_candidate(
            mock_client, trade, RollKind.OUT, mark, params
        )

    assert cand is not None
    assert cand.net_credit >= mark.cost_to_close
    assert cand.strike_width == 5.0
    short_put = [l for l in cand.legs if l.action.value == "sell_to_open"][0]
    long_put = [l for l in cand.legs if l.action.value == "buy_to_open"][0]
    assert short_put.strike == 50.0
    assert long_put.strike == 45.0
    assert validate_roll_candidate(
        cand,
        roll_kind="out",
        exit_debit_total=mark.cost_to_close,
        contracts=1,
        original_dte_remaining=12,
        original_strike_width=5.0,
        params=params,
    ).ok
