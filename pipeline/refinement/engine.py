"""
Refinement Engine — deterministic loop driver.

Responsibilities
----------------
1. Hold the current spec in-memory during the run.
2. Filter applicable rules: [r for r in registry if r.is_applicable(spec)].
3. Call pick_rule (injected — Agent 3's one-shot structured call) with the
   filtered list and current spec.
4. Validate pick_rule's return: rule in applicable set, params type-consistent.
5. Apply: new_spec = rule.apply(spec, params).
6. Append (rule_name, params) to refinement_chain.json.
7. Detect RTL-style termination via is_rtl_style().
8. Backtrack on stall: roll back N steps, mark choice excluded, re-prompt.

Purity invariant
----------------
rule.apply() is pure — same (spec, params) always produces the same output.
Backtracking replays the chain from scratch instead of undoing mutations.

No LLM calls here. No imports of openai / anthropic / any SDK.
"""

from __future__ import annotations

import copy
import hashlib
import json
import pathlib
from typing import Callable

from .rules import TIER1_RULES
from .rules.base import RefinementRule

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Maximum number of sequential backtrack steps before giving up.
MAX_BACKTRACK_DEPTH: int = 5

#: Maximum total rule-application steps (guards against infinite loops on
#: pathological pick_rule callables).
MAX_STEPS: int = 200

#: Registry of all rule instances used by the engine.
RULE_REGISTRY: list[RefinementRule] = TIER1_RULES


# ---------------------------------------------------------------------------
# RTL-style termination predicate
# ---------------------------------------------------------------------------

def is_rtl_style(spec: dict) -> bool:
    """
    Return True when the spec has been refined to RTL-style.

    A spec is RTL-style when Compiler 2 can derive the three named sections
    it requires — VARIABLES, CombinationalLogic, UpdatePipeline — directly
    from it.  Concretely:

    1. Every variable is concrete (abstract == False).
    2. Every variable has a concrete bounded type (not None / empty).
    3. Every variable has an explicit reset_value.
    4. There is an explicit reset_action named in spec["reset_action"].
    5. Every non-reset action is clocked (action["clocked"] == True).
    6. Every non-reset action has at least one explicit update
       (action["updates"] is non-empty).

    Rules 5 & 6 together guarantee that Compiler 2 can emit an
    always @(posedge clk) block from UpdatePipeline conjuncts.
    Rule 3 guarantees the reset branch of that block is populated.
    Rules 1 & 2 guarantee every variable can become a Verilog signal.

    Note: a spec with zero variables or zero non-reset actions is NOT
    RTL-style — something must have been added first.
    """
    variables = spec.get("variables", [])
    actions = spec.get("actions", [])
    reset_action = spec.get("reset_action")

    if not variables:
        return False
    if not reset_action:
        return False

    non_reset_actions = [a for a in actions if a["name"] != reset_action]
    if not non_reset_actions:
        return False

    for var in variables:
        if var.get("abstract", True):
            return False
        if not var.get("type"):
            return False
        if var.get("reset_value") is None:
            return False

    for action in non_reset_actions:
        if not action.get("clocked", False):
            return False
        if not action.get("updates"):
            return False

    return True


# ---------------------------------------------------------------------------
# Spec hashing (for chain replay verification)
# ---------------------------------------------------------------------------

