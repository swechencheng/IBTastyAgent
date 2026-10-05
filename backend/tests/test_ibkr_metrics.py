import os
import sqlite3
import tempfile
import pytest

from unittest.mock import AsyncMock, MagicMock
from tastyagent.ibkr.metrics import (
    IVMetrics,
    _get_cached_metric,
    _init_cache_table,
    _save_cached_metric,
    fetch_symbol_iv_metric,
)


def test_iv_metrics_cache_save_and_retrieve():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        _init_cache_table(db_path)
        metric = IVMetrics(
            symbol="TEST",
            iv_rank=0.45,
            iv_percentile=0.55,
            current_iv=0.35,
            min_iv=0.20,
            max_iv=0.50,
        )
        today_str = "2026-09-25"
        _save_cached_metric(metric, today_str, db_path)

        cached = _get_cached_metric("TEST", today_str, db_path)
        assert cached is not None
        assert cached.symbol == "TEST"
        assert cached.iv_rank == 0.45
        assert cached.iv_percentile == 0.55
        assert cached.current_iv == 0.35
        assert cached.min_iv == 0.20
        assert cached.max_iv == 0.50

        # Different date should return None (no fallback to prior days)
        assert _get_cached_metric("TEST", "2026-09-24", db_path) is None
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_iv_rank_math_formula():
    cur_iv = 0.40
    min_iv = 0.20
    max_iv = 0.60
    ivr = (cur_iv - min_iv) / (max_iv - min_iv)
    assert ivr == pytest.approx(0.50)  # Exactly 50% IV rank


@pytest.mark.asyncio
async def test_fetch_symbol_iv_metric_retries_with_backoff_and_succeeds():
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True
    mock_ib.qualifyContractsAsync = AsyncMock(return_value=None)

    # Fail on first attempt, succeed on second attempt
    call_count = 0

    class MockBar:
        def __init__(self, close):
            self.close = close

    async def mock_req_historical(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("Temporary glitch")
        # Return 252 bars
        return [MockBar(0.20 + i * 0.001) for i in range(252)]

    mock_ib.reqHistoricalDataAsync = mock_req_historical

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        _init_cache_table(db_path)
        m = await fetch_symbol_iv_metric(
            mock_ib,
            "SPY",
            "2026-10-05",
            db_path=db_path,
            max_retry_seconds=5.0,
            initial_backoff=0.01,
            backoff_multiplier=2.0,
        )
        assert m.symbol == "SPY"
        assert m.iv_rank is not None
        assert call_count == 2
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


@pytest.mark.asyncio
async def test_fetch_symbol_iv_metric_fails_after_retries_without_prior_cache_fallback():
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True
    mock_ib.qualifyContractsAsync = AsyncMock(return_value=None)
    mock_ib.reqHistoricalDataAsync = AsyncMock(
        side_effect=ConnectionError("Persistent error")
    )

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        _init_cache_table(db_path)
        # Pre-seed prior date in cache (e.g. yesterday)
        prior_metric = IVMetrics(
            symbol="SPY",
            iv_rank=0.88,
            iv_percentile=0.90,
            current_iv=0.30,
            min_iv=0.15,
            max_iv=0.45,
        )
        _save_cached_metric(prior_metric, "2026-10-02", db_path)

        # Live fetch fails and retries up to max_retry_seconds (using tiny backoff for fast test)
        m = await fetch_symbol_iv_metric(
            mock_ib,
            "SPY",
            "2026-10-05",
            db_path=db_path,
            max_retry_seconds=0.05,
            initial_backoff=0.01,
            backoff_multiplier=2.0,
        )
        # MUST return iv_rank=None, and NOT fall back to 0.88 from 2026-10-02
        assert m.symbol == "SPY"
        assert m.iv_rank is None
        assert m.current_iv is None
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)
