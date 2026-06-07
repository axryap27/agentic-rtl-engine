"""Direct regression for G12 — multi-branch / multi-step assignments to the
SAME variable must NOT be collapsed first-wins.

Background (test-comprehensiveness audit finding G12; see git history):
    The Alternation rule stashes its mutually-exclusive guarded branches on
    action["branches"]; SequentialComposition stashes its ordered sub-steps on
    action["sequential_steps"]. Both ALSO keep a flat action["updates"] list
    that collapses multiple assignments to one variable down to one entry
    (first-wins). The original bug: bridge.py emitted clocked logic ONLY from
    that flat "updates" list, so for any variable assigned by >=2 branches/steps
    (every FSM/mux/ALU next-state) all branches/steps after the first were
    silently dropped -> lint-clean RTL with the WRONG next-state.

    The fix composes branches into a priority-ordered nested conditional
    (IF g1 THEN e1 ELSE IF g2 THEN e2 ELSE <var>) and composes sequential steps
    by ordered substitution within one cycle. This file pins that behavior:

      1. Alternation: an FSM (state 0->1, 1->2, 2->0) whose branches all assign
         `state` survives into a nested IF in the RTL-TLA and into a nested
         ternary in the Verilog -- and a stale first-wins `updates` summary on
         the action is IGNORED by the bridge.
      2. SequentialComposition: two ordered steps assigning the same var
         (x -> x+1 then x -> x*2) compose to the substitution net expression
         (x + 1) * 2, not a first-wins drop.
      3. Purity: Alternation.apply() and SequentialComposition.apply() are pure
         -- identical output across two calls AND no mutation of input
         spec/params (sequential_composition once mutated params["steps"]).
      4. Functional (cocotb+iverilog): the FSM actually steps 0->1->2->0.

Deterministic and offline -- no LLM. Tool-dependent tests skip (not error)
when iverilog / cocotb are absent.

Run with:
    python3.11 -m pytest tests/test_branch_collapse.py -q
"""

from __future__ import annotations

import copy
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

# Ensure the project root is importable when run directly or under pytest.
_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from pipeline.compilers.compiler2 import compile_tla_to_verilog
from pipeline.refinement.bridge import engine_spec_to_rtl_tla
from pipeline.refinement.rules.alternation import Alternation
from pipeline.refinement.rules.sequential_composition import SequentialComposition


# ---------------------------------------------------------------------------
# Fixtures: engine-spec actions exercising the G12 path
# ---------------------------------------------------------------------------
# Engine-spec shape (see pipeline/refinement/rules/base.py docstring):
#   {"variables":[{name,width,abstract,reset_value,clocked}],
#    "actions":[{name,guard,clocked,branches|sequential_steps|updates}],
#    "reset_action","init","invariants"}


def _fsm_branches() -> list[dict]:
    """Three mutually-exclusive branches ALL assigning `state`:
    state=0 -> 1, state=1 -> 2, state=2 -> 0 (a mod-3 ring counter)."""
    return [
        {"guard": "state = 0", "updates": [{"variable": "state", "expression": "1"}]},
        {"guard": "state = 1", "updates": [{"variable": "state", "expression": "2"}]},
        {"guard": "state = 2", "updates": [{"variable": "state", "expression": "0"}]},
    ]


def _fsm_engine_spec() -> dict:
    """A clocked FSM whose Step action carries `branches` for `state` AND a
    STALE first-wins `updates` summary the bridge must ignore.

    The stale summary deliberately records only the FIRST branch's assignment
    (state' = 1). If the bridge ever fell back to `updates`, the emitted RTL
    would wrongly pin state' = 1 forever (the original G12 bug). `state` is a
    plain (non-r_/non-hw_) clocked var so Compiler 2 makes it `output reg`.
    """
    return {
        "variables": [
            {"name": "state", "type": "Nat", "width": 2, "abstract": False,
             "reset_value": "0", "clocked": True},
        ],
        "actions": [
            {"name": "Reset", "guard": "reset = 1", "clocked": True,
             "updates": [{"variable": "state", "expression": "0"}]},
            {
                "name": "Step",
                "guard": "TRUE",
                "clocked": True,
                "branches": _fsm_branches(),
                # STALE first-wins summary -- bridge must IGNORE this and compose
                # from `branches` instead.
                "updates": [{"variable": "state", "expression": "1"}],
            },
        ],
        "reset_action": "Reset",
        "init": "state = 0",
        "invariants": [],
    }


