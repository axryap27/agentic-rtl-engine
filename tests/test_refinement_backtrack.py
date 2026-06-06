"""
Backtracking, stall, and multi-pass chain-accumulation tests for the
Refinement Engine (deterministic — no LLM, no randomness).

Covers the deterministic half of audit gap G08 and gap G13:

  G08 (deterministic half)
  ------------------------
  - A deliberately-STALLING scripted pick_rule drives engine.run() into the
    backtrack path; we assert `_backtrack` is exercised, the `__invalid__`
    3-strike accounting fires, MAX_BACKTRACK_DEPTH is honoured, and
    RefinementStall is raised once the search is exhausted.
  - `_backtrack` itself is unit-tested white-box (pop + exclude + replay +
    remaining-rule check, and exhaustion past max_depth).
  - The MAX_STEPS guard raises RefinementStall on a non-converging picker.

  G13 (multi-pass chain accumulation)
  -----------------------------------
  - Stage 3 calls engine.run() repeatedly with the SAME run_id across
    different `allowed_rule_names` subsets, threading the refined spec in
    memory. We assert the on-disk refinement_chain.json ACCUMULATES every
    pass (not overwritten to the last) and that
    `_replay_chain(initial_abstract_spec, on_disk_chain)` reconstructs the
    FINAL multi-pass spec — the regression for the overwrite bug.

Pickers here are applicability-aware and (where shared across passes)
idempotent functions of the spec, never module-global counters. The one
varying picker (recover-after-backtrack) holds its state in an instance, not
a module global, and is used within a single run() only.

Run with:
    python3.11 -m pytest tests/test_refinement_backtrack.py -q
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys

import pytest

# Ensure the project root is on sys.path when run directly / under pytest.
_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from pipeline.refinement.engine import (  # noqa: E402
    MAX_BACKTRACK_DEPTH,
    RULE_REGISTRY,
    RefinementStall,
    _backtrack,
    _load_chain,
    _replay_chain,
    _spec_hash,
    is_rtl_style,
    run,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _counter_initial_spec() -> dict:
    """Fresh abstract 2-bit counter spec (mirrors the convergence fixture)."""
    return {
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
                "updates": [],
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


# The known-good 3-step sequence that drives the counter to RTL-style.
_GOOD_SEQUENCE: list[tuple[str, dict]] = [
    (
        "Initialization",
        {"reset_values": {"count": "0"}, "reset_action_name": "Reset"},
    ),
    (
        "Assignment",
        {
            "action_name": "Count",
            "updates": [{"variable": "count", "expression": "(count + 1) % 4"}],
        },
    ),
    ("Iteration", {"action_name": "Count"}),
]


def _good_pick_rule(applicable_rules: list[dict], spec: dict) -> dict:
    """
    Applicability-driven, idempotent (pure function of spec) picker.

    Returns the first entry of _GOOD_SEQUENCE whose preconditions on `spec`
    are not yet satisfied and whose rule is applicable. Because the engine's
    spec strictly advances toward RTL-style with this sequence, the same spec
    always maps to the same choice — no cursor, no module global, safe to share
    across run() calls.
    """
    names = {r["name"] for r in applicable_rules}

    # Stage 1: install reset (Initialization) — needed while any var unreset
    # or no reset action exists.
    needs_init = (
        spec.get("reset_action") is None
        or any(v.get("reset_value") is None for v in spec.get("variables", []))
    )
    if needs_init and "Initialization" in names:
        return {"rule_name": _GOOD_SEQUENCE[0][0], "params": _GOOD_SEQUENCE[0][1]}

    # Stage 2: give Count an explicit update (Assignment) while it has none.
    count = next((a for a in spec.get("actions", []) if a["name"] == "Count"), None)
    if count is not None and not count.get("updates") and "Assignment" in names:
        return {"rule_name": _GOOD_SEQUENCE[1][0], "params": _GOOD_SEQUENCE[1][1]}

    # Stage 3: clock Count (Iteration) while it is not yet clocked.
    if count is not None and not count.get("clocked") and "Iteration" in names:
        return {"rule_name": _GOOD_SEQUENCE[2][0], "params": _GOOD_SEQUENCE[2][1]}

    # Fallback: first applicable, empty params (surfaces an unexpected stall).
    return {"rule_name": applicable_rules[0]["name"], "params": {}}


# ---------------------------------------------------------------------------
# G08 — stall via always-invalid picker (3-strike → empty-chain backtrack)
# ---------------------------------------------------------------------------

def test_invalid_pick_strike_accounting(monkeypatch):
    """
    The 3-strike accounting calls _backtrack once a depth accumulates three
    DISTINCT `__invalid__` entries. We spy on _backtrack and assert it is
    reached only after the third invalid pick.

    NOTE: the strikes must be DISTINCT (different error text). The engine
    stores `__invalid__` markers in a *set* keyed on the rejection message, so
    three IDENTICAL invalid responses collapse to one entry and never reach the
    3-strike threshold — see test_identical_invalid_picks_should_backtrack
    (xfail) for that real bug. Here we vary the bad rule name each call so the
    error text (and therefore the set entry) differs.
    """
    import pipeline.refinement.engine as engine_mod

    calls = {"pick": 0, "backtrack": 0}

    def distinct_invalid(applicable_rules, spec):
        calls["pick"] += 1
        # Vary the bad rule name -> varying rejection text -> distinct strikes.
        return {"rule_name": f"NoSuchRule{calls['pick']}", "params": {}}

    real_backtrack = engine_mod._backtrack

    def spy_backtrack(*args, **kwargs):
        calls["backtrack"] += 1
        # Delegate to the real implementation (which raises on empty chain).
        return real_backtrack(*args, **kwargs)

    monkeypatch.setattr(engine_mod, "_backtrack", spy_backtrack)

    with pytest.raises(RefinementStall) as excinfo:
        run(
            formal_spec=_counter_initial_spec(),
            pick_rule=distinct_invalid,
            run_id="test_backtrack_strike_accounting",
            max_steps=50,
        )

    # _backtrack is invoked only after the 3rd invalid pick at depth 0, and on
    # an empty chain it raises immediately.
    assert calls["pick"] == 3, f"expected 3 invalid picks before backtrack, got {calls['pick']}"
    assert calls["backtrack"] == 1, f"expected 1 backtrack call, got {calls['backtrack']}"
    assert "chain is empty" in str(excinfo.value)


def test_distinct_invalid_picks_raise_via_empty_chain_backtrack():
    """
    Three DISTINCT invalid picks at depth 0 trip the 3-strike threshold, call
    _backtrack on an empty chain, and raise RefinementStall ("chain is empty")
    — the intended give-up path.
    """
    state = {"n": 0}

    def distinct_invalid(applicable_rules, spec):
        state["n"] += 1
        return {"rule_name": f"NoSuchRule{state['n']}", "params": {}}

    with pytest.raises(RefinementStall) as excinfo:
        run(
            formal_spec=_counter_initial_spec(),
            pick_rule=distinct_invalid,
            run_id="test_backtrack_distinct_invalid",
            max_steps=50,
        )
    assert "chain is empty" in str(excinfo.value)
    # Exactly 3 strikes were needed before the empty-chain backtrack fired.
    assert state["n"] == 3


@pytest.mark.xfail(
    reason=(
        "ENGINE BUG: the `__invalid__` 3-strike accounting stores markers in a "
        "set keyed on the rejection error text "
        "(excluded_at[depth].add(('__invalid__', json.dumps({'error': error[:120]})))). "
        "A picker that fails IDENTICALLY each time (e.g. repeatedly emitting the "
        "same unregistered rule name — the most common LLM stall mode) produces "
        "the same error string every call, so the set holds only ONE "
        "`__invalid__` entry and invalid_count never reaches 3. The intended "
        "3-strike->_backtrack path is therefore unreachable for identical "
        "failures; the engine instead spins until the MAX_STEPS guard fires. "
        "Fix: count invalid attempts with a per-depth counter, not a set keyed "
        "on error text."
    ),
    strict=True,
)
def test_identical_invalid_picks_should_backtrack_not_spin_to_max_steps():
    """
    A picker that returns the SAME invalid pick every time SHOULD trip the
    3-strike backtrack at depth 0 (empty chain -> RefinementStall "chain is
    empty") within ~3 picks. Today it instead spins to MAX_STEPS because the
    `__invalid__` markers dedup in a set. This xfail pins the real bug.
    """
    state = {"n": 0}

    def identical_invalid(applicable_rules, spec):
        state["n"] += 1
        return {"rule_name": "NoSuchRule", "params": {}}

    with pytest.raises(RefinementStall) as excinfo:
        run(
            formal_spec=_counter_initial_spec(),
            pick_rule=identical_invalid,
            run_id="test_backtrack_identical_invalid",
            max_steps=50,
        )
    # If the bug were fixed, the give-up would be the empty-chain backtrack
    # after exactly 3 strikes — NOT the MAX_STEPS guard.
    assert "chain is empty" in str(excinfo.value)
    assert state["n"] == 3


# ---------------------------------------------------------------------------
# G08 — stall via MAX_STEPS (valid picks that never converge)
# ---------------------------------------------------------------------------

def test_max_steps_guard_raises_refinement_stall():
    """
    A picker that keeps making VALID progress that never reaches RTL-style
    must trip the MAX_STEPS hard limit and raise RefinementStall. We use
    IntroduceVariable with a fresh name each step (derived from the current
    variable count — applicability/state-driven, not a module global), so the
    chain grows forever without satisfying is_rtl_style.
    """
    def introduce_forever(applicable_rules, spec):
        # Name derived from current spec state -> pure function of spec.
        idx = len(spec.get("variables", []))
        return {
            "rule_name": "IntroduceVariable",
            "params": {
                "name": f"reg{idx}",
                "type": "0..1",
                "abstract": True,
                "reset_value": None,
            },
        }

    with pytest.raises(RefinementStall) as excinfo:
        run(
            formal_spec=_counter_initial_spec(),
            pick_rule=introduce_forever,
            run_id="test_backtrack_max_steps",
            max_steps=5,
        )
    assert "steps without reaching" in str(excinfo.value)


# ---------------------------------------------------------------------------
# G08 — backtrack FIRES and the engine RECOVERS
# ---------------------------------------------------------------------------

class _RecoverAfterBacktrackPicker:
    """
    Stateful picker (instance state, NOT a module global) used within a single
    run() to force exactly one _backtrack and then recover.

    Plan:
      call 1 (depth 0): pick the good Initialization step -> succeeds, chain
                        advances to depth 1.
      calls 2..4 (depth 1): return three DISTINCT invalid picks -> 3 strikes ->
                        engine calls _backtrack, which pops the Initialization
                        step, excludes it at depth 0, replays to the abstract
                        spec, and (because other rules remain applicable at
                        depth 0) returns.
      calls >=5 (depth 0 again, after recover): drive the good sequence to
                        completion via the idempotent _good_pick_rule.

    The invalid picks must be DISTINCT each call: the engine dedups
    `__invalid__` strike markers in a set keyed on the rejection text, so
    identical invalids would never reach the 3-strike threshold (see the xfail
    test). We vary the bad rule name per strike.

    The instance counter is what makes the choice VARY across two visits to
    depth 0 (a pure-function-of-spec picker cannot recover, since the engine's
    pick_rule contract here does not pass the excluded set).
    """

    def __init__(self):
        self.calls = 0
        self.strikes = 0
        self.saw_post_backtrack_depth0 = False

    def __call__(self, applicable_rules, spec):
        self.calls += 1
        names = {r["name"] for r in applicable_rules}

        init_done = spec.get("reset_action") is not None
        count = next((a for a in spec.get("actions", []) if a["name"] == "Count"), None)

        # First visit to depth 0 (no reset yet, first call): make a real step
        # (Initialization) so the chain is non-empty when we then stall.
        if self.calls == 1 and not init_done and "Initialization" in names:
            return {
                "rule_name": "Initialization",
                "params": {"reset_values": {"count": "0"},
                           "reset_action_name": "Reset"},
            }

        # At depth 1 (Initialization committed, Count still has no updates),
        # emit three DISTINCT invalid picks to trigger 3-strike -> _backtrack.
        if init_done and count is not None and not count.get("updates") \
                and self.strikes < 3:
            self.strikes += 1
            return {"rule_name": f"BadRule{self.strikes}", "params": {}}

        # After backtrack the spec is rolled back to no reset_action (depth 0
        # again). The ORIGINAL Initialization choice (params with
        # reset_action_name="Reset") is now excluded at depth 0, so we must
        # offer DIFFERENT params to escape the exclusion. Use a different reset
        # action name; the resulting spec is still a valid path to RTL-style.
        if not init_done and "Initialization" in names:
            self.saw_post_backtrack_depth0 = True
            return {
                "rule_name": "Initialization",
                "params": {"reset_values": {"count": "0"},
                           "reset_action_name": "Rst"},
            }

        # Subsequent steps (Assignment, Iteration): idempotent good sequence.
        return _good_pick_rule(applicable_rules, spec)


def test_backtrack_fires_then_recovers():
    """
    Verify that _backtrack is genuinely exercised (the 3-strike path pops a
    committed step and replays) AND that the engine recovers to an RTL-style
    spec afterward. Proves the backtrack machinery is wired end-to-end, not
    just on the give-up path.
    """
    run_id = "test_backtrack_recover"
    chain_path = pathlib.Path("artifacts") / run_id / "refinement_chain.json"
    if chain_path.exists():
        chain_path.unlink()

    picker = _RecoverAfterBacktrackPicker()
    final_spec = run(
        formal_spec=_counter_initial_spec(),
        pick_rule=picker,
        run_id=run_id,
        max_steps=80,
    )

    assert is_rtl_style(final_spec), "engine should recover to RTL-style after backtrack"
    assert picker.saw_post_backtrack_depth0, (
        "expected the picker to be re-invoked at depth 0 after a backtrack "
        "(spec rolled back to no reset_action) — backtrack did not fire"
    )

    # On-disk chain replays to the same final spec (purity / replay invariant).
    # The excluded (popped) Initialization step must NOT be on disk — only the
    # committed path survives.
    chain = _load_chain(run_id)
    replayed = _replay_chain(_counter_initial_spec(), chain)
    assert _spec_hash(replayed) == _spec_hash(final_spec)


# ---------------------------------------------------------------------------
# G08 — _backtrack helper white-box unit tests
# ---------------------------------------------------------------------------

def test_backtrack_pops_excludes_and_replays():
    """
    White-box: build a one-step chain (Initialization applied), call
    _backtrack, and assert it pops the step, records the popped choice in
    excluded_at at its depth, and returns the replayed spec at the rollback
    point (the original abstract spec) with the truncated chain.
    """
    initial = _counter_initial_spec()
    rule_by_name = {r.__class__.__name__: r for r in RULE_REGISTRY}

    init_params = {"reset_values": {"count": "0"}, "reset_action_name": "Reset"}
    after_init = rule_by_name["Initialization"].apply(copy.deepcopy(initial), init_params)

    chain = [{
        "step": 0,
        "rule_name": "Initialization",
        "params": init_params,
        "pre_hash": _spec_hash(initial),
        "post_hash": _spec_hash(after_init),
    }]
    excluded_at: dict[int, set] = {}

    spec, new_chain = _backtrack(initial, chain, excluded_at, MAX_BACKTRACK_DEPTH)

    # Chain popped back to empty.
    assert new_chain == []
    # Replayed spec at rollback point equals the initial abstract spec.
    assert _spec_hash(spec) == _spec_hash(initial)
    # The popped choice was excluded at depth 0.
    params_json = json.dumps(init_params, sort_keys=True)
    assert ("Initialization", params_json) in excluded_at.get(0, set())


def test_backtrack_raises_on_empty_chain():
    """_backtrack with an empty chain raises RefinementStall immediately."""
    initial = _counter_initial_spec()
    with pytest.raises(RefinementStall) as excinfo:
        _backtrack(initial, [], {}, MAX_BACKTRACK_DEPTH)
    assert "chain is empty" in str(excinfo.value)


def test_backtrack_respects_max_depth():
    """
    When every rollback finds no remaining non-excluded applicable rule, the
    rollback count must cap at max_depth and raise RefinementStall ("exceeded
    maximum depth"). We force this by pre-excluding every rule at every depth
    so no rollback ever finds a `remaining` rule, with a chain longer than
    max_depth so the cap (not the empty-chain branch) is what fires.

    Build a chain of IntroduceVariable steps (each independently applicable and
    replayable), then exclude all rule names at every depth.
    """
    initial = _counter_initial_spec()
    rule_by_name = {r.__class__.__name__: r for r in RULE_REGISTRY}
    all_rule_names = list(rule_by_name)

    # Build a chain longer than max_depth using IntroduceVariable (always
    # applicable, always replayable with distinct names).
    spec = copy.deepcopy(initial)
    chain = []
    n_steps = MAX_BACKTRACK_DEPTH + 2
    for i in range(n_steps):
        params = {"name": f"v{i}", "type": "0..1", "abstract": True,
                  "reset_value": None}
        pre = _spec_hash(spec)
        spec = rule_by_name["IntroduceVariable"].apply(spec, params)
        chain.append({
            "step": i,
            "rule_name": "IntroduceVariable",
            "params": params,
            "pre_hash": pre,
            "post_hash": _spec_hash(spec),
        })

    # Exclude every rule NAME at every depth we could roll back to, so no
    # rollback point ever has a `remaining` applicable rule -> rollback_count
    # keeps incrementing until it hits max_depth. _backtrack computes
    # `remaining = applicable_names - {name for name, _ in excluded_here}`, i.e.
    # it subtracts by rule NAME only and ignores the params_json, so one entry
    # per name per depth (any params_json) suffices.
    excluded_at = {
        depth: {(name, "*") for name in all_rule_names}
        for depth in range(n_steps + 1)
    }

    with pytest.raises(RefinementStall) as excinfo:
        _backtrack(initial, chain, excluded_at, MAX_BACKTRACK_DEPTH)
    assert "maximum depth" in str(excinfo.value)


# ---------------------------------------------------------------------------
# G13 — multi-pass chain accumulation + replay reconstructs FINAL spec
# ---------------------------------------------------------------------------

def test_multipass_chain_accumulates_across_passes():
    """
    Simulate Stage 3's multi-pass pattern: call run() three times with the SAME
    run_id, each pass restricted to a different `allowed_rule_names` subset and
    threading the refined spec in memory between calls. The on-disk
    refinement_chain.json must ACCUMULATE all passes' steps (not be overwritten
    to the last pass), and _replay_chain(initial_abstract, on_disk_chain) must
    reconstruct the FINAL multi-pass spec.
    """
    run_id = "test_backtrack_multipass"
    chain_path = pathlib.Path("artifacts") / run_id / "refinement_chain.json"
    if chain_path.exists():
        chain_path.unlink()

    initial = _counter_initial_spec()

    # Pass 1: only Initialization allowed. Per-pass termination = "no allowed
    # rule applicable" so the pass exits cleanly once reset is installed.
    def pass_done(spec):
        # Done when no rule in this pass's allowed set is applicable.
        return False  # overridden below per-pass via allowed-set exhaustion

    # Use a termination_check that ends the pass when its single goal is met,
    # so run() returns rather than stalling on an allowed set of size 1.
    spec1 = run(
        formal_spec=copy.deepcopy(initial),
        pick_rule=_good_pick_rule,
        run_id=run_id,
        allowed_rule_names={"Initialization"},
        termination_check=lambda s: s.get("reset_action") is not None,
        max_steps=20,
    )

    # Pass 2: only Assignment allowed; thread spec1 in.
    spec2 = run(
        formal_spec=copy.deepcopy(spec1),
        pick_rule=_good_pick_rule,
        run_id=run_id,
        allowed_rule_names={"Assignment"},
        termination_check=lambda s: bool(
            next((a for a in s["actions"] if a["name"] == "Count"), {}).get("updates")
        ),
        max_steps=20,
    )

    # Pass 3: only Iteration allowed; thread spec2 in. Reaches RTL-style.
    spec3 = run(
        formal_spec=copy.deepcopy(spec2),
        pick_rule=_good_pick_rule,
        run_id=run_id,
        allowed_rule_names={"Iteration"},
        termination_check=is_rtl_style,
        max_steps=20,
    )

    assert is_rtl_style(spec3), "final multi-pass spec should be RTL-style"

    # The on-disk chain must contain all three passes' steps, in order.
    chain = _load_chain(run_id)
    rule_names = [s["rule_name"] for s in chain]
    assert rule_names == ["Initialization", "Assignment", "Iteration"], (
        f"chain did not accumulate all passes; got {rule_names} "
        f"(overwrite-to-last-pass regression if this is just ['Iteration'])"
    )

    # step indices renumbered to a single monotonic sequence.
    assert [s["step"] for s in chain] == [0, 1, 2]

    # Replaying the accumulated chain from the INITIAL abstract spec must
    # reconstruct the FINAL multi-pass spec, not just the last pass.
    replayed = _replay_chain(copy.deepcopy(initial), chain)
    assert _spec_hash(replayed) == _spec_hash(spec3), (
        "replay of the accumulated chain did not reconstruct the final "
        "multi-pass spec — chain accumulation/replay is broken"
    )


def test_multipass_does_not_overwrite_prefix_on_zero_step_pass():
    """
    A pass that makes zero successful steps must NOT overwrite the accumulated
    prefix to empty. After a productive pass writes the chain, a no-op pass
    (allowed set whose rules are all inapplicable / already satisfied) leaves
    the on-disk chain intact.
    """
    run_id = "test_backtrack_multipass_noop"
    chain_path = pathlib.Path("artifacts") / run_id / "refinement_chain.json"
    if chain_path.exists():
        chain_path.unlink()

    initial = _counter_initial_spec()

    # Productive pass: install reset.
    spec1 = run(
        formal_spec=copy.deepcopy(initial),
        pick_rule=_good_pick_rule,
        run_id=run_id,
        allowed_rule_names={"Initialization"},
        termination_check=lambda s: s.get("reset_action") is not None,
        max_steps=20,
    )
    before = _load_chain(run_id)
    assert [s["rule_name"] for s in before] == ["Initialization"]

    # No-op pass: allow only Iteration but declare the pass already terminated,
    # so run() returns immediately without applying or saving anything.
    run(
        formal_spec=copy.deepcopy(spec1),
        pick_rule=_good_pick_rule,
        run_id=run_id,
        allowed_rule_names={"Iteration"},
        termination_check=lambda s: True,  # already done -> zero steps
        max_steps=20,
    )

    after = _load_chain(run_id)
    assert after == before, "zero-step pass must not overwrite the committed prefix"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
