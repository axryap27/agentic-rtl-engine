"""
Convergence proof for the Refinement Engine on the 2-bit counter spec.

This test feeds a minimal abstract spec through engine.run() using a
DETERMINISTIC stub pick_rule (no LLM, no randomness) and verifies that:

  1. The engine terminates in a finite number of steps.
  2. The final spec satisfies is_rtl_style().
  3. The refinement_chain.json is written and contains all applied steps.
  4. Replaying the chain from scratch (verifying apply() purity) produces
     the same final spec.
  5. Every rule's apply() is pure (double-call identity check).

If the engine does NOT converge with this stub, the test fails and reports
exactly where it stalls — that is the critical finding.

Run with:
    python3.11 tests/test_refinement_convergence.py
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys

# Ensure the project root is on sys.path when run directly.
_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from pipeline.refinement.engine import (
    MAX_STEPS,
    RULE_REGISTRY,
    RefinementStall,
    _replay_chain,
    _spec_hash,
    is_rtl_style,
    run,
    verify_rule_purity,
)

# 2-bit counter — abstract starting spec
#
# This mirrors what Agent 3 / Compiler 1 would produce before refinement:
# - One abstract variable "count" with no reset and no clocked binding.
# - One abstract action "Count" with no explicit update, no clock, no branches.
# - No reset action yet.

COUNTER_INITIAL_SPEC: dict = {
    "variables": [
        {
            "name": "count",
            "type": "0..3",
            "abstract": True,
            "reset_value": None,
            "clocked": False,
        }
    ],
    "actions": [
        {
            "name": "Count",
            "guard": "TRUE",
            "updates": [],            # no explicit assignments yet
            "is_rtl_style": False,
            "clocked": False,
        }
    ],
    "init": "count = 0",
    "invariants": ["count \\in 0..3"],
    "abstraction_mapping": {},
    "reset_action": None,
    "properties": [],
}


# Deterministic stub pick_rule
#
# Policy: always pick the first rule in the ordered preferred list that is
# both (a) present in the applicable set and (b) not yet been tried for the
# current step index with specific params.  Params are hardcoded per rule
# to drive the counter to RTL-style in a known sequence.
#
# This is intentionally simple — it matches a known good sequence rather
# than trying all permutations.  The goal is to prove the engine CAN
# converge, not to explore the full search space.
#
# Hardcoded sequence for the 2-bit counter:
#   Step 0: Initialization — set reset_value for "count", create Reset action.
#   Step 1: Assignment    — add explicit update count' = (count + 1) mod 4.
#   Step 2: Iteration     — mark "Count" as clocked.
#   (After step 2, count is reset+concrete+typed, Count is clocked+updated
#    -> is_rtl_style() should be True.)

_STEP_SEQUENCE: list[tuple[str, dict]] = [
    (
        "Initialization",
        {
            "reset_values": {"count": "0"},
            "reset_action_name": "Reset",
        },
    ),
    (
        "Assignment",
        {
            "action_name": "Count",
            "updates": [{"variable": "count", "expression": "(count + 1) % 4"}],
        },
    ),
    (
        "Iteration",
        {
            "action_name": "Count",
        },
    ),
]

_step_index: int = 0  # global for the stub (reset before each test)


def _stub_pick_rule(applicable_rules: list[dict], spec: dict) -> dict:
    """
    Deterministic pick_rule stub.

    Walks the hardcoded _STEP_SEQUENCE in order; returns the next entry whose
    rule name appears in the applicable set.  Falls back to the first
    applicable rule with empty params if the sequence is exhausted (should
    not happen for the counter).
    """
    global _step_index
    applicable_names = {r["name"] for r in applicable_rules}

    # Try the hardcoded sequence
    for i in range(_step_index, len(_STEP_SEQUENCE)):
        rule_name, params = _STEP_SEQUENCE[i]
        if rule_name in applicable_names:
            _step_index = i + 1
            return {"rule_name": rule_name, "params": params}

    # Fallback: first applicable rule, no params (engine will likely reject or
    # the rule raises — this surfaces a stall clearly)
    fallback = applicable_rules[0]
    return {"rule_name": fallback["name"], "params": {}}


# Tests

def test_purity_of_all_rules() -> None:
    """Verify every Tier-1 rule's apply() is pure (double-call identity)."""
    print("\n--- Test: apply() purity for all Tier-1 rules ---")

    # Minimal specs that make each rule applicable
    specs_and_params = [
        (
            "Initialization",
            {
                "variables": [{"name": "x", "type": "0..1", "abstract": True,
                                "reset_value": None, "clocked": False}],
                "actions": [],
                "init": "x = 0",
                "invariants": [],
                "abstraction_mapping": {},
                "reset_action": None,
                "properties": [],
            },
            {"reset_values": {"x": "0"}, "reset_action_name": "Rst"},
        ),
        (
            "Assignment",
            {
                "variables": [{"name": "y", "type": "0..1", "abstract": True,
                                "reset_value": None, "clocked": False}],
                "actions": [{"name": "Act", "guard": "TRUE", "updates": [],
                              "is_rtl_style": False, "clocked": False}],
                "init": "y = 0",
                "invariants": [],
                "abstraction_mapping": {},
                "reset_action": None,
                "properties": [],
            },
            {"action_name": "Act", "updates": [{"variable": "y", "expression": "1"}]},
        ),
        (
            "Iteration",
            {
                "variables": [{"name": "z", "type": "0..1", "abstract": False,
                                "reset_value": "0", "clocked": False}],
                "actions": [{"name": "Tick", "guard": "TRUE",
                              "updates": [{"variable": "z", "expression": "1 - z"}],
                              "is_rtl_style": False, "clocked": False}],
                "init": "z = 0",
                "invariants": [],
                "abstraction_mapping": {},
                "reset_action": None,
                "properties": [],
            },
            {"action_name": "Tick"},
        ),
        (
            "Alternation",
            {
                "variables": [{"name": "s", "type": "0..1", "abstract": False,
                                "reset_value": "0", "clocked": False}],
                "actions": [{"name": "Mux", "guard": "TRUE",
                              "updates": [{"variable": "s", "expression": "1"}],
                              "is_rtl_style": False, "clocked": False}],
                "init": "s = 0",
                "invariants": [],
                "abstraction_mapping": {},
                "reset_action": None,
                "properties": [],
            },
            {
                "action_name": "Mux",
                "branches": [
                    {"guard": "s = 0", "updates": [{"variable": "s", "expression": "1"}]},
                    {"guard": "s = 1", "updates": [{"variable": "s", "expression": "0"}]},
                ],
            },
        ),
        (
            "SequentialComposition",
            {
                "variables": [{"name": "a", "type": "0..1", "abstract": False,
                                "reset_value": "0", "clocked": False}],
                "actions": [{"name": "Seq", "guard": "TRUE",
                              "updates": [{"variable": "a", "expression": "1"}],
                              "is_rtl_style": False, "clocked": False}],
                "init": "a = 0",
                "invariants": [],
                "abstraction_mapping": {},
                "reset_action": None,
                "properties": [],
            },
            {
                "action_name": "Seq",
                "steps": [
                    {"name": "s1", "guard": "TRUE",
                     "updates": [{"variable": "a", "expression": "1"}]},
                ],
            },
        ),
        (
            "IntroduceVariable",
            {
                "variables": [],
                "actions": [],
                "init": "",
                "invariants": [],
                "abstraction_mapping": {},
                "reset_action": None,
                "properties": [],
            },
            {"name": "new_reg", "type": "0..7", "abstract": False, "reset_value": "0"},
        ),
    ]

    rule_by_name = {r.__class__.__name__: r for r in RULE_REGISTRY}
    for rule_name, spec, params in specs_and_params:
        rule = rule_by_name[rule_name]
        verify_rule_purity(rule, spec, params)
        print(f"  [PASS] {rule_name}.apply() is pure")

    print("All purity checks passed.")


