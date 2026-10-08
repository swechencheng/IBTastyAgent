"""Adaptive LLM layer: OpenRouter LLM selects among guardrail-passing candidates.

The agent's hard guardrails (``strategy/guardrails.py``, ``risk/limits.py``) decide
what is *permissible*. This module is the *adaptive* layer: given only candidates
that already passed those rails, the LLM chooses which to actually open, sizes them,
and writes a plain-English rationale grounded in TastyTrade mechanics.

Design notes:
- Uses OpenRouter's OpenAI-compatible completions endpoint.
- Default model is `deepseek/deepseek-v4.1-flash`, configurable via `OPENROUTER_MODEL`.
- Structured JSON output is enforced and validated against ``LLMDecision``.
- The LLM can only *select from and size* the provided candidates — it cannot
  invent trades. Whatever it returns is re-validated against the guardrails by the
  orchestrator (defense in depth), so a hallucinated or oversized pick is rejected.
"""

from __future__ import annotations

import json
import logging
import os

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from ..models import CandidateTrade

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

SYSTEM_PROMPT = """\
You are the trade-selection layer of IBTastyAgent, an options-trading agent that \
mechanically applies tastytrade's premium-selling methodology. You are given a set \
of candidate trades that have ALREADY passed the agent's hard risk guardrails, plus \
the current portfolio and market regime. Your job: choose which candidates to open \
right now, size each one, and justify it.

tastytrade mechanics you operate by:
- Sell premium when implied volatility is elevated. Prefer higher IV rank (IVR) \
underlyings — richer premium, stronger mean-reversion edge. Between two otherwise \
similar candidates, prefer the higher IVR.
- Trade liquid underlyings: tight bid/ask, high open interest and volume. Prefer \
better liquidity; it lowers slippage on entry and management.
- Stay small and diversified. Avoid concentrating buying power in one underlying or \
one sector. Spread risk across uncorrelated names. Keep plenty of dry powder — do \
not deploy all available buying power at once; favor many small occurrences over a \
few large ones.
- Defined-risk structures (spreads, iron condors) are preferable when buying power \
or account size is constrained; undefined-risk (strangles, naked puts/calls) demand \
ample buying power and high liquidity. On defined-risk spreads, enforce the 1/3 Strike Width \
rule: collected credit must be at least 1/3 of the spread width (e.g. $1.67+ on a $5 spread).
- Mechanical management (handled elsewhere by the agent) takes profits around 50% of \
max credit (or 1/3 spread width, whichever is smaller) and defends near 21 DTE, so favor \
entries with enough premium and time for that to play out.
- "Number of occurrences" beats conviction on any single trade. When unsure, prefer \
spreading smaller positions across more candidates over a large single bet.

Selection rules:
- Choose ONLY from the provided candidates. Never invent a trade, strike, or symbol.
- You may select zero candidates if none are compelling or the portfolio is already \
heavily deployed — returning an empty selection is a valid, often correct answer.
- For each selected candidate, set `contracts` to a quantity consistent with staying \
small and within the portfolio's remaining buying power; the agent will independently \
re-check sizing and risk limits and may reduce or reject your pick.
- Give each pick a concise (1-3 sentence) `rationale` that cites the concrete reason \
(e.g. "IVR 62% with 4/5 liquidity; 16-delta strangle at 45 DTE diversifies away from \
existing tech exposure"). No boilerplate.
- In `commentary`, briefly explain the overall shape of your decision (what you \
prioritized, what you skipped and why).

Response format:
You MUST respond ONLY with a valid JSON object matching this schema:
{
  "selections": [
    {
      "candidate_id": "string (the exact id from candidate list)",
      "contracts": 1,
      "rationale": "string"
    }
  ],
  "commentary": "string"
}
"""


class LLMTradeSelection(BaseModel):
    """One chosen candidate, sized, with rationale."""

    candidate_id: str = Field(description="The id of the chosen candidate (verbatim).")
    contracts: int = Field(ge=1, description="Number of contracts to open.")
    rationale: str = Field(
        description="Concise reason for the trade, citing mechanics."
    )


class LLMDecision(BaseModel):
    selections: list[LLMTradeSelection] = Field(
        default_factory=list, description="Candidates to open now; may be empty."
    )
    commentary: str = Field(
        default="", description="Overall explanation of the decision."
    )


def candidate_id(candidate: CandidateTrade, index: int) -> str:
    """Stable id for referencing a candidate across the request/response."""
    return f"{candidate.symbol}-{candidate.strategy.value}-{index}"