def _seqcomp_engine_spec() -> dict:
    """A clocked action whose two ordered steps BOTH assign `x`:
    step 1: x -> x + 1, step 2: x -> x * 2. Net effect within one cycle (by
    substitution) is (x + 1) * 2. The stale first-wins `updates` records only
    x' = x + 1, which the bridge must ignore.
    """
    return {
        "variables": [
            {"name": "x", "type": "Nat", "width": 4, "abstract": False,
             "reset_value": "0", "clocked": True},
        ],
        "actions": [
            {"name": "Reset", "guard": "reset = 1", "clocked": True,
             "updates": [{"variable": "x", "expression": "0"}]},
            {
                "name": "Compute",
                "guard": "TRUE",
                "clocked": True,
                "sequential_steps": [
                    {"name": "inc", "guard": "TRUE",
                     "updates": [{"variable": "x", "expression": "x + 1"}]},
                    {"name": "dbl", "guard": "TRUE",
                     "updates": [{"variable": "x", "expression": "x * 2"}]},
                ],
                # STALE first-wins summary -- bridge must ignore.
                "updates": [{"variable": "x", "expression": "x + 1"}],
            },
        ],
        "reset_action": "Reset",
        "init": "x = 0",
        "invariants": [],
    }


# ---------------------------------------------------------------------------
# 1. Alternation -- branches survive into RTL-TLA and Verilog (not first-wins)
# ---------------------------------------------------------------------------

def test_alternation_branches_emit_nested_if_in_rtl_tla() -> None:
    """All three branch guards/values appear in a nested IF in the RTL-TLA.

    The G12 fix composes branches into IF g1 THEN e1 ELSE IF g2 THEN e2 ELSE
    <var>. We assert all three guards and all three values are present in the
    composed RHS, and that the trailing ELSE holds `state` (register-hold).
    The stale first-wins `updates` (state' = 1 alone) must NOT be the whole RHS.
    """
    rtl_tla = engine_spec_to_rtl_tla(_fsm_engine_spec(), "fsm")

    # The composed RHS for state must mention every branch guard and value.
    for guard in ("state = 0", "state = 1", "state = 2"):
        assert guard in rtl_tla, (
            f"branch guard {guard!r} dropped from RTL-TLA (first-wins collapse?):"
            f"\n{rtl_tla}"
        )
    # Nested-IF structure: at least two IF / ELSE keywords for 3 branches.
    assert rtl_tla.count("IF ") >= 2 and rtl_tla.count("ELSE") >= 2, (
        f"expected a nested IF/ELSE chain for 3 branches, got:\n{rtl_tla}"
    )

    # Locate the single composed next-state line for `state` and assert its RHS
    # is the full nested conditional, not the stale first-wins `state' = 1`.
    state_lines = [
        ln for ln in rtl_tla.splitlines()
        if re.search(r"state'\s*=", ln) and "IF" in ln
    ]
    assert state_lines, (
        f"no composed `state' = IF ...` line found in RTL-TLA:\n{rtl_tla}"
    )
    rhs = state_lines[0].split("=", 1)[1]
    # Trailing ELSE must hold the register (state), giving flip-flop semantics.
    assert re.search(r"ELSE\s+state\b", rhs), (
        f"composed state RHS missing register-hold `ELSE state`:\n{rhs}"
    )
    # Every branch target value present in the one composed RHS.
    for val in ("1", "2", "0"):
        assert val in rhs, f"branch value {val!r} missing from composed RHS:\n{rhs}"


