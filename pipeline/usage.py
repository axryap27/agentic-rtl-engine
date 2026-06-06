"""
LLM usage logger — a local, append-only ledger of every LLM call's token usage,
with a conservative cost estimate and 8 AM-anchored session bucketing.

Why this exists
---------------
The LLM clients used to discard ``response.usage`` after reading the message
content, so the only record of spend lived provider-side (the proxy dashboard
for Agent 1 / the diagnoser, the Anthropic console for Agent 3). This module
captures usage locally so the project has its own per-call, per-session
breakdown, independent of any dashboard.

Tokens are exact. Dollars are a CONSERVATIVE estimate
-----------------------------------------------------
Token counts come straight from the API response and are exact. Dollar amounts
are an *estimate*, biased so they never silently under-count spend:

  cost = SAFETY * (1/1e6) * [ in        * p_in
                            + cache_wr  * p_in * 1.25      (5-minute cache write)
                            + cache_rd  * p_in * 0.10      (cache hit)
                            + out       * p_out ]

where p_in / p_out are the official per-1M base rates for the call's model,
resolved per VERSION (Opus 4.5+ is $5/$25; Opus 4.1/4.0 is $15/$75; Sonnet 4.x
is $3/$15; Haiku 4.5 is $1/$5; Haiku 3.5 is $0.80/$4). Source:
https://platform.claude.com/docs/en/about-claude/pricing (fetched 2026-06).

Where things are uncertain, the formula rounds the cost UP, not down:
  * Unknown model id  -> the most expensive Claude rate ($15/$75), so an
    unrecognized model can never be under-counted.
  * Proxy (OpenAI-shape) responses -> the whole prompt is billed at p_in with
    NO cache-read discount (the third-party proxy is opaque and may mark up).
  * Cache writes assumed at the 5-minute rate (1.25x); cache reads at the real
    0.10x (a known discount, so no need to inflate it).
  * SAFETY (env ``USAGE_SAFETY_FACTOR``, default 1.0) multiplies everything —
    set it >1 (e.g. 1.1) to add headroom for proxy markup / 1-hour caches /
    US-only inference (a 1.1x modifier on 4.6+ models).

Override a model's rate exactly (these always win) with env vars, USD per 1M:
    USAGE_PRICE_<MODELKEY>_IN     USAGE_PRICE_<MODELKEY>_OUT
where ``<MODELKEY>`` is the model id upper-cased with every non-alphanumeric
char replaced by ``_`` (``anthropic/claude-sonnet-4.6`` -> ``ANTHROPIC_CLAUDE_SONNET_4_6``).

Sessions (8 AM-anchored, local time)
------------------------------------
A *session* is a ~24h window anchored at 8 AM local wall-clock time:
``[08:00 day D, 08:00 day D+1)``, labelled by its start date ``D``. The intended
working window is 08:00 to 23:59 of day D; calls after midnight (00:00-07:59)
roll into day D's session rather than being dropped, so no spend escapes a
bucket. Override the anchor hour with env ``USAGE_SESSION_START_HOUR`` (default 8).

Design contract
---------------
Logging must NEVER break a pipeline run. Every logging path swallows its own
errors. The log is append-only JSONL at ``artifacts/usage_log.jsonl`` (override
with ``USAGE_LOG_PATH``).

CLI::

    python3 -m pipeline.usage             # overall totals
    python3 -m pipeline.usage sessions    # per-session (8 AM-anchored) breakdown
    python3 -m pipeline.usage reprice     # recompute cost/session for existing rows
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Paths and small env helpers
# ---------------------------------------------------------------------------

def _log_path() -> Path:
    override = os.environ.get("USAGE_LOG_PATH")
    if override:
        return Path(override)
    return Path("artifacts") / "usage_log.jsonl"


def _model_key(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", model).upper()


def _safety_factor() -> float:
    raw = os.environ.get("USAGE_SAFETY_FACTOR")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return 1.0


def _session_start_hour() -> int:
    raw = os.environ.get("USAGE_SESSION_START_HOUR")
    if raw:
        try:
            return int(raw) % 24
        except ValueError:
            pass
    return 8


# Cache-pricing multipliers relative to base input price (official, 2026-06).
_CACHE_WRITE_MULT = 1.25   # 5-minute cache write (1-hour write would be 2.0x)
_CACHE_READ_MULT = 0.10    # cache hit / refresh

# Worst-case Claude rate, used when a model id can't be recognised so an
# unknown model is never under-counted. (Old Opus 4.1/4.0 list price.)
_FALLBACK_IN, _FALLBACK_OUT = 15.0, 75.0


# ---------------------------------------------------------------------------
# Price resolution — official per-version base rates, env override wins
# ---------------------------------------------------------------------------

def _read_env_price(key: str, suffix: str) -> Optional[float]:
    raw = os.environ.get(f"USAGE_PRICE_{key}_{suffix}")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_version(low: str) -> Optional[float]:
    """Pull a model version like 4.5 / 3.7 from a lowercased id.

    Handles both '4.5'/'3.7' (proxy) and '4-5' (Anthropic-direct) separators,
    and ignores trailing date stamps ('...-4-5-20250929' -> 4.5).
    """
    m = re.search(r"(\d+)[._-](\d+)", low)   # major.minor: 4-5, 4.5, 3.7
    if m:
        return int(m.group(1)) + int(m.group(2)) / 10.0
    m = re.search(r"(\d+)", low)             # bare major: sonnet-4 -> 4.0
    if m:
        return float(m.group(1))
    return None


def _official_rates(model: str) -> tuple[float, float, str]:
    """(input, output, source) per 1M tokens from the official table, by version.

    source is "official:<tier>-<ver>" when recognised, else "conservative_fallback".
    """
    low = model.lower()
    tier = (
        "opus" if "opus" in low else
        "sonnet" if "sonnet" in low else
        "haiku" if "haiku" in low else None
    )
    ver = _parse_version(low)

    if tier == "opus":
        if ver is None:
            return _FALLBACK_IN, _FALLBACK_OUT, "conservative_fallback:opus"
        if ver >= 4.5:
            return 5.0, 25.0, f"official:opus-{ver:g}"
        return 15.0, 75.0, f"official:opus-{ver:g}"          # 4.1, 4.0, older
    if tier == "sonnet":
        return 3.0, 15.0, f"official:sonnet{('-' + format(ver, 'g')) if ver else ''}"
    if tier == "haiku":
        if ver is not None and ver < 4.5:
            return 0.80, 4.0, f"official:haiku-{ver:g}"       # 3.5 and earlier
        return 1.0, 5.0, f"official:haiku{('-' + format(ver, 'g')) if ver else ''}"

    # Unrecognised model: assume the most expensive Claude rate (conservative).
    return _FALLBACK_IN, _FALLBACK_OUT, "conservative_fallback"


def _base_rates(model: str) -> tuple[float, float, str]:
    """(input, output, source). Env override wins; otherwise official-by-version."""
    key = _model_key(model)
    env_in = _read_env_price(key, "IN")
    env_out = _read_env_price(key, "OUT")
    if env_in is not None or env_out is not None:
        off_in, off_out, _ = _official_rates(model)
        return (
            env_in if env_in is not None else off_in,
            env_out if env_out is not None else off_out,
            "env",
        )
    return _official_rates(model)


# ---------------------------------------------------------------------------
# Token extraction (both SDK shapes) and the conservative cost formula
# ---------------------------------------------------------------------------

def _extract_tokens(usage: Any) -> dict:
    """Return {'input','output','cache_write','cache_read'} from either SDK shape.

    OpenAI-compatible: prompt_tokens / completion_tokens. prompt_tokens already
      includes any cached tokens, so cache_* are 0 and the whole prompt is billed
      at the input rate (conservative for the opaque proxy).
    Anthropic: input_tokens (uncached) + cache_creation_input_tokens (write) +
      cache_read_input_tokens (read) + output_tokens.
    Accepts an object or a dict; missing fields default to 0.
    """
    def g(obj: Any, *names: str) -> int:
        for n in names:
            v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
            if v is not None:
                return int(v)
        return 0

    zero = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
    if usage is None:
        return zero

    has_openai = (
        usage.get("prompt_tokens") if isinstance(usage, dict)
        else getattr(usage, "prompt_tokens", None)
    ) is not None
    if has_openai:
        return {
            "input": g(usage, "prompt_tokens"),
            "output": g(usage, "completion_tokens"),
            "cache_write": 0,
            "cache_read": 0,
        }
    return {
        "input": g(usage, "input_tokens"),
        "output": g(usage, "output_tokens"),
        "cache_write": g(usage, "cache_creation_input_tokens"),
        "cache_read": g(usage, "cache_read_input_tokens"),
    }


def _conservative_cost(model: str, toks: dict) -> tuple[float, str]:
    """Conservative USD estimate for one call. Returns (cost, price_source)."""
    p_in, p_out, source = _base_rates(model)
    cost = (
        toks["input"] * p_in
        + toks["cache_write"] * p_in * _CACHE_WRITE_MULT
        + toks["cache_read"] * p_in * _CACHE_READ_MULT
        + toks["output"] * p_out
    ) / 1_000_000 * _safety_factor()
    return round(cost, 6), source


# ---------------------------------------------------------------------------
# Sessions — 8 AM-anchored local-time windows
# ---------------------------------------------------------------------------

def _session_id_from_ts(ts_iso: str) -> Optional[str]:
    """Map an ISO timestamp to its 8 AM-anchored local session date (YYYY-MM-DD).

    The window is [start_hour:00 day D, start_hour:00 day D+1) in LOCAL time;
    a time before start_hour belongs to the previous day's session.
    """
    try:
        dt = datetime.fromisoformat(ts_iso)
    except Exception:
        return None
    try:
        dt = dt.astimezone()   # aware UTC -> local; naive -> interpreted as local
    except Exception:
        pass
    d = dt.date()
    if dt.hour < _session_start_hour():
        d = d - timedelta(days=1)
    return d.isoformat()


def current_session_id() -> str:
    """The session id for 'now' (used by a budget guard to scope current spend)."""
    return _session_id_from_ts(datetime.now(timezone.utc).isoformat()) or "unknown"


# ---------------------------------------------------------------------------
# Public entry point — append one record per call. Never raises.
# ---------------------------------------------------------------------------

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
        toks = _extract_tokens(usage)
        cost, source = _conservative_cost(model, toks)
        ts = timestamp or datetime.now(timezone.utc).isoformat()

        record = {
            "ts": ts,
            "session": _session_id_from_ts(ts),
            "agent": agent,
            "call_type": call_type,
            "run_id": run_id,
            "model": model,
            "input_tokens": toks["input"],
            "output_tokens": toks["output"],
            "cache_write_tokens": toks["cache_write"],
            "cache_read_tokens": toks["cache_read"],
            "total_tokens": toks["input"] + toks["output"]
            + toks["cache_write"] + toks["cache_read"],
            "cost_usd": cost,                 # conservative estimate (USD)
            "price_source": source,           # "official:..." | "env" | "conservative_fallback"
            "cost_is_estimate": source != "env",
        }

        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")
        return record
    except Exception:
        # Logging must never break a pipeline run.
        return None


# ---------------------------------------------------------------------------
# Re-pricing existing rows (after a price change) — in place, atomic
# ---------------------------------------------------------------------------

def reprice(path: Optional[Path] = None) -> dict:
    """Recompute cost / session / cache fields for every row, in place.

    Existing rows were written with whatever rate/logic was active then; this
    rewrites them against the current formula. Token counts and other fields are
    preserved; unparseable lines are kept verbatim. Atomic via a temp file.
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
            new_lines.append(line)
            result["skipped"] += 1
            continue
        toks = {
            "input": int(rec.get("input_tokens", 0) or 0),
            "output": int(rec.get("output_tokens", 0) or 0),
            "cache_write": int(rec.get("cache_write_tokens", 0) or 0),
            "cache_read": int(rec.get("cache_read_tokens", 0) or 0),
        }
        had_cost = rec.get("cost_usd") is not None
        cost, source = _conservative_cost(str(rec.get("model", "")), toks)
        rec["cache_write_tokens"] = toks["cache_write"]
        rec["cache_read_tokens"] = toks["cache_read"]
        rec["total_tokens"] = sum(toks.values())
        rec["cost_usd"] = cost
        rec["price_source"] = source
        rec["cost_is_estimate"] = source != "env"
        rec["session"] = _session_id_from_ts(rec.get("ts", ""))
        new_lines.append(json.dumps(rec))
        result["repriced"] += 1
        if cost is not None and not had_cost:
            result["newly_priced"] += 1

    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("\n".join(new_lines) + ("\n" if new_lines else ""))
    os.replace(tmp, p)
    return result


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _iter_records(p: Path):
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def summarize(path: Optional[Path] = None) -> dict:
    """Aggregate the ledger into overall totals plus per-agent / per-model.

    Never raises; returns zeros on a missing or unreadable log.
    """
    p = path or _log_path()
    summary: dict = {
        "calls": 0,
        "total_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
        "estimated_cost_usd": 0.0,
        "by_agent": {},
        "by_model": {},
        "note": (
            "estimated_cost_usd is a CONSERVATIVE estimate from official list "
            "prices (per-version) x USAGE_SAFETY_FACTOR; a proxy may bill "
            "differently. Override exact rates with USAGE_PRICE_* env vars."
        ),
    }
    try:
        for rec in _iter_records(p):
            ti = int(rec.get("input_tokens", 0) or 0)
            to = int(rec.get("output_tokens", 0) or 0)
            cw = int(rec.get("cache_write_tokens", 0) or 0)
            cr = int(rec.get("cache_read_tokens", 0) or 0)
            cost = rec.get("cost_usd")
            summary["calls"] += 1
            summary["input_tokens"] += ti
            summary["output_tokens"] += to
            summary["cache_write_tokens"] += cw
            summary["cache_read_tokens"] += cr
            summary["total_tokens"] += ti + to + cw + cr
            if cost is not None:
                summary["estimated_cost_usd"] = round(summary["estimated_cost_usd"] + float(cost), 6)
            for dim, key in (("by_agent", rec.get("agent")), ("by_model", rec.get("model"))):
                k = key or "unknown"
                bucket = summary[dim].setdefault(
                    k, {"calls": 0, "total_tokens": 0, "estimated_cost_usd": 0.0}
                )
                bucket["calls"] += 1
                bucket["total_tokens"] += ti + to + cw + cr
                if cost is not None:
                    bucket["estimated_cost_usd"] = round(bucket["estimated_cost_usd"] + float(cost), 6)
        return summary
    except Exception:
        return summary


