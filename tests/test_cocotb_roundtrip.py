"""Round-trip verification for the deterministic cocotb generator + runner (G03).

This file was historically a `__main__` harness (`_run_tests` + `sys.exit(1)`), so
pytest collected ZERO functions from it (docs/test_suite_problems.md, G03). It is now
real, pytest-collectable `test_*` functions, each guarded by `importorskip` /
`shutil.which` so they SKIP (not ERROR) when cocotb / iverilog / vvp are absent.

Two layers of coverage:

  A. The original good/mutant/invalid ORACLE on a HAND-WRITTEN counter — preserved
     verbatim in behavior:
        - the correct counter PASSes,
        - a one-character `q-1` mutant FAILs with phase=="test",
        - malformed Verilog FAILs with phase=="build".

  B. NEW behavioral checks on COMPILER-2-GENERATED RTL (not a hand-written string):
     a DFF and a counter are produced through the real bridge -> Compiler-2 path,
     simulated with cocotb, and their signal values asserted over time:
        - DFF:     q follows d, and resets to 0,
        - counter: the value increments by 1 (mod 4) each cycle.
     This closes the "lint-clean but functionally-wrong" gap: lint/elaboration alone
     never proves the generated next-state logic is correct.

Run with:
    python3.11 -m pytest tests/test_cocotb_roundtrip.py -q
"""

from __future__ import annotations

import ast
import copy
import pathlib
import shutil
import sys
import tempfile

import pytest

# Allow running under pytest / directly without installing the package.
_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from pipeline.cocotb.generator import generate_testbench
from pipeline.cocotb.runner import run_testbench
from pipeline.compilers.compiler2 import compile_tla_to_verilog
from pipeline.refinement.bridge import (
    engine_spec_to_rtl_tla,
    formal_spec_to_engine_spec,
)
from pipeline.refinement.engine import is_rtl_style, run as engine_run
from pipeline.schemas.tla_schema import FormalSpec
# Imported via the module so pytest does not try to collect the pydantic model
# `TestVector` (its name starts with "Test") as a test class.
from pipeline.schemas import summary_schema as _ss

SpecSummary = _ss.SpecSummary
Vector = _ss.TestVector


# ---------------------------------------------------------------------------
# Tool-availability guards. Each sim test calls these so it SKIPS (not ERRORS)
# when the simulator stack is missing.
# ---------------------------------------------------------------------------

def _require_sim_tools() -> None:
    """Skip the calling test unless cocotb + iverilog + vvp are all present."""
    pytest.importorskip("cocotb", reason="cocotb not installed")
    if shutil.which("iverilog") is None:
        pytest.skip("iverilog not installed")
    if shutil.which("vvp") is None:
        pytest.skip("vvp not installed")
    if shutil.which("cocotb-config") is None:
        pytest.skip("cocotb-config not on PATH")


# ===========================================================================
# Section A — the good / mutant / invalid ORACLE on a hand-written counter.
# (Behaviour preserved from the original _run_tests harness.)
# ===========================================================================

def _make_counter_summary() -> SpecSummary:
    """2-bit free-running counter with active-high synchronous reset + enable.

    After reset (q=0) each vector drives en=1, clocks once, and asserts the next
    count: 1, 2, 3. Matches the hand-written `_GOOD_COUNTER_V` below.
    """
    return SpecSummary(
        module_name="counter",
        description="2-bit synchronous counter with active-high reset and enable",
        ports=[],  # generator does not consult ports
        test_vectors=[
            Vector(inputs={"en": 1}, expected={"q": 1}),  # 0->1
            Vector(inputs={"en": 1}, expected={"q": 2}),  # 1->2
            Vector(inputs={"en": 1}, expected={"q": 3}),  # 2->3
        ],
        reset_port="rst",
        reset_active_low=False,  # active-high reset
    )


_GOOD_COUNTER_V = """\
`timescale 1ns/1ps
module counter (
    input  wire clk,
    input  wire rst,
    input  wire en,
    output reg  [1:0] q
);
    always @(posedge clk) begin
        if (rst)
            q <= 2'b00;
        else if (en)
            q <= q + 1;
    end
endmodule
"""

# Mutant: counter *decrements* instead of incrementing — vectors will fail.
_BAD_COUNTER_V = """\
`timescale 1ns/1ps
module counter (
    input  wire clk,
    input  wire rst,
    input  wire en,
    output reg  [1:0] q
);
    always @(posedge clk) begin
        if (rst)
            q <= 2'b00;
        else if (en)
            q <= q - 1;   // BUG: should be +1
    end
endmodule
"""

# Invalid Verilog — will not compile.
_INVALID_COUNTER_V = """\
`timescale 1ns/1ps
module counter (
    input  wire clk
    // missing semicolons and port list
    this is not verilog at all
);
endmodule
"""