def test_alternation_branches_emit_full_ternary_in_verilog() -> None:
    """Compiler 2 renders the nested IF as a nested parenthesized ternary that
    references ALL branch guards AND ALL branch values -- in priority order.

    We assert on token presence + order (Compiler 2 emits `(g) ? (e) : (...)`),
    NOT on exact whitespace.
    """
    rtl_tla = engine_spec_to_rtl_tla(_fsm_engine_spec(), "fsm")
    verilog = compile_tla_to_verilog(rtl_tla, "fsm")

    # All three guards appear as ternary conditions (compiler emits == for =).
    for guard_rx in (r"state\s*==\s*0", r"state\s*==\s*1", r"state\s*==\s*2"):
        assert re.search(guard_rx, verilog), (
            f"guard /{guard_rx}/ missing from Verilog ternary:\n{verilog}"
        )

    # A ternary operator chain must exist for the multi-branch next-state.
    assert verilog.count("?") >= 2 and verilog.count(":") >= 2, (
        f"expected a nested ternary (>=2 '?' and ':') for 3 branches:\n{verilog}"
    )

    # Priority order: the state==0 guard's value (1) must select before the
    # state==1 guard, which selects before state==2. Assert guard token order.
    g0 = verilog.find("state == 0")
    g1 = verilog.find("state == 1")
    g2 = verilog.find("state == 2")
    if g0 == -1:  # tolerate optional inner spacing differences
        g0 = re.search(r"state\s*==\s*0", verilog).start()
        g1 = re.search(r"state\s*==\s*1", verilog).start()
        g2 = re.search(r"state\s*==\s*2", verilog).start()
    assert g0 < g1 < g2, (
        f"branch guards not emitted in priority order (g0={g0}, g1={g1}, "
        f"g2={g2}):\n{verilog}"
    )
    # `state` is a 2-bit clocked var -> output reg.
    assert re.search(r"output\s+reg\s+\[1:0\]\s+state\b", verilog), (
        f"`state` not declared as 2-bit output reg:\n{verilog}"
    )


@pytest.mark.skipif(shutil.which("iverilog") is None, reason="iverilog not installed")
def test_alternation_fsm_elaborates_clean() -> None:
    """The multi-branch FSM Verilog elaborates clean under iverilog."""
    rtl_tla = engine_spec_to_rtl_tla(_fsm_engine_spec(), "fsm")
    verilog = compile_tla_to_verilog(rtl_tla, "fsm")
    rc, out = _iverilog_elaborates(verilog)
    assert rc == 0, f"FSM Verilog failed to elaborate (exit {rc}):\n{out}\n\n{verilog}"


# ---------------------------------------------------------------------------
# 2. SequentialComposition -- ordered steps compose by substitution
# ---------------------------------------------------------------------------

def test_sequential_steps_compose_by_substitution_in_rtl_tla() -> None:
    """Two ordered steps (x -> x+1 then x -> x*2) compose to the net expression
    (x + 1) * 2 -- NOT the first-wins drop (x + 1), and NOT the second alone.

    Semantics (bridge._sequential_exprs): steps run in order within one cycle;
    later steps observe earlier steps' freshly-computed values by substitution,
    yielding one nonblocking next-state expression per variable.
    """
    rtl_tla = engine_spec_to_rtl_tla(_seqcomp_engine_spec(), "seqc")

    # Two `x' =` lines exist: the reset assignment (x' = 0) and the composed
    # Compute next-state. Pick the composed (non-constant) one.
    x_lines = [
        ln for ln in rtl_tla.splitlines()
        if re.search(r"x'\s*=", ln) and not re.search(r"x'\s*=\s*0\s*$", ln)
    ]
    assert x_lines, f"no composed `x' = ...` next-state line in RTL-TLA:\n{rtl_tla}"
    rhs = x_lines[0].split("=", 1)[1]
    norm = re.sub(r"\s+", "", rhs)  # whitespace-insensitive

    # The substitution-composed net expression: (x + 1) * 2.
    assert "(x+1)*2" in norm, (
        f"sequential steps not composed by substitution; expected '(x+1)*2' in "
        f"RHS, got: {rhs!r}\nfull RTL-TLA:\n{rtl_tla}"
    )
    # Both operations survive -- a first-wins drop would have only '+1' and no '*2'.
    assert "+1" in norm and "*2" in norm, (
        f"a step was dropped (first-wins?): RHS={rhs!r}"
    )
    # Net effect is NOT merely the first step (x + 1) standing alone as the RHS.
    assert norm not in ("x+1", "(x+1)"), (
        f"RHS collapsed to first step only (G12 regression): {rhs!r}"
    )


