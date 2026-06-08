"""
Canonical D flip-flop integration test — deterministic, NO LLM.

This is the acceptance test for BUG-18 (free input ports referenced in
transitions were never declared) and the realization of BUG-9 (CLAUDE.md
documents `python3.11 tests/test_dff.py` but the file did not exist).

It runs a hand-built D flip-flop FormalSpec through the real pipeline path,
bypassing the LLM agents (Agent 1 / Agent 3) entirely:

    FormalSpec
      -> formal_spec_to_engine_spec       (bridge: forward)
      -> engine.run(stub_pick)            (deterministic refinement)
      -> engine_spec_to_rtl_tla           (bridge: reverse, RTL-style TLA+)
      -> compile_tla_to_verilog           (Compiler 2: Verilog-2001)

The DFF is the minimal design that exposes BUG-18: `q` follows the data input
`d`, which is a *free* identifier — it appears only in a transition's update
expression, is not a FormalSpec variable, and therefore never entered the
RTL-style VARIABLES block. Before the fix, Compiler 2 emitted

    q <= d;   // 'd' never declared as a port

and iverilog failed to elaborate it ("Unable to bind wire/reg/memory 'd'").

Acceptance criteria (each an assertion below):
  1. `always @(posedge clk)` is present (clocked logic).
  2. `d` IS declared as an `input` port.
  3. `q` is `output reg`.
  4. The emitted Verilog ELABORATES CLEAN under `iverilog -Wall -t null`
     (exit 0) — the criterion that would have caught BUG-18.

A regression assertion (test_counter_enable_declared_as_input) covers the
sibling defect: a 2-bit counter whose guard references the free input `en`
must now declare `en` as an input (previously the enable was silently dropped).

Run with:
    python3.11 -m pytest tests/test_dff.py -v
Or directly (as CLAUDE.md documents):
    python3.11 tests/test_dff.py
"""

from __future__ import annotations

import copy
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

# Ensure the project root is on sys.path when run directly.
_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from pipeline.compilers.compiler2 import compile_tla_to_verilog
from pipeline.refinement.bridge import (
    engine_spec_to_rtl_tla,
    formal_spec_to_engine_spec,
)
from pipeline.refinement.engine import is_rtl_style, run
from pipeline.schemas.tla_schema import FormalSpec


# ---------------------------------------------------------------------------
# Hand-built D flip-flop FormalSpec (no LLM)
# ---------------------------------------------------------------------------
# q follows the data input d; synchronous reset to 0. `d` is a free input:
# it appears only in the Capture transition's update, never as a variable.

def _dff_formal_spec() -> FormalSpec:
    return FormalSpec(
        module_name="dff",
        description="D flip-flop: q captures d on the clock edge, sync reset to 0.",
        variables={"q": {"type": "Bit", "width": 1}},
        initial={"q": "0"},
        transitions=[
            {"label": "Capture", "condition": "TRUE", "updates": {"q": "d"}},
        ],
        invariants=[],
    )


# ---------------------------------------------------------------------------
# Deterministic stub pick_rule (mirrors test_refinement_convergence.py)
# ---------------------------------------------------------------------------
# Known-good sequence that drives the DFF to RTL-style:
#   Step 0: Initialization — reset_value q=0, create Reset action.
#   Step 1: Assignment    — explicit update q' = d on Capture.
#   Step 2: Iteration     — mark Capture clocked.

_DFF_SEQUENCE: list[tuple[str, dict]] = [
    (
        "Initialization",
        {"reset_values": {"q": "0"}, "reset_action_name": "Reset"},
    ),
    (
        "Assignment",
        {
            "action_name": "Capture",
            "updates": [{"variable": "q", "expression": "d"}],
        },
    ),
    (
        "Iteration",
        {"action_name": "Capture"},
    ),
]

_step_index: int = 0  # global for the stub (reset before each test)