def _spec_hash(spec: dict) -> str:
    """Stable SHA-256 of a spec dict, used as a lightweight snapshot ID."""
    serialized = json.dumps(spec, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(serialized.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Chain persistence
# ---------------------------------------------------------------------------

def _artifact_path(run_id: str) -> pathlib.Path:
    """Return the path to refinement_chain.json for this run."""
    base = pathlib.Path("artifacts") / run_id
    base.mkdir(parents=True, exist_ok=True)
    return base / "refinement_chain.json"


def _load_chain(run_id: str) -> list[dict]:
    path = _artifact_path(run_id)
    if path.exists():
        with path.open() as f:
            return json.load(f)
    return []


def _save_chain(run_id: str, chain: list[dict]) -> None:
    path = _artifact_path(run_id)
    with path.open("w") as f:
        json.dump(chain, f, indent=2)


# ---------------------------------------------------------------------------
# Chain replay (backtracking substrate)
# ---------------------------------------------------------------------------

def _replay_chain(initial_spec: dict, chain: list[dict]) -> dict:
    """
    Replay a list of chain steps from initial_spec.

    Each step is {"rule_name": str, "params": dict}.
    Returns the spec that results from applying all steps in order.

    Relies on apply() purity: same (spec, params) -> same result every time.
    """
    rule_by_name: dict[str, RefinementRule] = {
        r.__class__.__name__: r for r in RULE_REGISTRY
    }
    spec = copy.deepcopy(initial_spec)
    for step in chain:
        rule_name = step["rule_name"]
        params = step["params"]
        rule = rule_by_name.get(rule_name)
        if rule is None:
            raise ValueError(
                f"Replay failed: unknown rule '{rule_name}'. "
                f"Known rules: {list(rule_by_name)}"
            )
        spec = rule.apply(spec, params)
    return spec


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_pick(
    choice: dict,
    applicable_names: list[str],
    excluded: set[tuple[str, str]],
) -> str | None:
    """
    Validate the dict returned by pick_rule.

    Returns None if valid, or an error message string if not.
    """
    if not isinstance(choice, dict):
        return f"pick_rule must return a dict, got {type(choice).__name__}"

    rule_name = choice.get("rule_name")
    params = choice.get("params")

    if not isinstance(rule_name, str) or not rule_name:
        return "pick_rule return missing 'rule_name' (non-empty str)"
    if not isinstance(params, dict):
        return "pick_rule return missing 'params' (dict)"

    if rule_name not in applicable_names:
        return (
            f"rule_name '{rule_name}' is not in the applicable set "
            f"{applicable_names}"
        )

    params_json = json.dumps(params, sort_keys=True)
    if (rule_name, params_json) in excluded:
        return (
            f"choice ({rule_name}, {params_json}) was already tried and "
            f"excluded at this step — pick a different rule or params"
        )

    # Reject overly elaborate params (TLA+ text smuggled in params)
    for key, val in params.items():
        if isinstance(val, str) and len(val) > 2000:
            return (
                f"params['{key}'] is suspiciously long ({len(val)} chars). "
                f"params must be structured data, not raw TLA+ text."
            )

    return None


# ---------------------------------------------------------------------------
# Main engine entry point
# ---------------------------------------------------------------------------

class RefinementStall(Exception):
    """Raised when the engine cannot make progress and has exhausted backtracking."""


def run(
    formal_spec: dict,
    pick_rule: Callable[[list[dict], dict], dict],
    *,
    run_id: str = "default",
) -> dict:
    """
    Drive the refinement loop until the spec reaches RTL-style.

    Parameters
    ----------
    formal_spec:
        The starting spec dict (engine-internal format described in base.py).
        Typically derived from FormalSpec.model_dump() after translation into
        the engine's variable/action shape.
    pick_rule:
        Injected callable — Agent 3's one-shot pick_rule function.
        Signature: pick_rule(applicable_rules: list[dict], spec: dict) -> dict
        Each entry in applicable_rules is {"name": str, "describe": str}.
        Returns {"rule_name": str, "params": dict}.
        The engine never calls an LLM itself.
    run_id:
        Identifies the artifact directory (artifacts/<run_id>/). Defaults to
        "default" for testing.

    Returns
    -------
    The RTL-style spec dict.

    Raises
    ------
    RefinementStall
        When all backtracking depth has been exhausted without reaching
        RTL-style. Contains a human-readable explanation.

    Notes
    -----
    - Backtracking policy: roll back 1 step first; if that exhausts choices at
      that depth, roll back further. Cap at MAX_BACKTRACK_DEPTH total rollback
      depth. excluded_choices persist across rollbacks within a run.
    - The chain written to disk contains pre/post hashes for each step so it
      can be replayed deterministically.
    """
    rule_by_name: dict[str, RefinementRule] = {
        r.__class__.__name__: r for r in RULE_REGISTRY
    }

    # committed chain: list of {"step", "rule_name", "params", "pre_hash", "post_hash"}
    chain: list[dict] = []
    # excluded set keyed by chain depth: excluded_at[depth] = set of (rule_name, params_json)
    excluded_at: dict[int, set[tuple[str, str]]] = {}
    # Current spec
    spec = copy.deepcopy(formal_spec)

    total_steps = 0

    while not is_rtl_style(spec):
        if total_steps >= MAX_STEPS:
            raise RefinementStall(
                f"Refinement exceeded {MAX_STEPS} steps without reaching "
                f"RTL-style. This likely indicates a pick_rule that cycles. "
                f"Chain length: {len(chain)}."
            )
        total_steps += 1

        # --- Filter applicable rules ---
        applicable: list[RefinementRule] = [
            r for r in RULE_REGISTRY if r.is_applicable(spec)
        ]

        depth = len(chain)
        excluded_here: set[tuple[str, str]] = excluded_at.get(depth, set())

        if not applicable:
            # No rule can fire AND not RTL-style -> must backtrack
            spec, chain = _backtrack(
                formal_spec, chain, excluded_at, MAX_BACKTRACK_DEPTH
            )
            continue

        # Build the list sent to pick_rule
        applicable_descs = [
            {"name": r.__class__.__name__, "describe": r.describe()}
            for r in applicable
        ]

        # --- Invoke pick_rule (the injected callable, NOT an LLM call here) ---
        choice = pick_rule(applicable_descs, spec)

        # --- Validate pick_rule's return ---
        applicable_names = [r.__class__.__name__ for r in applicable]
        error = _validate_pick(choice, applicable_names, excluded_here)
        if error:
            # Invalid return from pick_rule — count invalid responses at this
            # depth; after 3, backtrack.
            excluded_at.setdefault(depth, set()).add(
                ("__invalid__", json.dumps({"error": error[:120]}, sort_keys=True))
            )
            invalid_count = sum(
                1 for name, _ in excluded_at.get(depth, set())
                if name == "__invalid__"
            )
            if invalid_count >= 3:
                spec, chain = _backtrack(
                    formal_spec, chain, excluded_at, MAX_BACKTRACK_DEPTH
                )
            continue

        rule_name: str = choice["rule_name"]
        params: dict = choice["params"]
        params_json = json.dumps(params, sort_keys=True)

        # Double-check exclusion (pick_rule may not have honoured the contract)
        if (rule_name, params_json) in excluded_here:
            excluded_at.setdefault(depth, set()).add((rule_name, params_json))
            continue

        rule = rule_by_name[rule_name]
        pre_hash = _spec_hash(spec)

        # --- Apply the rule (pure — deterministic) ---
        try:
            new_spec = rule.apply(spec, params)
        except (ValueError, KeyError, TypeError) as exc:
            # Rule raised on these params -> exclude this choice
            excluded_at.setdefault(depth, set()).add((rule_name, params_json))
            continue

        post_hash = _spec_hash(new_spec)

        # --- Append to chain and persist ---
        step = {
            "step": depth,
            "rule_name": rule_name,
            "params": params,
            "pre_hash": pre_hash,
            "post_hash": post_hash,
        }
        chain.append(step)
        _save_chain(run_id, chain)

        spec = new_spec

    return spec


# ---------------------------------------------------------------------------
# Backtracking helper
# ---------------------------------------------------------------------------

def _backtrack(
    initial_spec: dict,
    chain: list[dict],
    excluded_at: dict[int, set[tuple[str, str]]],
    max_depth: int,
) -> tuple[dict, list[dict]]:
    """
    Roll back the chain by one step, marking the last choice as excluded.

    If the rolled-back depth is also exhausted, keeps rolling back further
    until a non-exhausted depth is found or max_depth is exceeded.

    Returns (spec_at_rollback_point, truncated_chain).

    Raises RefinementStall if we cannot roll back further.
    """
    rollback_count = 0

    while rollback_count < max_depth:
        if not chain:
            raise RefinementStall(
                "Backtracking exhausted — chain is empty and the spec is not "
                "RTL-style. The initial spec may be too abstract for the "
                f"current rule set to reach RTL-style. "
                f"Excluded choices per depth: {dict(excluded_at)}"
            )

        # Mark the last step as excluded at its depth
        last_step = chain[-1]
        depth_of_last = last_step["step"]
        params_json = json.dumps(last_step["params"], sort_keys=True)
        excluded_at.setdefault(depth_of_last, set()).add(
            (last_step["rule_name"], params_json)
        )

        # Pop the last step
        chain = chain[:-1]
        rollback_count += 1

        # Replay from scratch to the new chain tip
        spec = _replay_chain(initial_spec, chain)

        # Check if there are still applicable non-excluded rules at this depth
        applicable: list[RefinementRule] = [
            r for r in RULE_REGISTRY if r.is_applicable(spec)
        ]
        if not applicable:
            # Nothing applicable even here — keep rolling back
            continue

        current_depth = len(chain)
        excluded_here = excluded_at.get(current_depth, set())
        applicable_names = {r.__class__.__name__ for r in applicable}
        excluded_rule_names = {name for name, _ in excluded_here}
        remaining = applicable_names - excluded_rule_names
        if remaining:
            return spec, chain

        # All applicable rules at this depth are excluded — keep rolling back

    raise RefinementStall(
        f"Backtracking exceeded maximum depth ({max_depth}). "
        f"The engine could not find a path to RTL-style. "
        f"Chain at failure: {[s['rule_name'] for s in chain]}. "
        f"Excluded per depth: {dict(excluded_at)}"
    )


# ---------------------------------------------------------------------------
# Purity self-check (called by tests / QA gate)
# ---------------------------------------------------------------------------

def verify_rule_purity(rule: RefinementRule, spec: dict, params: dict) -> bool:
    """
    Assert that rule.apply() is pure: calling it twice on a deepcopy of the
    same (spec, params) produces equal results.

    Returns True. Raises AssertionError on violation.
    """
    result_a = rule.apply(copy.deepcopy(spec), copy.deepcopy(params))
    result_b = rule.apply(copy.deepcopy(spec), copy.deepcopy(params))
    assert result_a == result_b, (
        f"Purity violation in {rule.__class__.__name__}.apply(): "
        f"two calls on identical inputs produced different outputs.\n"
        f"First:  {result_a}\n"
        f"Second: {result_b}"
    )
    return True
