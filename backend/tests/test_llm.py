"""Unit tests for OpenRouter LLM selection layer."""

from unittest.mock import AsyncMock, MagicMock

from tastyagent.decision.llm import (
    _clean_json_text,
    select_trades,
)
from tastyagent.settings import Settings

from .conftest import make_candidate


def test_clean_json_text():
    raw_clean = '{"selections": []}'
    assert _clean_json_text(raw_clean) == '{"selections": []}'

    raw_fenced = '```json\n{"selections": []}\n```'
    assert _clean_json_text(raw_fenced) == '{"selections": []}'

    raw_fenced_no_lang = '```\n{"selections": []}\n```'
    assert _clean_json_text(raw_fenced_no_lang) == '{"selections": []}'


def test_settings_openrouter_defaults(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    s = Settings(openrouter_api_key="")
    assert s.openrouter_model == "deepseek/deepseek-v4.1-flash"
    assert s.openrouter_base_url == "https://openrouter.ai/api/v1"
    assert s.openrouter_api_key == ""


async def test_select_trades_empty_candidates():
    decision, id_map = await select_trades([], {}, {})
    assert decision.selections == []
    assert id_map == {}


async def test_select_trades_with_mocked_client():
    c1 = make_candidate(symbol="SPY")
    c2 = make_candidate(symbol="XLE")

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_choice = MagicMock()

    json_payload = (
        '{"selections": ['
        '{"candidate_id": "SPY-short_strangle-0", "contracts": 1, "rationale": "High IVR"},'
        '{"candidate_id": "GHOST-id-99", "contracts": 1, "rationale": "Hallucinated"}'
        '], "commentary": "Solid regime."}'
    )
    mock_choice.message.content = f"```json\n{json_payload}\n```"
    mock_response.choices = [mock_choice]
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    decision, id_map = await select_trades(
        [c1, c2],
        {"net_liq": 10000.0},
        {"regime": "test"},
        client=mock_client,
        model="deepseek/deepseek-v4.1-flash",
    )

    # Hallucinated ID must be dropped; SPY must be kept
    assert len(decision.selections) == 1
    assert decision.selections[0].candidate_id == "SPY-short_strangle-0"
    assert decision.selections[0].contracts == 1
    assert decision.commentary == "Solid regime."
    assert "SPY-short_strangle-0" in id_map
    assert "XLE-short_strangle-1" in id_map