def test_sequential_steps_compose_in_verilog() -> None:
    """Compiler 2 emits the composed (x + 1) * 2 with both ops present and the
    multiply applied to the parenthesized add (not to a dropped first step)."""
    rtl_tla = engine_spec_to_rtl_tla(_seqcomp_engine_spec(), "seqc")
    verilog = compile_tla_to_verilog(rtl_tla, "seqc")

    # Both arithmetic ops survive into the emitted RTL.
    assert re.search(r"x\s*\+\s*1", verilog), f"`x + 1` missing:\n{verilog}"
    assert re.search(r"\*\s*2", verilog), f"`* 2` missing (step dropped?):\n{verilog}"
    # The multiply operates on the (x + 1) result: an add appears before a `* 2`.
    add_pos = re.search(r"x\s*\+\s*1", verilog).start()
    mul_pos = re.search(r"\*\s*2", verilog).start()
    assert add_pos < mul_pos, (
        f"`x + 1` must precede `* 2` in the composed expression:\n{verilog}"
    )


@pytest.mark.skipif(shutil.which("iverilog") is None, reason="iverilog not installed")
def test_sequential_steps_elaborate_clean() -> None:
    """The composed sequential-step Verilog elaborates clean under iverilog."""
    rtl_tla = engine_spec_to_rtl_tla(_seqcomp_engine_spec(), "seqc")
    verilog = compile_tla_to_verilog(rtl_tla, "seqc")
    rc, out = _iverilog_elaborates(verilog)
    assert rc == 0, f"seqcomp Verilog failed to elaborate (exit {rc}):\n{out}\n\n{verilog}"


# ---------------------------------------------------------------------------
# 3. Purity -- apply() is deterministic AND does not mutate its inputs
# ---------------------------------------------------------------------------

def _alternation_spec_and_params() -> tuple[dict, dict]:
    """Abstract spec + Alternation params (FSM branches on `state`)."""
    spec = {
        "variables": [
            {"name": "state", "type": "Nat", "width": 2, "abstract": True,
             "reset_value": None, "clocked": False},
        ],
        "actions": [
            {"name": "Step", "guard": "TRUE", "clocked": False,
             "updates": [], "is_rtl_style": False},
        ],
        "reset_action": None,
        "init": "state = 0",
        "invariants": [],
    }
    params = {"action_name": "Step", "branches": _fsm_branches()}
    return spec, params


def _sequential_spec_and_params() -> tuple[dict, dict]:
    """Abstract spec + SequentialComposition params (two steps on `x`)."""
    spec = {
        "variables": [
            {"name": "x", "type": "Nat", "width": 4, "abstract": True,
             "reset_value": None, "clocked": False},
        ],
        "actions": [
            {"name": "Compute", "guard": "TRUE", "clocked": False,
             "updates": [], "is_rtl_style": False},
        ],
        "reset_action": None,
        "init": "x = 0",
        "invariants": [],
    }
    params = {
        "action_name": "Compute",
        "steps": [
            {"name": "inc", "guard": "TRUE",
             "updates": [{"variable": "x", "expression": "x + 1"}]},
            {"name": "dbl", "guard": "TRUE",
             "updates": [{"variable": "x", "expression": "x * 2"}]},
        ],
    }
    return spec, params


def test_alternation_apply_is_pure() -> None:
    """Alternation.apply() is deterministic and does not mutate spec/params."""
    rule = Alternation()
    spec, params = _alternation_spec_and_params()
    spec_before = copy.deepcopy(spec)
    params_before = copy.deepcopy(params)

    out1 = rule.apply(copy.deepcopy(spec), copy.deepcopy(params))
    out2 = rule.apply(copy.deepcopy(spec), copy.deepcopy(params))
    assert out1 == out2, "Alternation.apply() is not deterministic"

    # Same input dicts passed by reference must remain untouched after apply().
    rule.apply(spec, params)
    assert spec == spec_before, "Alternation.apply() mutated the input spec"
    assert params == params_before, "Alternation.apply() mutated the input params"