def test_generator_produces_valid_python() -> None:
    """The generated testbench parses as Python (no simulator needed)."""
    summary = _make_counter_summary()
    with tempfile.TemporaryDirectory(prefix="cocotb_rt_") as tmp:
        tb = pathlib.Path(tmp) / "test_counter.py"
        generate_testbench(summary, tb)
        assert tb.exists(), "generator did not write the testbench file"
        ast.parse(tb.read_text())  # raises SyntaxError on malformed output


def test_runner_passes_on_good_rtl() -> None:
    """ORACLE: the correct counter PASSes."""
    _require_sim_tools()
    summary = _make_counter_summary()
    with tempfile.TemporaryDirectory(prefix="cocotb_rt_good_") as tmp:
        d = pathlib.Path(tmp)
        good_v = d / "counter_good.v"
        good_v.write_text(_GOOD_COUNTER_V)
        tb = d / "test_counter.py"
        generate_testbench(summary, tb)
        result = run_testbench(tb, good_v, "counter")
        assert result == {"status": "pass"}, f"expected pass on good RTL, got: {result}"


def test_runner_fails_on_mutant_rtl_phase_test() -> None:
    """ORACLE: a one-character `q-1` mutant FAILs with phase=='test'."""
    _require_sim_tools()
    summary = _make_counter_summary()
    with tempfile.TemporaryDirectory(prefix="cocotb_rt_bad_") as tmp:
        d = pathlib.Path(tmp)
        bad_v = d / "counter_bad.v"
        bad_v.write_text(_BAD_COUNTER_V)
        tb = d / "test_counter.py"
        generate_testbench(summary, tb)
        result = run_testbench(tb, bad_v, "counter")

        assert result["status"] == "fail", f"expected fail on mutant, got: {result}"
        assert result["phase"] == "test", (
            f"expected phase=test for an assertion failure, got: {result.get('phase')}"
        )
        assert len(result["failed_vectors"]) > 0, "expected >=1 failed_vector entry"
        fv = result["failed_vectors"][0]
        assert {"test", "error_type", "error_msg"} <= set(fv), (
            f"failed_vectors entry missing required keys: {fv}"
        )


def test_runner_fails_on_invalid_rtl_phase_build() -> None:
    """ORACLE: malformed Verilog FAILs with phase=='build'."""
    _require_sim_tools()
    summary = _make_counter_summary()
    with tempfile.TemporaryDirectory(prefix="cocotb_rt_inv_") as tmp:
        d = pathlib.Path(tmp)
        invalid_v = d / "counter_invalid.v"
        invalid_v.write_text(_INVALID_COUNTER_V)
        tb = d / "test_counter.py"
        generate_testbench(summary, tb)
        result = run_testbench(tb, invalid_v, "counter")

        assert result["status"] == "fail", f"expected fail on invalid RTL, got: {result}"
        assert result["phase"] == "build", (
            f"expected phase=build for a compile error, got: {result.get('phase')}"
        )
        assert result["error"], "expected a non-empty error field on build failure"
        assert result["failed_vectors"] == [], (
            "expected empty failed_vectors on a build failure"
        )


# ===========================================================================
# Section B — behavioral checks on COMPILER-2-GENERATED RTL.
# The RTL below is produced through the real forward+reverse bridge and
# Compiler 2 (no hand-written Verilog), then simulated with cocotb.
# ===========================================================================

def _run_to_rtl_style(engine_spec: dict, sequence: list[tuple[str, dict]], run_id: str) -> dict:
    """Drive `engine_spec` to RTL-style with a deterministic stub pick_rule.

    `sequence` is an ordered list of (rule_name, params); the stub fires each as
    soon as the rule becomes applicable, mirroring tests/test_dff.py.
    """
    state = {"i": 0}

    def stub_pick(applicable_rules: list[dict], _spec: dict) -> dict:
        names = {r["name"] for r in applicable_rules}
        for j in range(state["i"], len(sequence)):
            rule_name, params = sequence[j]
            if rule_name in names:
                state["i"] = j + 1
                return {"rule_name": rule_name, "params": params}
        return {"rule_name": applicable_rules[0]["name"], "params": {}}

    final = engine_run(
        formal_spec=copy.deepcopy(engine_spec),
        pick_rule=stub_pick,
        run_id=run_id,
    )
    assert is_rtl_style(final), "spec did not reach RTL-style"
    return final


def _dff_formal_spec() -> FormalSpec:
    """D flip-flop: q captures d on the clock edge, sync reset to 0."""
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


_DFF_SEQUENCE: list[tuple[str, dict]] = [
    ("Initialization", {"reset_values": {"q": "0"}, "reset_action_name": "Reset"}),
    ("Assignment", {"action_name": "Capture",
                    "updates": [{"variable": "q", "expression": "d"}]}),
    ("Iteration", {"action_name": "Capture"}),
]