def _stub_pick_rule(applicable_rules: list[dict], spec: dict) -> dict:
    """Deterministic pick_rule: walk _DFF_SEQUENCE in order."""
    global _step_index
    applicable_names = {r["name"] for r in applicable_rules}
    for i in range(_step_index, len(_DFF_SEQUENCE)):
        rule_name, params = _DFF_SEQUENCE[i]
        if rule_name in applicable_names:
            _step_index = i + 1
            return {"rule_name": rule_name, "params": params}
    fallback = applicable_rules[0]
    return {"rule_name": fallback["name"], "params": {}}


# ---------------------------------------------------------------------------
# Shared helper: run the no-LLM DFF pipeline to Verilog
# ---------------------------------------------------------------------------

def _compile_dff_to_verilog(run_id: str = "test_dff") -> str:
    """Run the hand-built DFF spec through the full no-LLM path to Verilog."""
    global _step_index
    _step_index = 0  # reset stub state

    spec = _dff_formal_spec()
    engine_spec = formal_spec_to_engine_spec(spec)

    final_spec = run(
        formal_spec=copy.deepcopy(engine_spec),
        pick_rule=_stub_pick_rule,
        run_id=run_id,
    )
    assert is_rtl_style(final_spec), "DFF did not reach RTL-style"

    rtl_tla = engine_spec_to_rtl_tla(final_spec, spec.module_name)
    return compile_tla_to_verilog(rtl_tla, spec.module_name)


# ---------------------------------------------------------------------------
# iverilog elaboration gate
# ---------------------------------------------------------------------------

def _iverilog_elaborates(verilog_src: str) -> tuple[int, str]:
    """Elaborate verilog_src with iverilog. Returns (exit_code, output)."""
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_dff_emits_clocked_logic() -> None:
    """(1) The DFF must emit always @(posedge clk)."""
    verilog = _compile_dff_to_verilog()
    assert "always @(posedge clk)" in verilog, (
        f"DFF missing clocked block:\n{verilog}"
    )


def test_dff_data_input_declared() -> None:
    """(2) BUG-18: the free data input `d` must be declared as an input port."""
    verilog = _compile_dff_to_verilog()
    assert re.search(r"input\s+d\b", verilog), (
        "BUG-18 regression: free input `d` is referenced but NOT declared as a "
        f"port. Emitted Verilog:\n{verilog}"
    )


def test_dff_q_is_output_reg() -> None:
    """(3) `q` must be an output reg (clocked storage driven by the always block)."""
    verilog = _compile_dff_to_verilog()
    assert re.search(r"output\s+reg\s+q\b", verilog), (
        f"`q` not declared as output reg:\n{verilog}"
    )


def test_dff_no_spurious_rst_port() -> None:
    """The formal-only reset guard (`rst = TRUE`) must NOT leak a `rst` port.

    The Initialization rule writes the Reset action's guard as `rst = TRUE`,
    but engine_spec_to_rtl_tla replaces that guard with a hardcoded
    `IF reset = 1 THEN ...`, so `rst` never appears in emitted Verilog. The
    free-input scan must not manufacture a dangling `rst` input from it.
    """
    verilog = _compile_dff_to_verilog()
    assert not re.search(r"\brst\b", verilog), (
        f"spurious `rst` signal leaked into Verilog:\n{verilog}"
    )


def test_dff_elaborates_clean_under_iverilog() -> None:
    """(4) ACCEPTANCE: the DFF Verilog must elaborate clean under iverilog.

    This is the criterion that would have caught BUG-18: before the fix,
    iverilog failed with "Unable to bind wire/reg/memory 'd'".
    """
    if shutil.which("iverilog") is None:
        import pytest

        pytest.skip("iverilog not installed")

    verilog = _compile_dff_to_verilog()
    rc, out = _iverilog_elaborates(verilog)
    assert rc == 0, (
        f"BUG-18 regression: DFF Verilog failed to elaborate under iverilog "
        f"(exit {rc}):\n{out}\n\nVerilog:\n{verilog}"
    )


def test_dff_deterministic() -> None:
    """Same input must produce byte-identical Verilog (compiler purity)."""
    v1 = _compile_dff_to_verilog(run_id="test_dff_det_1")
    v2 = _compile_dff_to_verilog(run_id="test_dff_det_2")
    assert v1 == v2, "DFF compilation is not deterministic"