def test_sequential_apply_is_pure_and_no_param_mutation() -> None:
    """SequentialComposition.apply() is deterministic and does not mutate inputs.

    Targets a previously-fixed bug where apply() mutated params["steps"] in
    place (it wrote the inherited guard onto steps[0]). The engine replays the
    SAME params from refinement_chain.json to backtrack, so in-place mutation
    would corrupt replay. We deep-compare params before/after.
    """
    rule = SequentialComposition()
    spec, params = _sequential_spec_and_params()
    spec_before = copy.deepcopy(spec)
    params_before = copy.deepcopy(params)

    out1 = rule.apply(copy.deepcopy(spec), copy.deepcopy(params))
    out2 = rule.apply(copy.deepcopy(spec), copy.deepcopy(params))
    assert out1 == out2, "SequentialComposition.apply() is not deterministic"

    rule.apply(spec, params)
    assert spec == spec_before, "SequentialComposition.apply() mutated the input spec"
    assert params == params_before, (
        "SequentialComposition.apply() mutated the input params "
        "(regression: params['steps'] mutated in place -> breaks chain replay)"
    )


def test_sequential_apply_with_unguarded_first_step_no_mutation() -> None:
    """First step with an EMPTY guard inherits the action guard -- and that
    inheritance must NOT write back into the caller's params['steps'][0].

    This is the exact shape that triggered the original in-place mutation:
    apply() filled steps[0]['guard'] from the action. With a deepcopy guard in
    place, the input params must survive unchanged.
    """
    rule = SequentialComposition()
    spec = {
        "variables": [
            {"name": "x", "type": "Nat", "width": 4, "abstract": True,
             "reset_value": None, "clocked": False},
        ],
        "actions": [
            {"name": "Compute", "guard": "en = 1", "clocked": False,
             "updates": [], "is_rtl_style": False},
        ],
        "reset_action": None,
        "init": "x = 0",
        "invariants": [],
    }
    params = {
        "action_name": "Compute",
        "steps": [
            # No 'guard' key on the first step -> apply() should inherit "en = 1".
            {"name": "inc", "updates": [{"variable": "x", "expression": "x + 1"}]},
            {"name": "dbl", "guard": "TRUE",
             "updates": [{"variable": "x", "expression": "x * 2"}]},
        ],
    }
    params_before = copy.deepcopy(params)
    result = rule.apply(spec, params)

    # Inheritance happened on the OUTPUT (action's stored steps), not the input.
    stored = result["actions"][0]["sequential_steps"]
    assert stored[0].get("guard") == "en = 1", (
        f"first step did not inherit the action guard: {stored[0]}"
    )
    assert params == params_before, (
        "guard inheritance leaked back into the caller's params['steps'] "
        "(in-place mutation regression)"
    )
    assert "guard" not in params["steps"][0], (
        "apply() wrote a guard onto the input params' first step"
    )


# ---------------------------------------------------------------------------
# 4. Functional simulation -- the FSM actually steps 0 -> 1 -> 2 -> 0
# ---------------------------------------------------------------------------

def _fsm_summary():
    """SpecSummary for the mod-3 FSM. reset_port='reset' matches the bridge's
    emitted reset port name; the FSM advances every clock with no inputs.

    Expectation offset: the cocotb generator's reset block clocks ONE extra
    cycle with reset deasserted before the first vector (documented in
    tests/test_cocotb_roundtrip.py). So by vector 0 the FSM is already at
    state=1, and the vectors clock it through 2 -> 0 -> 1 -> 2, exercising the
    full mod-3 ring (every branch fires, the 2 -> 0 wrap proves the trailing
    branch survived -- the exact assignment a first-wins collapse would drop).
    """
    from pipeline.schemas.summary_schema import SpecSummary, TestVector

    return SpecSummary(
        module_name="fsm",
        description="mod-3 ring FSM: state steps 0->1->2->0 every clock",
        ports=[],
        test_vectors=[
            # After the reset block the FSM is already at state=1.
            TestVector(inputs={}, expected={"state": 2}),  # 1 -> 2
            TestVector(inputs={}, expected={"state": 0}),  # 2 -> 0 (wrap proven)
            TestVector(inputs={}, expected={"state": 1}),  # 0 -> 1
            TestVector(inputs={}, expected={"state": 2}),  # 1 -> 2
        ],
        reset_port="reset",
        reset_active_low=False,
    )