def _candidate_payload(candidate: CandidateTrade, cid: str) -> dict:
    return {
        "id": cid,
        "symbol": candidate.symbol,
        "strategy": candidate.strategy.value,
        "defined_risk": candidate.strategy.is_defined_risk,
        "dte": candidate.dte,
        "iv_rank": round(candidate.iv_rank, 3),
        "net_credit": round(candidate.net_credit, 2),
        "max_profit": round(candidate.max_profit, 2),
        "max_loss": (
            None if candidate.max_loss == float("inf") else round(candidate.max_loss, 2)
        ),
        "buying_power_per_contract": round(candidate.buying_power_reduction, 2),
        "underlying_price": round(candidate.underlying_price, 2),
        "strike_width": candidate.strike_width,
        "credit_width_ratio": (
            round(candidate.credit_width_ratio, 3)
            if candidate.credit_width_ratio is not None
            else None
        ),
        "max_short_leg_delta": round(candidate.max_short_leg_delta, 3),
        "probability_of_profit": round(candidate.probability_of_profit, 3),
        "liquidity": {
            "bid_ask_width_pct": round(candidate.liquidity.bid_ask_width_pct, 4),
            "open_interest": candidate.liquidity.open_interest,
            "daily_volume": candidate.liquidity.daily_volume,
        },
        "earnings_in_days": candidate.earnings_in_days,
    }


def build_user_message(
    candidates: list[CandidateTrade],
    portfolio: dict,
    regime: dict,
) -> tuple[str, dict[str, CandidateTrade]]:
    """Serialize the volatile request context and return (text, id->candidate)."""
    id_map: dict[str, CandidateTrade] = {}
    payload = []
    for i, c in enumerate(candidates):
        cid = candidate_id(c, i)
        id_map[cid] = c
        payload.append(_candidate_payload(c, cid))

    # Deterministic serialization (sorted keys) keeps logs/diffs stable.
    text = (
        "PORTFOLIO:\n"
        + json.dumps(portfolio, indent=2, sort_keys=True)
        + "\n\nMARKET REGIME:\n"
        + json.dumps(regime, indent=2, sort_keys=True)
        + "\n\nCANDIDATES (all have passed hard guardrails):\n"
        + json.dumps(payload, indent=2, sort_keys=True)
        + "\n\nSelect which candidates to open now, size each, and explain in JSON format."
    )
    return text, id_map


def _clean_json_text(text: str) -> str:
    """Strip markdown code fence blocks if returned by the model."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


async def select_trades(
    candidates: list[CandidateTrade],
    portfolio: dict,
    regime: dict,
    *,
    client: AsyncOpenAI | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> tuple[LLMDecision, dict[str, CandidateTrade]]:
    """Ask OpenRouter LLM to choose among candidates. Returns (decision, id->candidate map).

    The caller MUST re-validate every returned selection against the guardrails and
    risk limits before placing any order.
    """
    if not candidates:
        return LLMDecision(selections=[], commentary="No candidates supplied."), {}

    user_text, id_map = build_user_message(candidates, portfolio, regime)

    base_url = base_url or os.environ.get("OPENROUTER_BASE_URL", DEFAULT_BASE_URL)
    model = model or os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)

    if client is None:
        api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            logger.warning("OPENROUTER_API_KEY is not set. Returning empty selection.")
            return (
                LLMDecision(
                    selections=[],
                    commentary="OPENROUTER_API_KEY not set in environment.",
                ),
                id_map,
            )

        headers = {}
        site_url = os.environ.get("OPENROUTER_SITE_URL")
        app_name = os.environ.get("OPENROUTER_APP_NAME", "IBTastyAgent")
        if site_url:
            headers["HTTP-Referer"] = site_url
        if app_name:
            headers["X-Title"] = app_name

        client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            default_headers=headers if headers else None,
        )

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        response_format={"type": "json_object"},
    )

    raw_content = response.choices[0].message.content or "{}"
    try:
        cleaned = _clean_json_text(raw_content)
        decision = LLMDecision.model_validate_json(cleaned)
    except Exception as e:  # noqa: BLE001
        decision = LLMDecision(
            selections=[],
            commentary=f"Failed to parse LLM response as JSON: {e}. Raw content: {raw_content[:200]}",
        )

    # Drop any hallucinated candidate ids defensively (guardrails re-check the rest).
    decision.selections = [s for s in decision.selections if s.candidate_id in id_map]
    return decision, id_map
