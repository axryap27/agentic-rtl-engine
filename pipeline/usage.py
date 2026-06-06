"""
LLM usage logger — per-day, append-only ledgers of every LLM call's token usage,
with a conservative cost estimate, 8 AM-anchored sessions, and a budget guard.

Storage layout (per-day, easy to read and follow)
-------------------------------------------------
Each session-day gets its own file:

    artifacts/usage/<YYYY-MM-DD>.jsonl

where <YYYY-MM-DD> is the 8 AM-anchored *session* date (see Sessions below). One
line per call (append-only JSONL). Aggregations glob the whole ``artifacts/usage/``
directory, so the budget guard always sees total spend across every day. For a
clean human view of any day:

    python3 -m pipeline.usage              # today's report (formatted table)
    python3 -m pipeline.usage report 2026-06-06
    python3 -m pipeline.usage days         # one line per day

Overrides: ``USAGE_LOG_DIR`` changes the directory; ``USAGE_LOG_PATH`` forces a
single flat file (used by tests for isolation). A pre-existing monolithic
``artifacts/usage_log.jsonl`` is still read for back-compat; ``migrate`` splits it
into per-day files and archives it.

Tokens are exact. Dollars are a CONSERVATIVE estimate
-----------------------------------------------------
Token counts come straight from the API response and are exact. Dollar amounts
are an *estimate*, biased so they never silently under-count spend:

  cost = SAFETY * (1/1e6) * [ in        * p_in
                            + cache_wr  * p_in * 1.25      (5-minute cache write)
                            + cache_rd  * p_in * 0.10      (cache hit)
                            + out       * p_out ]

p_in / p_out are the official per-1M base rates for the call's model, resolved
per VERSION (Opus 4.5+ = $5/$25; Opus 4.1/4.0 = $15/$75; Sonnet 4.x = $3/$15;
Haiku 4.5 = $1/$5; Haiku 3.5 = $0.80/$4). Source:
https://platform.claude.com/docs/en/about-claude/pricing (fetched 2026-06).

Where uncertain, the cost rounds UP: unknown model -> worst-case $15/$75; proxy
(OpenAI-shape) responses bill the whole prompt at p_in with no cache discount;
SAFETY (env ``USAGE_SAFETY_FACTOR``, default 1.0) scales everything. Override an
exact rate with ``USAGE_PRICE_<MODELKEY>_IN/_OUT`` (model id upper-cased,
non-alphanumerics -> ``_``); these always win.

Sessions (8 AM-anchored, local time)
------------------------------------
A *session* is a ~24h window anchored at 8 AM local wall-clock:
``[08:00 day D, 08:00 day D+1)``, labelled by start date D. Calls after midnight
(00:00-07:59) roll into day D's session, so none escape a bucket. Override the
anchor hour with ``USAGE_SESSION_START_HOUR`` (default 8).

Budget guard & reconciliation
-----------------------------
``check_budget(agent, cap, reserve)`` raises ``BudgetExceeded`` before a call
would push cumulative spend past the cap (the Agent 3 $100 ceiling). Because the
local ledger can miss calls the console saw, ``record_baseline(agent, usd)`` pins
an authoritative figure (read off the provider console); ``agent_cost`` then
counts that baseline plus only calls logged after it.

Design contract: logging NEVER breaks a pipeline run — every logging path
swallows its own errors. ``check_budget`` is the deliberate exception: it raises.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Storage layout — per-day files, repo-anchored (never CWD-relative)
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    # this file is <repo>/pipeline/usage.py
    return Path(__file__).resolve().parent.parent


def _usage_dir() -> Path:
    override = os.environ.get("USAGE_LOG_DIR")
    if override:
        return Path(override)
    return _repo_root() / "artifacts" / "usage"


def _legacy_log_path() -> Path:
    """The old monolithic ledger; still read if present, until `migrate` runs."""
    return _repo_root() / "artifacts" / "usage_log.jsonl"


def _single_file_override() -> Optional[Path]:
    """USAGE_LOG_PATH forces all reads/writes through one flat file (tests)."""
    p = os.environ.get("USAGE_LOG_PATH")
    return Path(p) if p else None


def _day_file(session_id: Optional[str]) -> Path:
    return _usage_dir() / f"{session_id or 'unknown'}.jsonl"


def _write_path_for(session_id: Optional[str]) -> Path:
    ov = _single_file_override()
    return ov if ov is not None else _day_file(session_id)


def _record_files(path: Optional[Path] = None) -> list:
    """Every file aggregation should read. A single explicit path wins; then the
    USAGE_LOG_PATH override; otherwise every per-day file plus any legacy ledger."""
    if path is not None:
        return [Path(path)]
    ov = _single_file_override()
    if ov is not None:
        return [ov]
    files: list = []
    d = _usage_dir()
    if d.exists():
        files.extend(sorted(d.glob("*.jsonl")))
    # The legacy monolith is only relevant at the default location. If
    # USAGE_LOG_DIR redirects the dir (a custom/isolated store, e.g. tests), do
    # not also fold in the default-location legacy file.
    if not os.environ.get("USAGE_LOG_DIR"):
        legacy = _legacy_log_path()
        if legacy.exists():
            files.append(legacy)
    return files


def _iter_records(path: Optional[Path] = None):
    """Yield every JSON record across the relevant file(s). Never raises."""
    for f in _record_files(path):
        try:
            if not f.exists():
                continue
            for line in f.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Small env helpers
# ---------------------------------------------------------------------------

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

# Worst-case Claude rate when a model id can't be recognised (old Opus list price).
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
    """Pull a version like 4.5 / 3.7 from a lowercased id. Handles '4.5'/'4-5'
    separators and ignores trailing date stamps ('...-4-5-20250929' -> 4.5)."""
    m = re.search(r"(\d+)[._-](\d+)", low)
    if m:
        return int(m.group(1)) + int(m.group(2)) / 10.0
    m = re.search(r"(\d+)", low)
    if m:
        return float(m.group(1))
    return None


def _official_rates(model: str) -> tuple:
    """(input, output, source) per 1M tokens from the official table, by version."""
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

    return _FALLBACK_IN, _FALLBACK_OUT, "conservative_fallback"


def _base_rates(model: str) -> tuple:
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

    OpenAI: prompt_tokens / completion_tokens (prompt_tokens already includes any
      cached tokens, so cache_* are 0 — the whole prompt billed at input rate).
    Anthropic: input_tokens + cache_creation_input_tokens + cache_read_input_tokens
      + output_tokens. Accepts an object or a dict; missing fields default to 0.
    """
    def g(obj: Any, *names: str) -> int:
        for n in names:
            v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
            if v is not None:
                return int(v)
        return 0

    if usage is None:
        return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}

    has_openai = (
        usage.get("prompt_tokens") if isinstance(usage, dict)
        else getattr(usage, "prompt_tokens", None)
    ) is not None
    if has_openai:
        return {"input": g(usage, "prompt_tokens"),
                "output": g(usage, "completion_tokens"),
                "cache_write": 0, "cache_read": 0}
    return {"input": g(usage, "input_tokens"),
            "output": g(usage, "output_tokens"),
            "cache_write": g(usage, "cache_creation_input_tokens"),
            "cache_read": g(usage, "cache_read_input_tokens")}