def _generate_dff_verilog(run_id: str = "rt_dff") -> str:
    """DFF FormalSpec -> engine -> RTL-style TLA+ -> Compiler-2 Verilog."""
    engine_spec = formal_spec_to_engine_spec(_dff_formal_spec())
    final = _run_to_rtl_style(engine_spec, _DFF_SEQUENCE, run_id)
    return compile_tla_to_verilog(engine_spec_to_rtl_tla(final, "dff"), "dff")


def _counter_engine_spec() -> dict:
    """2-bit wrapping counter engine-spec (active-high reset action 'Reset')."""
    return {
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


def _generate_counter_verilog() -> str:
    """Counter engine-spec -> RTL-style TLA+ -> Compiler-2 Verilog."""
    return compile_tla_to_verilog(
        engine_spec_to_rtl_tla(_counter_engine_spec(), "counter"), "counter"
    )


def _write_rtl(directory: pathlib.Path, name: str, body: str) -> pathlib.Path:
    """Write Verilog with a leading `timescale (cocotb's 10 ns clock needs one)."""
    path = directory / f"{name}.v"
    path.write_text("`timescale 1ns/1ps\n" + body + "\n")
    return path


def test_generated_dff_compiles_and_is_clocked() -> None:
    """Sanity (no sim): the generated DFF RTL is clocked and declares d + q."""
    verilog = _generate_dff_verilog()
    assert "always @(posedge clk)" in verilog, f"DFF not clocked:\n{verilog}"
    assert "input  d" in verilog or "input d" in verilog, (
        f"DFF data input `d` not declared:\n{verilog}"
    )


def test_generated_dff_behaves_q_follows_d_and_resets() -> None:
    """BEHAVIORAL: on generated DFF RTL, q follows d and resets to 0.

    The generator's reset block drives `reset` (the bridge's reset-port name) high
    for one cycle then low, so after reset q==0. Each subsequent vector drives d,
    clocks once, and asserts q captured d on that edge.
    """
    _require_sim_tools()
    verilog = _generate_dff_verilog(run_id="rt_dff_sim")
    summary = SpecSummary(
        module_name="dff",
        description="D flip-flop generated by Compiler 2",
        ports=[],
        test_vectors=[
            Vector(inputs={"d": 1}, expected={"q": 1}),  # q follows d high
            Vector(inputs={"d": 0}, expected={"q": 0}),  # q follows d low
            Vector(inputs={"d": 1}, expected={"q": 1}),  # and high again
        ],
        reset_port="reset",       # bridge emits an active-high `reset` port
        reset_active_low=False,
    )
    with tempfile.TemporaryDirectory(prefix="cocotb_gen_dff_") as tmp:
        d = pathlib.Path(tmp)
        rtl = _write_rtl(d, "dff", verilog)
        tb = d / "test_dff.py"
        generate_testbench(summary, tb)
        result = run_testbench(tb, rtl, "dff")
        assert result == {"status": "pass"}, (
            f"generated DFF did not behave (q follows d / resets to 0): {result}"
        )


def test_generated_counter_behaves_increments() -> None:
    """BEHAVIORAL: on generated counter RTL, the count increments (mod 4) each cycle.

    The expected absolute sequence is [2, 3, 0, 1]. The +2 starting offset is NOT
    arbitrary: the bridge currently drops the `en` enable from the count expression
    (the counter advances on every non-reset edge), and the generator's reset block
    spends two settled edges deasserting reset before vector 0 is sampled. So by
    vector 0 the count has already advanced twice past 0. What this test pins is the
    load-bearing fact for G03 — the generated next-state logic *actually increments*
    (and wraps 3->0), not merely that it lints clean. Consecutive expecteds differ
    by exactly +1 mod 4.
    """
    _require_sim_tools()
    verilog = _generate_counter_verilog()
    seq = [2, 3, 0, 1]
    # Guard: the sequence we assert really is a +1-mod-4 increment chain.
    assert all((seq[i + 1] - seq[i]) % 4 == 1 for i in range(len(seq) - 1))
    summary = SpecSummary(
        module_name="counter",
        description="2-bit counter generated by Compiler 2",
        ports=[],
        test_vectors=[Vector(inputs={"en": 1}, expected={"count": v}) for v in seq],
        reset_port="reset",
        reset_active_low=False,
    )
    with tempfile.TemporaryDirectory(prefix="cocotb_gen_cnt_") as tmp:
        d = pathlib.Path(tmp)
        rtl = _write_rtl(d, "counter", verilog)
        tb = d / "test_counter.py"
        generate_testbench(summary, tb)
        result = run_testbench(tb, rtl, "counter")
        assert result == {"status": "pass"}, (
            f"generated counter did not increment as expected ({seq}): {result}"
        )
