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
from .bridge import _is_identity_hold

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
    5. Every non-reset action is clocked (action["clocked"] == True),
       EXCEPT a pure register-hold (every update is ``v' = v``) which emits
       nothing distinct and so need not be separately clocked.
    6. Every non-reset action has at least one explicit update
       (action["updates"] is non-empty).

    Rules 5 & 6 together guarantee that Compiler 2 can emit an
    always @(posedge clk) block from UpdatePipeline conjuncts.
    Rule 3 guarantees the reset branch of that block is populated.
    Rules 1 & 2 guarantee every variable can become a Verilog signal.

    The rule-5 carve-out for identity holds is what keeps convergence off the
    Rule Picker's critical path: a spec with a dedicated Hold/idle transition
    (e.g. an enable-gated register) is RTL-style once the *real* actions are
    clocked, without the picker having to also iterate the redundant Hold.

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
        # A memory array (depth set) is a register file / RAM. Synthesis-canonical
        # memories carry NO reset — an all-element reset does not infer block RAM
        # and is rarely intended — so a memory is concrete once it is in a clock
        # domain (Iteration sets abstract=False) and needs no reset_value. The
        # reverse bridge likewise emits no reset conjunct for it. (Mirrors the
        # rule-5 identity-hold carve-out below: a structural exception, not a relax
        # of the invariant for ordinary registers, which still must reset.)
        if var.get("depth"):
            continue
        if var.get("reset_value") is None:
            return False

    for action in non_reset_actions:
        # A pure register-hold / idle action (every update is `v' = v`) emits
        # nothing distinct — the register holds via the ELSE branch of its clocked
        # driver — so it need not be separately clocked. Requiring it to be clocked
        # was the root of a refinement stall: the engine could only reach RTL-style
        # if the picker happened to Iterate a redundant Hold action. The bridge
        # likewise drops such actions from CombinationalLogic (no double-drive).
        if _is_identity_hold(action):
            continue
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


def _renumber_steps(chain: list[dict]) -> list[dict]:
    """Return *chain* with globally-monotonic ``step`` indices (0..n-1).

    Replay (``_replay_chain``) ignores the ``step`` field entirely, so this is
    purely cosmetic for on-disk readability and ``/trace-refinement``. Used when
    concatenating a same-run_id committed prefix with a new pass's steps so the
    persisted chain reads as one continuous sequence (G13).
    """
    return [{**step, "step": i} for i, step in enumerate(chain)]


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
    tlc_check: Callable[[dict], bool] | None = None,
    allowed_rule_names: set[str] | None = None,
    termination_check: Callable[[dict], bool] = is_rtl_style,
    max_steps: int = MAX_STEPS,
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
    tlc_check:
        Optional callable invoked after each rule application. Receives the
        candidate new spec and returns True if TLC accepts it. If False, the
        choice is excluded and the engine backtracks. Permissive on errors
        (exceptions inside the callback should return True to avoid blocking).
    allowed_rule_names:
        Optional set of rule class names to consider. Rules not in this set are
        filtered out before pick_rule is called. None means all rules allowed.
    termination_check:
        Callable that returns True when this engine pass is complete. Defaults
        to is_rtl_style (global RTL-style termination). Per-pass termination
        (e.g. "no allowed rules applicable") should be passed here.
    max_steps:
        Hard limit on rule-application steps. Raises RefinementStall if hit.

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

    # G13: Stage 3 calls run() once per structured pass with the SAME run_id,
    # threading the refined spec in memory between calls. The on-disk chain must
    # ACCUMULATE across those passes so that _replay_chain(initial_abstract_spec,
    # on_disk_chain) reconstructs the FINAL multi-pass spec — not just the last
    # pass. We load whatever prior passes already committed and treat it as an
    # immutable prefix: this pass appends after it and never backtracks into it.
    #
    # Correctness of concatenation: each pass starts from the previous pass's
    # output spec (passed in as `formal_spec`), which by purity equals
    # replay(initial, committed_prefix). So replay(initial, prefix + this_pass)
    # == apply(this_pass) on `formal_spec`. The prefix steps are relative to the
    # initial spec; this pass's steps are relative to `formal_spec`; appended in
    # order they replay correctly from the initial spec.
    committed_prefix: list[dict] = _load_chain(run_id)

    # committed chain for THIS pass: list of
    # {"step", "rule_name", "params", "pre_hash", "post_hash"}.
    # Backtracking operates only over this pass's steps (replayed from
    # `formal_spec`); the prefix is never rolled back.
    chain: list[dict] = []
    # excluded set keyed by chain depth: excluded_at[depth] = set of (rule_name, params_json)
    excluded_at: dict[int, set[tuple[str, str]]] = {}
    # Per-depth count of FAILED pick attempts (an invalid pick_rule return, or a
    # re-pick of an already-excluded choice). A plain INTEGER counter — NOT a set
    # keyed on the rejection text — so that a picker which fails IDENTICALLY every
    # call (the most common LLM stall: re-emitting the same bad rule name) still
    # reaches the strike threshold and backtracks, instead of deduping to one
    # entry and spinning to MAX_STEPS (D3). Re-picking an excluded choice is also
    # counted, so a pure-function-of-spec picker that keeps returning the same
    # now-excluded choice backtracks rather than looping (D4). Reset on a
    # successful commit at a depth so a later revisit gets fresh strikes.
    invalid_counts: dict[int, int] = {}
    # Current spec
    spec = copy.deepcopy(formal_spec)

    total_steps = 0

    while not termination_check(spec):
        if total_steps >= max_steps:
            # Report the ACTUAL cap (the max_steps PARAMETER), not the module
            # default — Stage 3 passes a small per-pass cap, so printing the 200
            # default here misattributed every capped stall as a 200-step blow-up.
            # Include the rules still firing (but never terminating) and the tail
            # of the chain so a stall is debuggable from the artifact alone,
            # without a re-run.
            applicable_now = [
                r.__class__.__name__ for r in RULE_REGISTRY
                if r.is_applicable(spec)
                and (allowed_rule_names is None
                     or r.__class__.__name__ in allowed_rule_names)
            ]
            raise RefinementStall(
                f"Refinement exceeded {max_steps} steps without reaching "
                f"RTL-style. This likely indicates a pick_rule that cycles. "
                f"Chain length: {len(chain)}. "
                f"Rules still applicable at stall: {applicable_now}. "
                f"Last picks: {[s['rule_name'] for s in chain[-8:]]}."
            )
        total_steps += 1

        # --- Filter applicable rules (by is_applicable and allowed set) ---
        applicable: list[RefinementRule] = [
            r for r in RULE_REGISTRY
            if r.is_applicable(spec)
            and (allowed_rule_names is None or r.__class__.__name__ in allowed_rule_names)
        ]

        depth = len(chain)
        excluded_here: set[tuple[str, str]] = excluded_at.get(depth, set())

        if not applicable:
            # No allowed rule can fire. If termination_check now agrees, exit
            # cleanly rather than backtracking (this pass is done).
            if termination_check(spec):
                break
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
        # A pick_rule FAILURE (the LLM returns unparseable or non-pick JSON, a
        # transport error, a max_tokens truncation, ...) must NEVER abort the
        # whole refinement run. Treat it exactly like an invalid return: strike
        # at this depth and backtrack after 3 (the same D3/D4 policy, extended
        # to the picker THROWING rather than returning a bad dict). Without this,
        # one bad Agent-3 response on any pass kills the entire pipeline — the
        # 2-bit-counter handshake-pass crash that produced a 'partial' artifact.
        try:
            choice = pick_rule(applicable_descs, spec)
        except Exception:
            invalid_counts[depth] = invalid_counts.get(depth, 0) + 1
            if invalid_counts[depth] >= 3:
                spec, chain = _backtrack(
                    formal_spec, chain, excluded_at, MAX_BACKTRACK_DEPTH
                )
            continue

        # --- Validate pick_rule's return ---
        applicable_names = [r.__class__.__name__ for r in applicable]
        error = _validate_pick(choice, applicable_names, excluded_here)
        if error:
            # Invalid return from pick_rule — count this strike at the current
            # depth (integer counter, not a set keyed on error text — D3); after
            # 3 strikes, backtrack.
            invalid_counts[depth] = invalid_counts.get(depth, 0) + 1
            if invalid_counts[depth] >= 3:
                spec, chain = _backtrack(
                    formal_spec, chain, excluded_at, MAX_BACKTRACK_DEPTH
                )
            continue

        rule_name: str = choice["rule_name"]
        params: dict = choice["params"]
        params_json = json.dumps(params, sort_keys=True)

        # Double-check exclusion (pick_rule may not have honoured the contract).
        # A picker that is a pure function of the spec keeps returning the same
        # now-excluded choice forever; count each re-pick as a strike so the
        # 3-strike backtrack fires instead of spinning to MAX_STEPS (D4).
        if (rule_name, params_json) in excluded_here:
            excluded_at.setdefault(depth, set()).add((rule_name, params_json))
            invalid_counts[depth] = invalid_counts.get(depth, 0) + 1
            if invalid_counts[depth] >= 3:
                spec, chain = _backtrack(
                    formal_spec, chain, excluded_at, MAX_BACKTRACK_DEPTH
                )
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

        # --- No-op guard: an application that does not change the spec made no
        # progress toward RTL-style. Committing it lets a picker that keeps
        # choosing an already-satisfied rule (e.g. Iteration on an
        # already-clocked action — now idempotent) spin: it would append
        # identical no-op steps until max_steps. Treat a no-op like an invalid
        # pick: exclude this exact choice here and strike; backtrack after 3 so
        # the picker is forced toward a rule/params that actually advance.
        if post_hash == pre_hash:
            excluded_at.setdefault(depth, set()).add((rule_name, params_json))
            invalid_counts[depth] = invalid_counts.get(depth, 0) + 1
            if invalid_counts[depth] >= 3:
                spec, chain = _backtrack(
                    formal_spec, chain, excluded_at, MAX_BACKTRACK_DEPTH
                )
            continue

        # --- TLC gate: verify the candidate spec before committing ---
        if tlc_check is not None and not tlc_check(new_spec):
            excluded_at.setdefault(depth, set()).add((rule_name, params_json))
            continue

        # --- Append to chain and persist ---
        step = {
            "step": depth,
            "rule_name": rule_name,
            "params": params,
            "pre_hash": pre_hash,
            "post_hash": post_hash,
        }
        chain.append(step)
        # This depth is resolved — clear its strike count so a later backtrack
        # that revisits it gets a fresh 3-strike budget (D3/D4).
        invalid_counts.pop(depth, None)
        # Persist the accumulated chain (prior passes + this pass), renumbered
        # to a single monotonic sequence for on-disk readability (G13).
        _save_chain(run_id, _renumber_steps(committed_prefix + chain))

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