def _conservative_cost(model: str, toks: dict) -> tuple:
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
    A time before the anchor hour belongs to the previous day's session."""
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
    """The session id for 'now'."""
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
    """Append one usage record to that day's ledger. Never raises.

    Returns the record dict written, or None if logging failed.
    """
    try:
        toks = _extract_tokens(usage)
        cost, source = _conservative_cost(model, toks)
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        sid = _session_id_from_ts(ts)

        record = {
            "ts": ts,
            "session": sid,
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
            "cost_usd": cost,
            "price_source": source,
            "cost_is_estimate": source != "env",
        }

        path = _write_path_for(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")
        return record
    except Exception:
        return None


def record_baseline(agent: str, usd: float, path: Optional[Path] = None) -> dict:
    """Record an authoritative cumulative-spend baseline (e.g. read off the
    provider console). agent_cost() then counts this baseline plus only the calls
    logged after it. Returns the appended record. Re-run any time to re-sync."""
    ts = datetime.now(timezone.utc).isoformat()
    rec = {
        "ts": ts,
        "session": _session_id_from_ts(ts),
        "agent": agent,
        "type": "baseline",
        "baseline_usd": float(usd),
    }
    p = path if path is not None else _write_path_for(rec["session"])
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


# ---------------------------------------------------------------------------
# Maintenance — reprice in place, and migrate the legacy monolith to per-day
# ---------------------------------------------------------------------------

def reprice(path: Optional[Path] = None) -> dict:
    """Recompute cost / session / cache fields for every row, across all day
    files, in place (atomic per file). Baseline rows are left untouched."""
    result = {"repriced": 0, "skipped": 0, "newly_priced": 0, "files": 0}
    for f in _record_files(path):
        if not f.exists():
            continue
        new_lines: list = []
        for line in f.read_text().splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                rec = json.loads(s)
            except json.JSONDecodeError:
                new_lines.append(line)
                result["skipped"] += 1
                continue
            if rec.get("type") == "baseline":
                new_lines.append(json.dumps(rec))
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
        tmp = f.with_suffix(f.suffix + ".tmp")
        tmp.write_text("\n".join(new_lines) + ("\n" if new_lines else ""))
        os.replace(tmp, f)
        result["files"] += 1
    return result


def migrate(legacy: Optional[Path] = None) -> dict:
    """Split the legacy monolithic ledger into per-day files, then archive it.
    Idempotent-ish: once archived (.migrated) there is nothing left to split."""
    src = legacy or _legacy_log_path()
    result = {"legacy": str(src), "migrated_rows": 0, "days": 0, "archived": None}
    if not src.exists():
        return result
    by_day: dict = {}
    for line in src.read_text().splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            rec = json.loads(s)
        except json.JSONDecodeError:
            continue
        sid = _session_id_from_ts(rec.get("ts", "")) or "unknown"
        by_day.setdefault(sid, []).append(json.dumps(rec))
    _usage_dir().mkdir(parents=True, exist_ok=True)
    for sid, lines in by_day.items():
        with _day_file(sid).open("a") as f:
            for ln in lines:
                f.write(ln + "\n")
        result["migrated_rows"] += len(lines)
    result["days"] = len(by_day)
    archived = src.with_suffix(src.suffix + ".migrated")
    src.rename(archived)
    result["archived"] = str(archived)
    return result


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def summarize(path: Optional[Path] = None) -> dict:
    """Overall totals plus per-agent / per-model, across all days. Never raises."""
    summary: dict = {
        "calls": 0, "total_tokens": 0, "input_tokens": 0, "output_tokens": 0,
        "cache_write_tokens": 0, "cache_read_tokens": 0,
        "estimated_cost_usd": 0.0, "by_agent": {}, "by_model": {},
        "note": ("estimated_cost_usd is a CONSERVATIVE estimate from official "
                 "list prices (per-version) x USAGE_SAFETY_FACTOR; a proxy may "
                 "bill differently. Override exact rates with USAGE_PRICE_*."),
    }
    try:
        for rec in _iter_records(path):
            if rec.get("type") == "baseline":
                continue
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
                    k, {"calls": 0, "total_tokens": 0, "estimated_cost_usd": 0.0})
                bucket["calls"] += 1
                bucket["total_tokens"] += ti + to + cw + cr
                if cost is not None:
                    bucket["estimated_cost_usd"] = round(bucket["estimated_cost_usd"] + float(cost), 6)
        return summary
    except Exception:
        return summary