def test_fsm_steps_through_all_states_functionally(tmp_path) -> None:
    """Behavioral proof: the multi-branch FSM produces the CORRECT next-state
    sequence 0->1->2->0->1, not a first-wins pin to state=1.

    This is the test the original G12 bug would fail: with branches collapsed
    first-wins, state' would be 1 forever and vector 1 (expect state=2) fails.
    Skips (not errors) when iverilog or cocotb are unavailable.
    """
    if shutil.which("iverilog") is None:
        pytest.skip("iverilog not installed")
    pytest.importorskip("cocotb", reason="cocotb not installed")

    from pipeline.cocotb.generator import generate_testbench
    from pipeline.cocotb.runner import run_testbench

    rtl_tla = engine_spec_to_rtl_tla(_fsm_engine_spec(), "fsm")
    verilog = compile_tla_to_verilog(rtl_tla, "fsm")

    # Compiler 2 emits NO `timescale` directive, so iverilog defaults to a 1s
    # time precision and rejects the cocotb generator's `Clock(..., 10, "ns")`
    # ("Bad period: unable to represent 10(ns) with precision 1e0"). That is an
    # unrelated compiler2/generator defect (see DISCOVERY in the dedicated xfail
    # test below); prepend a timescale here so this behavioral G12 check is not
    # blocked by it. The DUT logic under test is untouched.
    verilog_sim = "`timescale 1ns/1ps\n" + verilog

    rtl_path = tmp_path / "fsm.v"
    rtl_path.write_text(verilog_sim)

    tb_path = tmp_path / "test_fsm.py"
    generate_testbench(_fsm_summary(), tb_path)

    result = run_testbench(tb_path, rtl_path, "fsm")
    assert result.get("status") == "pass", (
        "multi-branch FSM did not step 1->2->0->1->2 correctly "
        f"(G12 behavioral regression). Runner result: {result}\n\nVerilog:\n{verilog}"
    )


def test_compiler2_output_simulatable_without_prepended_timescale(tmp_path) -> None:
    """The FSM, AS EMITTED by Compiler 2 (no timescale prepended), simulates
    under the stock cocotb generator.

    This isolates the timescale fix from the G12 branch-collapse logic: the DUT
    next-state is provably correct (see the ternary-order tests and the
    timescale-prepended functional test above). D1 fix: Compiler 2 now emits
    `timescale 1ns/1ps`, so iverilog can represent the 10 ns clock and the module
    is simulatable with no hand-prepended directive."""
    if shutil.which("iverilog") is None:
        pytest.skip("iverilog not installed")
    pytest.importorskip("cocotb", reason="cocotb not installed")

    from pipeline.cocotb.generator import generate_testbench
    from pipeline.cocotb.runner import run_testbench

    rtl_tla = engine_spec_to_rtl_tla(_fsm_engine_spec(), "fsm")
    verilog = compile_tla_to_verilog(rtl_tla, "fsm")  # NO timescale prepended

    rtl_path = tmp_path / "fsm.v"
    rtl_path.write_text(verilog)

    tb_path = tmp_path / "test_fsm.py"
    generate_testbench(_fsm_summary(), tb_path)

    result = run_testbench(tb_path, rtl_path, "fsm")
    assert result.get("status") == "pass", (
        f"Compiler-2 output not simulatable without a prepended timescale: {result}"
    )


# ---------------------------------------------------------------------------
# iverilog elaboration helper (mirrors tests/test_dff.py)
# ---------------------------------------------------------------------------

def _iverilog_elaborates(verilog_src: str) -> tuple[int, str]:
    """Elaborate verilog_src with iverilog. Returns (exit_code, combined_output)."""
    import os

    with tempfile.NamedTemporaryFile(suffix=".v", mode="w", delete=False) as f:
        f.write(verilog_src)
        fname = f.name
    try:
        r = subprocess.run(
            ["iverilog", "-Wall", "-t", "null", fname],
            capture_output=True,
            text=True,
        )
        return r.returncode, r.stdout + r.stderr
    finally:
        os.unlink(fname)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