def test_convergence_counter() -> None:
    """
    Run the 2-bit counter through engine.run() with the deterministic stub.

    Verifies:
    - Terminates (no RefinementStall raised).
    - Final spec satisfies is_rtl_style().
    - refinement_chain.json is written with >= 1 step.
    - Replay of chain from initial spec produces the same final spec (purity).
    """
    global _step_index
    _step_index = 0  # reset stub state

    run_id = "test_counter_convergence"
    chain_path = pathlib.Path("artifacts") / run_id / "refinement_chain.json"

    # Clean up any prior run artifact
    if chain_path.exists():
        chain_path.unlink()

    print("\n--- Test: 2-bit counter convergence ---")
    print("Initial spec:")
    print(f"  variables: {[v['name'] for v in COUNTER_INITIAL_SPEC['variables']]}")
    print(f"  actions:   {[a['name'] for a in COUNTER_INITIAL_SPEC['actions']]}")
    print(f"  is_rtl_style (initial): {is_rtl_style(COUNTER_INITIAL_SPEC)}")
    print()

    try:
        final_spec = run(
            formal_spec=copy.deepcopy(COUNTER_INITIAL_SPEC),
            pick_rule=_stub_pick_rule,
            run_id=run_id,
        )
    except RefinementStall as exc:
        print(f"\n[FAIL] Engine stalled: {exc}")
        print()
        print("CRITICAL FINDING: The stub pick_rule sequence does not converge.")
        print("Examine _STEP_SEQUENCE in this file to diagnose the gap.")
        sys.exit(1)

    # --- Assertion 1: is_rtl_style ---
    assert is_rtl_style(final_spec), (
        f"Engine returned a spec that is NOT RTL-style:\n{json.dumps(final_spec, indent=2)}"
    )
    print("[PASS] Final spec satisfies is_rtl_style()")

    # --- Assertion 2: chain file written ---
    assert chain_path.exists(), f"refinement_chain.json not written to {chain_path}"
    with chain_path.open() as f:
        chain = json.load(f)
    assert len(chain) >= 1, "refinement_chain.json is empty"
    print(f"[PASS] refinement_chain.json written with {len(chain)} step(s)")

    # --- Print chain summary ---
    print("\nRefinement chain:")
    for step in chain:
        params_summary = json.dumps(step["params"])
        if len(params_summary) > 80:
            params_summary = params_summary[:77] + "..."
        print(f"  Step {step['step']}: {step['rule_name']}  params={params_summary}")
        print(f"           pre={step['pre_hash']} -> post={step['post_hash']}")

    # --- Assertion 3: replay purity ---
    print("\nReplaying chain from scratch to verify apply() purity...")
    replayed_spec = _replay_chain(copy.deepcopy(COUNTER_INITIAL_SPEC), chain)
    replayed_hash = _spec_hash(replayed_spec)
    final_hash = _spec_hash(final_spec)
    assert replayed_hash == final_hash, (
        f"Replay produced a different spec!\n"
        f"  Engine final hash:  {final_hash}\n"
        f"  Replay final hash:  {replayed_hash}\n"
        f"  Engine final spec:  {json.dumps(final_spec, indent=2)}\n"
        f"  Replayed spec:      {json.dumps(replayed_spec, indent=2)}"
    )
    print(f"[PASS] Replay hash matches engine output ({final_hash})")

    # --- Print final spec summary ---
    print("\nFinal RTL-style spec summary:")
    for var in final_spec["variables"]:
        print(
            f"  var {var['name']}: type={var['type']} "
            f"abstract={var['abstract']} reset={var['reset_value']} "
            f"clocked={var['clocked']}"
        )
    for action in final_spec["actions"]:
        updates = [f"{u['variable']}={u['expression']}" for u in action.get("updates", [])]
        print(
            f"  action {action['name']}: clocked={action['clocked']} "
            f"updates={updates}"
        )
    print(f"  reset_action: {final_spec['reset_action']}")
    print()
    print("CONVERGENCE DEMONSTRATED: engine.run() reached RTL-style in finite steps.")