def sessions(path: Optional[Path] = None) -> dict:
    """Group the ledger into 8 AM-anchored sessions (recomputed from each ts)."""
    out: dict = {}
    try:
        for rec in _iter_records(path):
            if rec.get("type") == "baseline":
                continue
            sid = _session_id_from_ts(rec.get("ts", "")) or "unknown"
            b = out.setdefault(sid, {
                "calls": 0, "input_tokens": 0, "output_tokens": 0,
                "cache_write_tokens": 0, "cache_read_tokens": 0,
                "total_tokens": 0, "estimated_cost_usd": 0.0, "by_agent": {}})
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
    """Total estimated USD for one session (default: the current one)."""
    sid = session_id or current_session_id()
    return float(sessions(path).get(sid, {}).get("estimated_cost_usd", 0.0))


def days(path: Optional[Path] = None) -> list:
    """One row per day: (session_id, calls, total_tokens, estimated_cost_usd)."""
    return [(sid, v["calls"], v["total_tokens"], v["estimated_cost_usd"])
            for sid, v in sessions(path).items()]


def report(day: Optional[str] = None, path: Optional[Path] = None) -> str:
    """Human-readable, aligned report for one session-day (default: today)."""
    sid = day or current_session_id()
    rows = [r for r in _iter_records(path)
            if r.get("type") != "baseline"
            and _session_id_from_ts(r.get("ts", "")) == sid]
    rows.sort(key=lambda r: r.get("ts", "") or "")

    W = 64
    out = ["=" * W,
           f" Usage — {sid}   (session {_session_start_hour():02d}:00 local -> next day)",
           "=" * W]
    if not rows:
        out.append(" (no usage recorded for this day)")
    else:
        out.append(f" {'time':<8} {'agent':<9} {'call_type':<19} {'in':>6} {'out':>6} {'$':>8}")
        out.append(" " + "-" * (W - 2))
        tin = tout = 0
        tcost = 0.0
        by_agent: dict = {}
        for r in rows:
            t = (r.get("ts", "") or "")[11:19]
            ag = str(r.get("agent") or "?")
            ct = str(r.get("call_type") or "")
            i = int(r.get("input_tokens", 0) or 0)
            o = int(r.get("output_tokens", 0) or 0)
            c = float(r.get("cost_usd") or 0.0)
            tin += i
            tout += o
            tcost += c
            ba = by_agent.setdefault(ag, [0, 0.0])
            ba[0] += 1
            ba[1] += c
            out.append(f" {t:<8} {ag[:9]:<9} {ct[:19]:<19} {i:>6} {o:>6} {c:>8.4f}")
        out.append(" " + "-" * (W - 2))
        out.append(f" {('TOTAL ' + str(len(rows)) + ' calls'):<37}{tin:>6} {tout:>6} {tcost:>8.4f}")
        out.append(" by agent: " + "   ".join(
            f"{a}=${v[1]:.4f}({v[0]})" for a, v in sorted(by_agent.items())))
    a3 = agent_cost("agent3")
    out.append("=" * W)
    out.append(f" agent3 cumulative (all days): ${a3:.4f} / $100   "
               f"headroom ~${max(0.0, 100 - 0.5 - a3):.2f}")
    out.append("=" * W)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Budget guard — pre-flight spend cap (the Agent 3 $100 budget)
