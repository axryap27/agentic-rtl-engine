"""
Deterministic unit tests for the LLM usage ledger (pipeline/usage.py).

NO LLM, NO network. White-box: we call the module's private helpers directly
(``_extract_tokens``, ``_base_rates``, ``_official_rates``, ``_conservative_cost``)
alongside the public surface (``log_usage``, ``record_baseline``, ``agent_cost``,
``check_budget``).

Isolation (critical): every test runs through the ``isolated_ledger`` fixture,
which redirects ALL ledger I/O to a tmp file via ``USAGE_LOG_PATH`` and pins
``USAGE_SAFETY_FACTOR=1.0``. The real ``artifacts/usage/`` ledger is never read
or written. Each test gets its own tmp file, so baselines never accumulate
across tests.

Run with:
    python3.11 -m pytest tests/test_usage_ledger.py -q
"""

from __future__ import annotations

import os
import sys
import types

import pytest

# Project root on sys.path (mirrors the other test files).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import usage


# ---------------------------------------------------------------------------
# Isolation fixture — never touch the real artifacts/usage/ ledger.
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    """Redirect all ledger I/O to a per-test tmp file and pin SAFETY=1.0.

    - USAGE_LOG_PATH forces a single flat file (both reads and writes route
      through it — see usage._single_file_override / _record_files).
    - USAGE_LOG_DIR is deleted so the default per-day dir is never consulted.
    - USAGE_SAFETY_FACTOR=1.0 makes the cost formula exact (no safety scaling).
    - Any pre-existing USAGE_PRICE_* env overrides are cleared so the rate
      table is the official one (the env-override test sets its own).
    """
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("USAGE_LOG_PATH", str(ledger))
    monkeypatch.delenv("USAGE_LOG_DIR", raising=False)
    monkeypatch.setenv("USAGE_SAFETY_FACTOR", "1.0")
    for k in list(os.environ):
        if k.startswith("USAGE_PRICE_"):
            monkeypatch.delenv(k, raising=False)
    return ledger


# A timestamp far in the future, so a logged call's ts sorts strictly AFTER any
# baseline ts (which record_baseline stamps with datetime.now). agent_cost only
# counts calls with ts strictly greater than the latest baseline ts.
_FUTURE_TS = "2999-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# 1. check_budget boundary (>= guard) via record_baseline for an EXACT spend.
# ---------------------------------------------------------------------------

def test_check_budget_empty_ledger_under_budget(isolated_ledger):
    # No records at all: cost is 0, budget check returns 0.0 and does not raise.
    assert usage.agent_cost("agent3") == 0.0
    assert usage.check_budget("agent3", 100.0, 0.5) == 0.0


def test_check_budget_just_under_returns_spend(isolated_ledger):
    usage.record_baseline("agent3", 99.0)
    # 99.0 + 0.5 reserve = 99.5 < 100.0 -> within budget, returns the spend.
    spent = usage.check_budget("agent3", 100.0, 0.5)
    assert spent == pytest.approx(99.0)


def test_check_budget_exactly_at_cap_raises(isolated_ledger):
    usage.record_baseline("agent3", 99.5)
    # 99.5 + 0.5 == 100.0, and the guard is `>=`, so this must raise.
    with pytest.raises(usage.BudgetExceeded):
        usage.check_budget("agent3", 100.0, 0.5)


def test_check_budget_over_cap_raises(isolated_ledger):
    usage.record_baseline("agent3", 100.0)
    with pytest.raises(usage.BudgetExceeded):
        usage.check_budget("agent3", 100.0, 0.5)


# ---------------------------------------------------------------------------
# 2. agent_cost reconciliation: baseline + calls logged strictly after it.
# ---------------------------------------------------------------------------

def test_agent_cost_baseline_plus_later_call(isolated_ledger):
    usage.record_baseline("agent3", 50.0)
    # 1e6 input tokens at opus-4.5 $5/1M = $5.00 (output 0).
    usage.log_usage(
        agent="agent3",
        model="claude-opus-4-5",
        usage={"input_tokens": 1_000_000, "output_tokens": 0},
        timestamp=_FUTURE_TS,
    )
    assert usage.agent_cost("agent3") == pytest.approx(50.0 + 5.0)


# ---------------------------------------------------------------------------
# 3. log_usage NEVER raises (the logging contract) — returns dict or None.
# ---------------------------------------------------------------------------

