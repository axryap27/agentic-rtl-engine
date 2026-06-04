"""
LLM usage logger — a local, append-only ledger of every LLM call's token usage.

Why this exists
---------------
The LLM clients used to discard ``response.usage`` after reading the message
content, so the only record of spend lived provider-side (the proxy dashboard
for Agent 1 / the diagnoser, the Anthropic console for Agent 3). This module
captures usage locally so the project has its own per-call breakdown,
independent of any dashboard.

What it records (certain) vs. estimates (built-in, overridable)
---------------------------------------------------------------
Token counts come straight from the API response and are exact. Dollar amounts
are an *estimate*: a built-in table of published Anthropic list prices (by tier
— opus / sonnet / haiku) is applied automatically so costs show up without any
configuration. These are LIST prices, and a third-party proxy (writingmate) may
bill a different, marked-up rate, so treat every dollar figure as approximate.

To pin an exact rate for a model, set environment variables (USD per 1M tokens);
these always override the built-in table:

    USAGE_PRICE_<MODELKEY>_IN     input  price per 1M tokens
    USAGE_PRICE_<MODELKEY>_OUT    output price per 1M tokens

where ``<MODELKEY>`` is the model id upper-cased with every non-alphanumeric
character replaced by ``_`` (e.g. model ``anthropic/claude-sonnet-4.6`` ->
``ANTHROPIC_CLAUDE_SONNET_4_6``). Each record carries ``price_source``
("default" | "env" | null) so you can see where its rate came from, and
``cost_is_estimate`` is true only for built-in (default) prices. A model that
matches no tier and has no env price is logged with tokens only
(``cost_usd: null``).

Already-logged rows keep whatever cost they were written with. Re-price the
whole file in place against current rates with::

    python3 -m pipeline.usage reprice

Design contract
---------------
Logging must NEVER break a pipeline run. Every public logging function swallows
its own errors — a logging bug cannot crash Stage 1. The log is append-only
JSONL at ``artifacts/usage_log.jsonl`` (override with ``USAGE_LOG_PATH``).
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


# Built-in list-price estimates (USD per 1M tokens), matched by tier substring
# against the lowercased model id. Published Anthropic rates, used as a default
# so costs appear without any configuration. The proxy may bill differently, so
# these are estimates — override per model with USAGE_PRICE_<MODELKEY>_IN/_OUT.
# Matched in order; the tiers below are mutually exclusive so order is moot.
_DEFAULT_PRICES: list[tuple[str, float, float]] = [
    # needle in model.lower()   input/1M   output/1M
    ("opus",                     15.0,      75.0),
    ("sonnet",                    3.0,      15.0),
    ("haiku",                     0.80,      4.0),
]


def _price_per_million(
    model: str,
) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """Resolve (input_price, output_price, source) per 1M tokens for a model.

    Resolution order:
      1. Per-model env override USAGE_PRICE_<MODELKEY>_IN / _OUT  -> source "env"
      2. Built-in tier table (opus / sonnet / haiku)             -> source "default"
      3. No match                                                -> (None, None, None)

    The env override always wins, so any model can be corrected to its real rate.
    """
    key = _model_key(model)

    def _read(suffix: str) -> Optional[float]:
        raw = os.environ.get(f"USAGE_PRICE_{key}_{suffix}")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    env_in, env_out = _read("IN"), _read("OUT")
    if env_in is not None or env_out is not None:
        return env_in, env_out, "env"

    low = model.lower()
    for needle, p_in, p_out in _DEFAULT_PRICES:
        if needle in low:
            return p_in, p_out, "default"

    return None, None, None


def _compute_cost(
    model: str, inp: int, out: int
) -> tuple[Optional[float], Optional[str]]:
    """Return (cost_usd, price_source) for a token count, or (None, None).

    None cost only when the model matches neither an env override nor a tier.
    """
    price_in, price_out, source = _price_per_million(model)
    if price_in is None and price_out is None:
        return None, None
    cost = round(
        (inp / 1_000_000) * (price_in or 0.0)
        + (out / 1_000_000) * (price_out or 0.0),
        6,
    )
    return cost, source


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
        cost, source = _compute_cost(model, inp, out)

        record = {
            "ts": timestamp or datetime.now(timezone.utc).isoformat(),
            "agent": agent,
            "call_type": call_type,
            "run_id": run_id,
            "model": model,
            "input_tokens": inp,
            "output_tokens": out,
            "total_tokens": inp + out,
            "cost_usd": cost,            # null only when the model matches no price
            "price_source": source,      # "default" (built-in estimate) | "env" | null
            "cost_is_estimate": source == "default",
        }

        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")
        return record
    except Exception:
        # Logging must never break a pipeline run.
        return None


def reprice(path: Optional[Path] = None) -> dict:
    """Recompute cost_usd / price_source for every row in the log, in place.

    Useful after prices change (a new env override, or the built-in table moved):
    existing rows were written with whatever rate was active then. This rewrites
    them against current rates. Token counts and every other field are preserved;
    lines that don't parse as JSON are kept verbatim. Writes atomically via a
    temp file. Returns a small summary. Unlike log_usage, this is a CLI utility
    and may raise on a genuinely unwritable path.
    """
    p = path or _log_path()
    result = {"path": str(p), "repriced": 0, "skipped": 0, "newly_priced": 0}
    if not p.exists():
        return result

    new_lines: list[str] = []
    for line in p.read_text().splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            rec = json.loads(s)
        except json.JSONDecodeError:
            new_lines.append(line)  # preserve unparseable lines as-is
            result["skipped"] += 1
            continue
        had_cost = rec.get("cost_usd") is not None
        inp = int(rec.get("input_tokens", 0) or 0)
        out = int(rec.get("output_tokens", 0) or 0)
        cost, source = _compute_cost(str(rec.get("model", "")), inp, out)
        rec["cost_usd"] = cost
        rec["price_source"] = source
        rec["cost_is_estimate"] = source == "default"
        new_lines.append(json.dumps(rec))
        result["repriced"] += 1
        if cost is not None and not had_cost:
            result["newly_priced"] += 1

    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("\n".join(new_lines) + ("\n" if new_lines else ""))
    os.replace(tmp, p)
    return result


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
        "note": (
            "estimated_cost_usd uses built-in list prices unless overridden by "
            "USAGE_PRICE_* env vars; the proxy may bill differently. Approximate."
        ),
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
    # `python3 -m pipeline.usage`          -> print running totals
    # `python3 -m pipeline.usage reprice`  -> recompute costs for existing rows
    import sys
    import pprint

    if len(sys.argv) > 1 and sys.argv[1] == "reprice":
        print("repriced:", reprice())
    else:
        pprint.pp(summarize())
