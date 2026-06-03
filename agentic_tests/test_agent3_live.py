"""
Live tests for Agent 3 — the spec author + rule picker.

Transport: Anthropic SDK directly (ANTHROPIC_API_KEY / AGENT3_MODEL), a separate
account from the proxy. Gated: marker `live_llm` + RUN_LIVE_LLM=1 + Anthropic key.

Four entry points are covered:
  generate_formal_spec(summary)        -> FormalSpec
  revise_on_tlc(spec, tlc_errors)      -> FormalSpec
  pick_rule(applicable_rules, spec)    -> {"rule_name": str, "params": dict}
  revise_on_cocotb(spec, sim_log)      -> FormalSpec

The pick_rule tests are the most important in the whole suite: they exercise the
bounded-action-space invariant against the LIVE model (one-shot, no tools, must
return a choice from the offered set).
"""

from __future__ import annotations

import json

from pipeline.agents import agent3
from pipeline.schemas.tla_schema import FormalSpec


# ---------------------------------------------------------------------------
# generate_formal_spec
# ---------------------------------------------------------------------------

def test_agent3_generate_formal_spec_valid(require_anthropic, counter_summary):
    """Agent 3 turns a SpecSummary into a schema-valid FormalSpec."""
    spec = agent3.generate_formal_spec(counter_summary)

    assert isinstance(spec, FormalSpec)
    assert spec.module_name
    assert spec.variables, "FormalSpec must declare at least one variable"
    assert spec.transitions, "FormalSpec must declare at least one transition"


def test_agent3_formalspec_internally_consistent(require_anthropic, counter_summary):
    """Every variable must have an initial value, and transitions must update
    only declared variables — the consistency Compiler 1 depends on."""
    spec = agent3.generate_formal_spec(counter_summary)

    var_names = set(spec.variables)
    for name in spec.initial:
        assert name in var_names, f"initial sets unknown var {name!r}"
    for t in spec.transitions:
        for updated in t.updates:
            assert updated in var_names, (
                f"transition {t.label!r} updates unknown var {updated!r}; "
                f"declared={sorted(var_names)}"
            )


# ---------------------------------------------------------------------------
# pick_rule — bounded action space (the load-bearing invariant)
# ---------------------------------------------------------------------------

def _abstract_counter_engine_spec() -> dict:
    """The engine-format spec a 2-bit counter starts at, before refinement."""
    return {
        "variables": [
            {"name": "count", "type": "0..3", "abstract": True,
             "reset_value": None, "clocked": False, "width": 2},
        ],
        "actions": [
            {"name": "Count", "guard": "en = 1", "updates": [],
             "is_rtl_style": False, "clocked": False},
        ],
        "init": "count = 0",
        "invariants": ["count >= 0"],
        "abstraction_mapping": {},
        "reset_action": None,
        "properties": [],
    }


def test_agent3_pick_rule_returns_a_choice_from_the_set(require_anthropic):
    """pick_rule must return {"rule_name", "params"} with rule_name drawn from
    the offered applicable set — the core bounded-action-space contract."""
    applicable = [
        {"name": "Initialization", "describe": "Add a synchronous reset action and reset values."},
        {"name": "Assignment", "describe": "Add an explicit register update to an action."},
        {"name": "Iteration", "describe": "Mark an action as clocked (per-cycle update)."},
    ]
    offered = {r["name"] for r in applicable}

    choice = agent3.pick_rule(applicable, _abstract_counter_engine_spec())

    assert isinstance(choice, dict)
    assert set(choice.keys()) >= {"rule_name", "params"}, choice
    assert isinstance(choice["params"], dict)
    assert choice["rule_name"] in offered, (
        f"pick_rule chose {choice['rule_name']!r}, not in the offered set {sorted(offered)} "
        "— bounded-action-space violation"
    )


def test_agent3_pick_rule_respects_single_option(require_anthropic):
    """When only one rule applies, pick_rule must choose it (no inventing
    alternatives outside the action space)."""
    applicable = [
        {"name": "Initialization",
         "describe": "Add a synchronous reset action and assign reset values."},
    ]
    choice = agent3.pick_rule(applicable, _abstract_counter_engine_spec())
    assert choice["rule_name"] == "Initialization", choice


def test_agent3_pick_rule_output_json_serializable(require_anthropic):
    """The returned choice must round-trip through JSON — the engine serializes
    it into refinement_chain.json, so non-serializable params would break replay."""
    applicable = [
        {"name": "Initialization", "describe": "Add reset values."},
        {"name": "Assignment", "describe": "Add an explicit update."},
    ]
    choice = agent3.pick_rule(applicable, _abstract_counter_engine_spec())
    # Should not raise.
    json.dumps(choice)


# ---------------------------------------------------------------------------
# revise_on_tlc — error-driven revision
# ---------------------------------------------------------------------------

def test_agent3_revise_on_tlc_returns_valid_spec(require_anthropic, counter_summary):
    """Given a spec and a synthetic TLC error, revise_on_tlc returns a still
    schema-valid FormalSpec (we assert validity, not that it fixes the error)."""
    spec = agent3.generate_formal_spec(counter_summary)
    fake_tlc_error = (
        "Error: Invariant count >= 0 is violated.\n"
        "The behavior up to this point is:\nState 1: count = -1\n"
    )
    revised = agent3.revise_on_tlc(spec, fake_tlc_error)

    assert isinstance(revised, FormalSpec)
    assert revised.module_name == spec.module_name
    assert revised.variables


# ---------------------------------------------------------------------------
# revise_on_cocotb — simulation-failure revision
# ---------------------------------------------------------------------------

def test_agent3_revise_on_cocotb_returns_valid_spec(require_anthropic, counter_summary):
    """Given a spec and a synthetic cocotb failure log, revise_on_cocotb returns
    a still schema-valid FormalSpec."""
    spec = agent3.generate_formal_spec(counter_summary)
    fake_sim_log = (
        "Error summary: 1 test(s) failed in test_counter\n"
        "Phase: test\n"
        "Failed test vectors:\n"
        '[{"test": "test_counter", "error_type": "AssertionError", '
        '"error_msg": "vector 0: expected count=1, got 0"}]\n'
    )
    revised = agent3.revise_on_cocotb(spec, fake_sim_log)

    assert isinstance(revised, FormalSpec)
    assert revised.module_name == spec.module_name
    assert revised.variables


# ---------------------------------------------------------------------------
# Key guard — this one does NOT need a real key (it tests the guard itself)
# ---------------------------------------------------------------------------

def test_agent3_clear_error_when_key_missing(require_opt_in, monkeypatch):
    """When ANTHROPIC_API_KEY is the placeholder/absent, Agent 3 raises a clear,
    actionable error rather than a cryptic SDK auth failure. Verifies the guard
    even on a machine that DOES have a real key, by overriding the env.

    Gated only by opt-in (no key needed) because it asserts the *absence* path.
    """
    import importlib
    import pipeline.agents.agent3 as a3

    monkeypatch.setenv("ANTHROPIC_API_KEY", a3._PLACEHOLDER_SENTINEL)
    # Reset the cached client so the guard re-evaluates the (now placeholder) key.
    monkeypatch.setattr(a3, "_client", None, raising=False)

    try:
        a3._get_api_key()
    except RuntimeError as exc:
        assert "not configured" in str(exc).lower()
    else:
        raise AssertionError("expected a RuntimeError for the placeholder key")