def test_counter_enable_declared_as_input() -> None:
    """BUG-18 sibling regression: a counter guarded by free input `en`.

    A 2-bit counter whose Tick action is guarded by `en = 1` references the
    free input `en`. Before the fix, `en` was silently dropped (the counter
    counted unconditionally). It must now be declared as an input port.

    This exercises the same root cause via a guard (not an update expression),
    and goes through the real bridge -> Compiler 2 path.
    """
    engine_spec = {
        "variables": [
            {"name": "count", "type": "Nat", "width": 2, "abstract": False,
             "reset_value": "0", "clocked": True},
        ],
        "actions": [
            {"name": "Reset", "guard": "reset = 1", "clocked": True,
             "updates": [{"variable": "count", "expression": "0"}]},
            {"name": "Tick", "guard": "en = 1", "clocked": True,
             "updates": [{"variable": "count",
                          "expression": "IF count = 3 THEN 0 ELSE count + 1"}]},
        ],
        "reset_action": "Reset",
        "init": "count = 0",
        "invariants": [],
    }
    rtl_tla = engine_spec_to_rtl_tla(engine_spec, "counter")
    verilog = compile_tla_to_verilog(rtl_tla, "counter")
    assert re.search(r"input\s+en\b", verilog), (
        "BUG-18 regression: guard-only free input `en` not declared as a port "
        f"(enable silently dropped). Verilog:\n{verilog}"
    )
    # D5 / FIX 2: the enable must also be WOVEN INTO the next-state, not merely
    # declared. The Tick guard `en = 1` gates the increment: when not enabled,
    # count holds. FIX 2 emits the POSITIVE-guard form
    #   count <= (en == 1) ? (<increment/wrap>) : count
    # (was the equivalent negated form (en != 1) ? count : <...> before cross-
    # action composition subsumed the per-action negated-guard weaving). The
    # semantics are identical: count advances only when en is asserted, else holds.
    assert re.search(
        r"count\s*<=\s*\(en\s*==\s*1\)\s*\?.*:\s*\(?count\)?;", verilog
    ), (
        "D5 regression: `en` is declared but the count next-state does not gate "
        f"on it (counter advances unconditionally). Verilog:\n{verilog}"
    )


def test_compiler2_undeclared_input_declared_when_bridge_bypassed() -> None:
    """Defensive (B): hand-written TLA+ fed straight to Compiler 2.

    When the bridge is bypassed, Compiler 2 itself must never emit a module
    that references an undeclared identifier. A hand-written DFF spec whose
    VARIABLES block omits `d` must still declare `d` as a scalar input.
    """
    hand_tla = r"""
---- MODULE dff ----
EXTENDS Integers

VARIABLES
    clk, reset,
    q

UpdatePipeline ==
    /\ clk' = 1 - clk
    /\ IF reset = 1 THEN
          /\ q' = 0
       ELSE
          /\ q' = d

Next == /\ UpdatePipeline
Spec == Init /\ [][Next]_vars
====
"""
    verilog = compile_tla_to_verilog(hand_tla, "dff")
    assert re.search(r"input\s+d\b", verilog), (
        f"defensive pass failed to declare undeclared `d`:\n{verilog}"
    )
    # Declared exactly once (no double-declaration).
    assert len(re.findall(r"input\s+d\b", verilog)) == 1, (
        f"`d` declared more than once:\n{verilog}"
    )


# ---------------------------------------------------------------------------
# Entry point (dual-mode: pytest + direct execution, per CLAUDE.md)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("D Flip-Flop Integration Test (no LLM) — BUG-18 acceptance")
    print("=" * 60)

    tests = [
        test_dff_emits_clocked_logic,
        test_dff_data_input_declared,
        test_dff_q_is_output_reg,
        test_dff_no_spurious_rst_port,
        test_dff_elaborates_clean_under_iverilog,
        test_dff_deterministic,
        test_counter_enable_declared_as_input,
        test_compiler2_undeclared_input_declared_when_bridge_bypassed,
    ]

    # Print the emitted DFF Verilog for visibility.
    print("\nEmitted DFF Verilog:\n")
    print(_compile_dff_to_verilog())
    print()

    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {t.__name__}: {exc}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