def test_is_rtl_style_predicate() -> None:
    """Unit test the is_rtl_style predicate on known cases."""
    print("\n--- Test: is_rtl_style predicate ---")

    # Should be False — no reset action
    assert not is_rtl_style({
        "variables": [{"name": "x", "type": "0..1", "abstract": False,
                        "reset_value": "0", "clocked": True}],
        "actions": [{"name": "Act", "guard": "TRUE",
                     "updates": [{"variable": "x", "expression": "1"}],
                     "clocked": True}],
        "reset_action": None,
    }), "Expected False: no reset_action"
    print("  [PASS] No reset_action -> False")

    # Should be False — abstract variable
    assert not is_rtl_style({
        "variables": [{"name": "x", "type": "0..1", "abstract": True,
                        "reset_value": "0", "clocked": True}],
        "actions": [{"name": "Act", "guard": "TRUE",
                     "updates": [{"variable": "x", "expression": "1"}],
                     "clocked": True}],
        "reset_action": "Rst",
    }), "Expected False: abstract variable"
    print("  [PASS] Abstract variable -> False")

    # Should be False — non-reset action not clocked
    assert not is_rtl_style({
        "variables": [{"name": "x", "type": "0..1", "abstract": False,
                        "reset_value": "0", "clocked": False}],
        "actions": [{"name": "Act", "guard": "TRUE",
                     "updates": [{"variable": "x", "expression": "1"}],
                     "clocked": False}],
        "reset_action": "Rst",
    }), "Expected False: non-reset action not clocked"
    print("  [PASS] Unclocked non-reset action -> False")

    # Should be True — all conditions met
    assert is_rtl_style({
        "variables": [{"name": "x", "type": "0..1", "abstract": False,
                        "reset_value": "0", "clocked": True}],
        "actions": [
            {"name": "Rst", "guard": "rst = TRUE",
             "updates": [{"variable": "x", "expression": "0"}],
             "clocked": False},
            {"name": "Act", "guard": "TRUE",
             "updates": [{"variable": "x", "expression": "1 - x"}],
             "clocked": True},
        ],
        "reset_action": "Rst",
    }), "Expected True: all RTL-style conditions met"
    print("  [PASS] All conditions met -> True")

    print("is_rtl_style predicate tests passed.")


# Entry point

if __name__ == "__main__":
    print("=" * 60)
    print("Refinement Engine Convergence Test")
    print("=" * 60)

    test_is_rtl_style_predicate()
    test_purity_of_all_rules()
    test_convergence_counter()

    print()
    print("=" * 60)
    print("All tests PASSED.")
    print("=" * 60)
