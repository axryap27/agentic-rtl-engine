"""
LLM usage logger — a local, append-only ledger of every LLM call's token usage.

Why this exists
---------------
The LLM clients used to discard ``response.usage`` after reading the message
content, so the only record of spend lived provider-side (the proxy dashboard
for Agent 1 / the diagnoser, the Anthropic console for Agent 3). This module
captures usage locally so the project has its own per-call breakdown,
independent of any dashboard.

What it records (certain) vs. estimates (configurable)
------------------------------------------------------
Token counts come straight from the API response and are exact. Dollar amounts
are only an *estimate* and only appear when you configure a price, because on a
third-party proxy the price-per-token may be the proxy's markup, not the raw
model rate. Set prices via environment variables (USD per 1M tokens):

    USAGE_PRICE_<MODELKEY>_IN     input  price per 1M tokens
    USAGE_PRICE_<MODELKEY>_OUT    output price per 1M tokens

where ``<MODELKEY>`` is the model id upper-cased with every non-alphanumeric
character replaced by ``_`` (e.g. model ``anthropic/claude-sonnet-4.6`` ->
``ANTHROPIC_CLAUDE_SONNET_4_6``). If no price is set for a model, the entry
is logged with tokens only and ``cost_usd: null``.

Design contract
---------------
Logging must NEVER break a pipeline run. Every public function swallows its own
errors — a logging bug cannot crash Stage 1. The log is append-only JSONL at
``artifacts/usage_log.jsonl`` (override with ``USAGE_LOG_PATH``).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _log_path() -> Path:
    override = os.environ.get("USAGE_LOG_PATH")
    if override:
        return Path(override)
    return Path("artifacts") / "usage_log.jsonl"


def _model_key(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", model).upper()


def _price_per_million(model: str) -> tuple[Optional[float], Optional[float]]:
    """Return (input_price, output_price) per 1M tokens from env, or (None, None)."""
    key = _model_key(model)
    def _read(suffix: str) -> Optional[float]:
        raw = os.environ.get(f"USAGE_PRICE_{key}_{suffix}")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None
    return _read("IN"), _read("OUT")


def _extract_tokens(usage: Any) -> tuple[int, int]:
    """Pull (input_tokens, output_tokens) from either SDK's usage object.

    OpenAI-compatible: prompt_tokens / completion_tokens.
    Anthropic:         input_tokens / output_tokens.
    Accepts an object or a dict; missing fields default to 0.
    """
    def _get(obj: Any, *names: str) -> int:
        for n in names:
            if isinstance(obj, dict):
                if obj.get(n) is not None:
                    return int(obj[n])
            else:
                val = getattr(obj, n, None)
                if val is not None:
                    return int(val)
        return 0

    if usage is None:
        return 0, 0
    inp = _get(usage, "prompt_tokens", "input_tokens")
    out = _get(usage, "completion_tokens", "output_tokens")
    return inp, out


def log_usage(
    *,
    agent: str,
    model: str,
    usage: Any,
    run_id: Optional[str] = None,
    call_type: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> Optional[dict]:
    """Append one usage record to the ledger. Never raises.

    Args:
        agent:     which caller ("agent1", "diagnoser", "agent3", ...).
        model:     the model id used for this call.
        usage:     the SDK response's usage object (OpenAI or Anthropic shape).
        run_id:    optional pipeline run id, for per-run attribution.
        call_type: optional sub-label (e.g. "pick_rule", "generate_formal_spec").
        timestamp: optional ISO8601 string; defaults to now (UTC).

    Returns the record dict that was written, or None if logging failed.
    """
    try:
        inp, out = _extract_tokens(usage)
        price_in, price_out = _price_per_million(model)
        cost: Optional[float] = None
        if price_in is not None or price_out is not None:
            cost = round(
                (inp / 1_000_000) * (price_in or 0.0)
                + (out / 1_000_000) * (price_out or 0.0),
                6,
            )

        record = {
            "ts": timestamp or datetime.now(timezone.utc).isoformat(),
            "agent": agent,
            "call_type": call_type,
            "run_id": run_id,
            "model": model,
            "input_tokens": inp,
            "output_tokens": out,
            "total_tokens": inp + out,
            "cost_usd": cost,            # null when no price configured (tokens still logged)
            "cost_is_estimate": cost is not None,
        }

        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")
        return record
    except Exception:
        # Logging must never break a pipeline run.
        return None


def summarize(path: Optional[Path] = None) -> dict:
    """Aggregate the ledger into totals overall and per-agent / per-model.

    Returns a dict with total tokens, total estimated cost (over entries that
    have a price), and breakdowns. Never raises; returns zeros on a missing or
    unreadable log.
    """
    p = path or _log_path()
    summary: dict = {
        "calls": 0,
        "total_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_usd": 0.0,
        "priced_calls": 0,
        "unpriced_calls": 0,
        "by_agent": {},
        "by_model": {},
    }
    try:
        if not p.exists():
            return summary
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            summary["calls"] += 1
            tin = int(rec.get("input_tokens", 0) or 0)
            tout = int(rec.get("output_tokens", 0) or 0)
            summary["input_tokens"] += tin
            summary["output_tokens"] += tout
            summary["total_tokens"] += tin + tout
            cost = rec.get("cost_usd")
            if cost is not None:
                summary["estimated_cost_usd"] = round(summary["estimated_cost_usd"] + float(cost), 6)
                summary["priced_calls"] += 1
            else:
                summary["unpriced_calls"] += 1

            for dim, key in (("by_agent", rec.get("agent")), ("by_model", rec.get("model"))):
                k = key or "unknown"
                bucket = summary[dim].setdefault(
                    k, {"calls": 0, "total_tokens": 0, "estimated_cost_usd": 0.0}
                )
                bucket["calls"] += 1
                bucket["total_tokens"] += tin + tout
                if cost is not None:
                    bucket["estimated_cost_usd"] = round(bucket["estimated_cost_usd"] + float(cost), 6)
        return summary
    except Exception:
        return summary


if __name__ == "__main__":  # pragma: no cover
    # Quick CLI: `python3.11 -m pipeline.usage` prints the running totals.
    import pprint
    pprint.pp(summarize())