def sessions(path: Optional[Path] = None) -> dict:
    """Group the ledger into 8 AM-anchored sessions (derived from each row's ts).

    Returns an ordered dict ``{session_id: {calls, tokens, estimated_cost_usd,
    by_agent}}``. Session id is recomputed from ``ts`` so it always reflects the
    current anchor/timezone, regardless of what was stamped at write time.
    """
    p = path or _log_path()
    out: dict = {}
    try:
        for rec in _iter_records(p):
            sid = _session_id_from_ts(rec.get("ts", "")) or "unknown"
            b = out.setdefault(sid, {
                "calls": 0, "input_tokens": 0, "output_tokens": 0,
                "cache_write_tokens": 0, "cache_read_tokens": 0,
                "total_tokens": 0, "estimated_cost_usd": 0.0, "by_agent": {},
            })
            ti = int(rec.get("input_tokens", 0) or 0)
            to = int(rec.get("output_tokens", 0) or 0)
            cw = int(rec.get("cache_write_tokens", 0) or 0)
            cr = int(rec.get("cache_read_tokens", 0) or 0)
            cost = rec.get("cost_usd")
            b["calls"] += 1
            b["input_tokens"] += ti
            b["output_tokens"] += to
            b["cache_write_tokens"] += cw
            b["cache_read_tokens"] += cr
            b["total_tokens"] += ti + to + cw + cr
            if cost is not None:
                b["estimated_cost_usd"] = round(b["estimated_cost_usd"] + float(cost), 6)
            a = rec.get("agent") or "unknown"
            ab = b["by_agent"].setdefault(a, {"calls": 0, "estimated_cost_usd": 0.0})
            ab["calls"] += 1
            if cost is not None:
                ab["estimated_cost_usd"] = round(ab["estimated_cost_usd"] + float(cost), 6)
        return dict(sorted(out.items()))
    except Exception:
        return out