# ---------------------------------------------------------------------------

class BudgetExceeded(RuntimeError):
    """Raised when an agent's cumulative ledger spend would exceed its budget."""


def agent_cost(agent: str, path: Optional[Path] = None) -> float:
    """Best estimate of cumulative USD spent by one agent.

    If a baseline has been recorded for the agent (record_baseline), the result
    is that baseline PLUS only the calls logged after it — reconciling the local
    ledger to ground truth even when earlier calls weren't captured. With no
    baseline it is the plain sum.
    """
    base_usd = 0.0
    base_ts = ""
    calls: list = []
    try:
        for rec in _iter_records(path):
            if rec.get("agent") != agent:
                continue
            if rec.get("type") == "baseline":
                ts = rec.get("ts", "") or ""
                if ts >= base_ts:                 # ISO timestamps sort lexically
                    base_ts = ts
                    base_usd = float(rec.get("baseline_usd", 0.0) or 0.0)
            else:
                c = rec.get("cost_usd")
                if c is not None:
                    calls.append((rec.get("ts", "") or "", float(c)))
    except Exception:
        pass
    total = base_usd + sum(c for ts, c in calls if ts > base_ts)
    return round(total, 6)


def check_budget(
    agent: str,
    budget_usd: float,
    reserve_usd: float = 0.0,
    path: Optional[Path] = None,
) -> float:
    """Pre-flight budget check. Raise BudgetExceeded if the next call could exceed.

    Compares cumulative spend + reserve against the cap. The reserve is a
    conservative stand-in for the not-yet-logged in-flight call, so we stop a
    little early rather than overshoot. Returns the current spend if within
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

    cmd = sys.argv[1] if len(sys.argv) > 1 else "today"
    if cmd in ("today", "report"):
        print(report(sys.argv[2] if len(sys.argv) > 2 else None))
    elif cmd == "days":
        print(f"{'day':<12} {'calls':>6} {'tokens':>10} {'$cost':>11}")
        for sid, calls, tok, cost in days():
            print(f"{sid:<12} {calls:>6} {tok:>10} {cost:>11.4f}")
    elif cmd == "reprice":
        print("repriced:", reprice())
    elif cmd == "migrate":
        print("migrated:", migrate())
    elif cmd == "baseline":
        ag = sys.argv[2] if len(sys.argv) > 2 else "agent3"
        usd = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
        print("recorded:", record_baseline(ag, usd))
        print(f"{ag} reconciled cumulative now: ${agent_cost(ag)}")
    elif cmd == "sessions":
        print(f"current session: {current_session_id()} "
              f"(anchored {_session_start_hour():02d}:00 local)")
        pprint.pp(sessions())
    elif cmd == "summary":
        pprint.pp(summarize())
    else:
        print(report())