def test_log_usage_never_raises_on_bad_inputs(isolated_ledger):
    # usage=None -> a record with all-zero token fields.
    rec = usage.log_usage(agent="a", model="claude-opus-4-5", usage=None)
    assert isinstance(rec, dict)
    assert rec["input_tokens"] == 0
    assert rec["output_tokens"] == 0
    assert rec["cache_write_tokens"] == 0
    assert rec["cache_read_tokens"] == 0
    assert rec["total_tokens"] == 0

    # usage=object() with no attributes -> must not raise; dict or None.
    rec2 = usage.log_usage(agent="a", model="claude-opus-4-5", usage=object())
    assert rec2 is None or isinstance(rec2, dict)

    # Empty and garbage model ids -> must not raise.
    rec3 = usage.log_usage(agent="a", model="", usage=None)
    assert rec3 is None or isinstance(rec3, dict)
    rec4 = usage.log_usage(agent="a", model="garbage-model", usage=None)
    assert rec4 is None or isinstance(rec4, dict)


# ---------------------------------------------------------------------------
# 4. _extract_tokens — both SDK shapes (dict + attr object) and None.
# ---------------------------------------------------------------------------

def test_extract_tokens_openai_dict():
    toks = usage._extract_tokens({"prompt_tokens": 100, "completion_tokens": 50})
    assert toks == {"input": 100, "output": 50, "cache_write": 0, "cache_read": 0}


def test_extract_tokens_openai_attr_object():
    obj = types.SimpleNamespace(prompt_tokens=100, completion_tokens=50)
    toks = usage._extract_tokens(obj)
    assert toks == {"input": 100, "output": 50, "cache_write": 0, "cache_read": 0}


def test_extract_tokens_anthropic_dict():
    toks = usage._extract_tokens({
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 10,
        "cache_read_input_tokens": 5,
    })
    assert toks == {"input": 100, "output": 50, "cache_write": 10, "cache_read": 5}


def test_extract_tokens_none_all_zeros():
    assert usage._extract_tokens(None) == {
        "input": 0, "output": 0, "cache_write": 0, "cache_read": 0
    }


# ---------------------------------------------------------------------------
# 5. model -> rate table (official, per-version) and the env override.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model, expected", [
    ("claude-opus-4-5", (5.0, 25.0)),
    ("claude-opus-4-5-20250929", (5.0, 25.0)),   # date-stamped id
    ("claude-opus-4-1", (15.0, 75.0)),
    ("claude-sonnet-4-6", (3.0, 15.0)),
    ("claude-haiku-4-5", (1.0, 5.0)),
    ("claude-haiku-3-5", (0.80, 4.0)),
    ("gpt-4o", (15.0, 75.0)),                     # unknown -> conservative fallback
    ("mystery-x", (15.0, 75.0)),                  # unknown -> conservative fallback
])
def test_official_rates_table(model, expected):
    in_rate, out_rate, _source = usage._official_rates(model)
    assert (in_rate, out_rate) == expected


@pytest.mark.parametrize("model, expected", [
    ("claude-opus-4-5", (5.0, 25.0)),
    ("claude-opus-4-1", (15.0, 75.0)),
    ("claude-sonnet-4-6", (3.0, 15.0)),
    ("claude-haiku-4-5", (1.0, 5.0)),
    ("claude-haiku-3-5", (0.80, 4.0)),
    ("gpt-4o", (15.0, 75.0)),
])
def test_base_rates_match_official_without_override(isolated_ledger, model, expected):
    # isolated_ledger clears any USAGE_PRICE_* so _base_rates falls through to
    # the official table.
    in_rate, out_rate, _source = usage._base_rates(model)
    assert (in_rate, out_rate) == expected


def test_base_rates_env_override_wins(isolated_ledger, monkeypatch):
    # Key: model id upper-cased, non-alphanumerics -> underscore.
    monkeypatch.setenv("USAGE_PRICE_CLAUDE_OPUS_4_5_IN", "1.0")
    in_rate, _out_rate, source = usage._base_rates("claude-opus-4-5")
    assert in_rate == 1.0
    assert source == "env"


# ---------------------------------------------------------------------------
# 6. _conservative_cost sanity (SAFETY=1.0).
# ---------------------------------------------------------------------------

def test_conservative_cost_input_only(isolated_ledger):
    cost, _source = usage._conservative_cost(
        "claude-opus-4-5",
        {"input": 1_000_000, "output": 0, "cache_write": 0, "cache_read": 0},
    )
    assert cost == pytest.approx(5.0)


def test_conservative_cost_applies_output_rate(isolated_ledger):
    # 1e6 input @ $5 + 1e6 output @ $25 = $30.00 for opus-4.5.
    cost, _source = usage._conservative_cost(
        "claude-opus-4-5",
        {"input": 1_000_000, "output": 1_000_000, "cache_write": 0, "cache_read": 0},
    )
    assert cost == pytest.approx(30.0)