def session_cost(session_id: Optional[str] = None, path: Optional[Path] = None) -> float:
    """Total estimated USD for one session (default: the current one).

    Foundation for the Agent-3 budget guard: scope spend to the active session.
    """
    sid = session_id or current_session_id()
    return float(sessions(path).get(sid, {}).get("estimated_cost_usd", 0.0))


# ---------------------------------------------------------------------------
# Budget guard — pre-flight spend cap (foundation: the Agent 3 $100 budget)
# ---------------------------------------------------------------------------

class BudgetExceeded(RuntimeError):
    """Raised when an agent's cumulative ledger spend would exceed its budget."""


def agent_cost(agent: str, path: Optional[Path] = None) -> float:
    """Cumulative estimated USD logged for one agent across the whole ledger."""
    total = 0.0
    try:
        for rec in _iter_records(path or _log_path()):
            if rec.get("agent") == agent:
                c = rec.get("cost_usd")
                if c is not None:
                    total += float(c)
    except Exception:
        pass
    return round(total, 6)


def check_budget(
    agent: str,
    budget_usd: float,
    reserve_usd: float = 0.0,
    path: Optional[Path] = None,
) -> float:
    """Pre-flight budget check. Raise BudgetExceeded if the next call could exceed.

    Sums the agent's cumulative cost from the ledger and compares
    ``spent + reserve_usd`` against ``budget_usd``. The reserve is a conservative
    stand-in for the not-yet-logged cost of the call about to be made, so we stop
    slightly early rather than overshoot. Returns the current spend if within
    budget. Unlike the logging functions, this is MEANT to raise.
    """
    spent = agent_cost(agent, path)
    if spent + reserve_usd >= budget_usd:
        raise BudgetExceeded(
            f"Agent '{agent}' budget reached: spent ~${spent:.4f} + reserve "
            f"${reserve_usd:.2f} >= cap ${budget_usd:.2f}. Refusing further calls. "
            f"Top up credits or raise the cap (AGENT3_BUDGET_USD) to continue."
        )
    return spent


if __name__ == "__main__":  # pragma: no cover
    import sys
    import pprint

    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    if cmd == "reprice":
        print("repriced:", reprice())
    elif cmd == "sessions":
        print(f"current session: {current_session_id()}  "
              f"(anchored at {_session_start_hour():02d}:00 local)")
        pprint.pp(sessions())
    else:
        pprint.pp(summarize())
