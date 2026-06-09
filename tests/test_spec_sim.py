"""
Tests for the spec-derived golden-vector path (pipeline/cocotb/spec_sim.py +
vector_check.py).

The cocotb golden vectors come from Agent 1, which hand-computes them; on deep
sequential designs its arithmetic is fragile (the live FIFO run failed a correct
RTL on one bad vector — a false red). The fix simulates the REFINED engine spec
(an independent interpreter) to derive correct expected outputs from Agent 1's
input stimulus, runs cocotb against those (no false red), and surfaces any Agent-1
vs spec disagreement.

These tests pin:
  * the interpreter (arithmetic, logic, IF, indexing, X-propagation, masking);
  * the simulator reproducing EVERY in-repo design class's real-cocotb-proven
    trace exactly (the trust anchor — an independent model agreeing with the RTL);
  * the cross-check agreeing on correct vectors, and FLAGGING + correcting a wrong
    Agent-1 vector so cocotb passes a correct RTL (the false-red removal);
  * fail-soft (no refined spec -> fall back to Agent 1's testbench).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline.schemas.summary_schema import SpecSummary
from pipeline.refinement.bridge import (
    formal_spec_to_engine_spec, engine_spec_to_rtl_tla,
)
from pipeline.refinement.engine import _replay_chain, is_rtl_style
from pipeline.compilers.compiler2 import RTLTLACompiler
from pipeline.cocotb.spec_sim import derive_expected, _eval, _coerce_input, SpecSimulator
from pipeline.cocotb.vector_check import apply_spec_derived_vectors
from pipeline.cocotb.generator import generate_testbench
from tests.fixtures.medium_designs import MEDIUM_DESIGNS

_HAVE_COCOTB = shutil.which("cocotb-config") is not None and shutil.which("iverilog") is not None


def _refined(design):
    chain = [{"rule_name": n, "params": p} for n, p in design["picker_sequence"]()]
    refined = _replay_chain(formal_spec_to_engine_spec(design["formal_spec"]()), chain)
    assert is_rtl_style(refined)
    return refined, chain


# ---------------------------------------------------------------------------
# Interpreter
# ---------------------------------------------------------------------------

_U32 = (1 << 32) - 1


def test_eval_arithmetic_logic_index():
    assert _eval("1 + 2 * 3", {}) == 7
    assert _eval("(1 + 2) * 3", {}) == 9
    assert _eval("7 % 4", {}) == 3
    assert _eval("(count + 1) % 4", {"count": 3}) == 0
    assert _eval("count - 1", {"count": 3}) == 2
    assert _eval("IF a = 1 THEN 10 ELSE 20", {"a": 1}) == 10
    assert _eval("IF a = 1 THEN 10 ELSE 20", {"a": 0}) == 20
    assert _eval("IF g1 THEN 1 ELSE IF g2 THEN 2 ELSE 3", {"g1": 0, "g2": 1}) == 2
    assert _eval("a /\\ b", {"a": 1, "b": 0}) == 0
    assert _eval("a \\/ b", {"a": 1, "b": 0}) == 1
    assert _eval("~ a", {"a": 0}) == 1
    assert _eval("count = 4", {"count": 4}) == 1
    assert _eval("count /= 4", {"count": 4}) == 0
    assert _eval("mem[i]", {"mem": [10, 20, 30, 40], "i": 2}) == 30
    assert _eval("mem[wptr]", {"mem": [5, 6], "wptr": 0}) == 5


def test_eval_unsigned_underflow_matches_verilog():
    """Verilog evaluates integer expressions unsigned in a (>=32-bit) context, so
    `count - 1` at count==0 wraps to all-ones — a relational op / index / modulo
    then sees the wrapped value, not a signed -1. (iverilog confirms: a 3-bit count
    with `assign f = (count-1) >= 4` gives f=1 at count==0.)"""
    assert _eval("count - 1", {"count": 0}) == _U32          # wraps, not -1
    assert _eval("count - 1 >= 4", {"count": 0}) == 1        # matches real RTL
    assert _eval("(count - 1) % 4", {"count": 0}) == 3       # unsigned -1 % 4
    assert _eval("count - 1", {"count": 5}) == 4             # normal case unaffected


def test_coerce_input_normalisation():
    assert _coerce_input(5) == 5
    assert _coerce_input(True) == 1 and _coerce_input(False) == 0
    assert _coerce_input("0xff") == 255          # hex string (the generator accepts these)
    assert _coerce_input("10") == 10             # decimal string
    assert _coerce_input("x") is None            # don't-care -> X
    assert _coerce_input("1z") is None           # 4-state literal -> X
    assert _coerce_input(None) is None


def test_eval_x_propagation():
    assert _eval("mem[i]", {"mem": [None, None], "i": 0}) is None    # unwritten cell
    assert _eval("x + 1", {"x": None}) is None
    assert _eval("IF c THEN 1 ELSE 0", {"c": None}) is None
    assert _eval("a / b", {"a": 5, "b": 0}) is None                  # div-by-zero -> X
    assert _eval("mem[i]", {"mem": [1, 2], "i": 9}) is None          # out-of-range -> X


# ---------------------------------------------------------------------------
# Simulator reproduces every design class's proven trace (the trust anchor)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(MEDIUM_DESIGNS))
def test_simulator_reproduces_fixture_trace(name):
    """The independent simulator must reproduce each fixture's hand-derived,
    real-cocotb-proven cocotb_trace EXACTLY — across FSM, ALU, accumulator,
    register file, and FIFO (combinational flags, memory, back-pressure, latency)."""
    d = MEDIUM_DESIGNS[name]
    summary = d["summary"]()
    refined, _ = _refined(d)
    stim = [tv.inputs for tv in summary.test_vectors]
    outs = [p.name for p in summary.ports if p.direction == "output"]
    sim = derive_expected(refined, stim, outs,
                          reset_port=summary.reset_port or "reset",
                          reset_active_low=bool(summary.reset_active_low))
    fixture = [dict(tv.expected) for tv in summary.test_vectors]
    assert sim == fixture, f"{name}: sim {sim} != fixture {fixture}"


def test_simulator_register_file_cold_read_is_x():
    """A read of an unwritten memory cell must be X (omitted), matching the
    register file's warm-up vector which carries an empty expected."""
    d = MEDIUM_DESIGNS["register_file"]
    summary = d["summary"]()
    refined, _ = _refined(d)
    stim = [tv.inputs for tv in summary.test_vectors]
    sim = derive_expected(refined, stim, ["rdata"], reset_port="rst_n", reset_active_low=True)
    assert sim[0] == {}, "cold read should yield no (X) expected"
    assert "rdata" in sim[1]                       # subsequent reads are defined


# ---------------------------------------------------------------------------
# Cross-check (vector_check) — agreement, flagging+correction, fail-soft
# ---------------------------------------------------------------------------

def _seed(tmp_path, design_name, mutate=None, write_rtl=False):
    d = MEDIUM_DESIGNS[design_name]
    summary = d["summary"]()
    chain = [{"rule_name": n, "params": p} for n, p in d["picker_sequence"]()]
    ad = tmp_path / "art"
    ad.mkdir(exist_ok=True)
    sd = summary.model_dump()
    sd["status"] = "success"
    if mutate:
        mutate(sd)
    (ad / "01_summary.json").write_text(json.dumps(sd))
    fd = d["formal_spec"]().model_dump()
    fd["status"] = "success"
    (ad / "02_formal_spec.json").write_text(json.dumps(fd))
    (ad / "refinement_chain.json").write_text(json.dumps(chain))
    if write_rtl:
        refined, _ = _refined(d)
        pw = {p.name: p.width for p in summary.ports if p.direction == "input"}
        rtl = engine_spec_to_rtl_tla(refined, summary.module_name, port_widths=pw,
                                     reset_port=summary.reset_port or "reset",
                                     reset_active_low=bool(summary.reset_active_low))
        v = RTLTLACompiler(rtl, reset_port=summary.reset_port or "reset",
                           reset_active_low=bool(summary.reset_active_low)).compile(
            module_name=summary.module_name)
        (ad / "output.v").write_text(v)
    return ad


def test_cross_check_agrees_on_correct_fixture(tmp_path):
    vc = apply_spec_derived_vectors(_seed(tmp_path, "fifo"))
    assert vc is not None
    assert vc["agreed"] is True
    assert vc["report"]["num_disagreements"] == 0


def test_cross_check_flags_and_corrects_a_wrong_agent1_vector(tmp_path):
    """Corrupt one Agent-1 expected (as the live FIFO v10 empty error did); the
    cross-check must FLAG it and emit a testbench with the spec-derived value."""
    def mutate(sd):
        sd["test_vectors"][3]["expected"]["full"] = 0     # fixture has full=1 at v3

    ad = _seed(tmp_path, "fifo", mutate=mutate)
    vc = apply_spec_derived_vectors(ad)
    assert vc is not None and vc["agreed"] is False
    flagged = {(x["vector"], x["port"], x["agent1"], x["spec"])
               for x in vc["report"]["disagreements"]}
    assert (3, "full", 0, 1) in flagged
    # the corrected testbench asserts the spec value (full == 1) at vector 3.
    tb = (ad / "02_testbench_specvec.py").read_text()
    assert "expected full=1" in tb
    assert (ad / "02_vector_check.json").exists()


def test_cross_check_falls_back_when_an_output_is_never_driven(tmp_path):
    """If the refined spec never drives a declared output (the spec-derived
    reference would assert it ZERO times -> any RTL passes it silently), refuse
    the spec-derived bench and fall back to Agent 1's. Guards the all-X / spec-
    undriven-output silent-green class."""
    def mutate(sd):
        sd["ports"].append({"name": "ghost_out", "direction": "output", "width": 1})
    ad = _seed(tmp_path, "fifo", mutate=mutate)
    assert apply_spec_derived_vectors(ad) is None


def test_cross_check_fail_soft_on_no_chain(tmp_path):
    ad = _seed(tmp_path, "fifo")
    (ad / "refinement_chain.json").write_text("[]")        # refinement did not run
    assert apply_spec_derived_vectors(ad) is None


def test_cross_check_fail_soft_on_missing_summary(tmp_path):
    ad = tmp_path / "empty"
    ad.mkdir()
    assert apply_spec_derived_vectors(ad) is None


@pytest.mark.skipif(not _HAVE_COCOTB, reason="iverilog + cocotb-config required")
def test_stage4_records_disagreement_instead_of_silent_green(tmp_path, monkeypatch):
    """B1: a passing run whose Agent-1 vectors disagreed with the spec must be
    recorded as a NON-CLEAN success (vector_disagreement on 04_evaluation), not a
    silent green — so a possible spec/intent bug is visible."""
    from pipeline.nodes.stage4 import run_stage4

    monkeypatch.chdir(tmp_path)
    run_id = "vc_stage4"
    ad = Path("artifacts") / run_id
    ad.mkdir(parents=True)
    d = MEDIUM_DESIGNS["fifo"]
    summary = d["summary"]()

    sd = summary.model_dump()
    sd["status"] = "success"
    sd["test_vectors"][9]["expected"]["dout"] = 123          # a wrong Agent-1 value
    (ad / "01_summary.json").write_text(json.dumps(sd))
    fd = d["formal_spec"]().model_dump()
    fd["status"] = "success"
    (ad / "02_formal_spec.json").write_text(json.dumps(fd))
    chain = [{"rule_name": n, "params": p} for n, p in d["picker_sequence"]()]
    (ad / "refinement_chain.json").write_text(json.dumps(chain))

    tb = ad / "02_testbench.py"
    generate_testbench(SpecSummary.model_validate(sd), tb)    # Agent 1's (wrong) bench
    (ad / "02_testbench_meta.json").write_text(
        json.dumps({"status": "success", "testbench_path": str(tb)}))

    refined, _ = _refined(d)
    pw = {p.name: p.width for p in summary.ports if p.direction == "input"}
    rtl = engine_spec_to_rtl_tla(refined, summary.module_name, port_widths=pw,
                                 reset_port=summary.reset_port, reset_active_low=summary.reset_active_low)
    v = RTLTLACompiler(rtl, reset_port=summary.reset_port,
                       reset_active_low=summary.reset_active_low).compile(module_name=summary.module_name)
    vp = ad / "output.v"
    vp.write_text(v)
    (ad / "03_rtl_output.json").write_text(json.dumps(
        {"status": "success", "verilog_path": str(vp), "module_name": summary.module_name}))

    run_stage4({"run_id": run_id, "retry_counts": {}, "halt": False})

    ev = json.loads((ad / "04_evaluation.json").read_text())
    assert ev["status"] == "success"                          # correct RTL not failed (no false red)
    assert "vector_disagreement" in ev                        # but the disagreement IS recorded
    assert any(x["vector"] == 9 and x["port"] == "dout" for x in ev["vector_disagreement"])


@pytest.mark.skipif(not _HAVE_COCOTB, reason="iverilog + cocotb-config required")
def test_corrected_vectors_make_cocotb_pass_a_correct_rtl(tmp_path):
    """End-to-end false-red removal: with a CORRUPTED Agent-1 expected, the
    spec-corrected testbench makes cocotb PASS the correct FIFO RTL."""
    from pipeline.cocotb.runner import run_testbench

    def mutate(sd):
        sd["test_vectors"][9]["expected"]["dout"] = 123    # a wrong Agent-1 value

    ad = _seed(tmp_path, "fifo", mutate=mutate, write_rtl=True)
    vc = apply_spec_derived_vectors(ad)
    assert vc is not None and vc["agreed"] is False        # the corruption was caught
    result = run_testbench(vc["testbench_path"], ad / "output.v", "fifo")
    assert result.get("status") == "pass", (
        "spec-corrected testbench did not pass the correct RTL:\n"
        f"{result.get('error')}\n{result.get('raw', '')[-1500:]}"
    )
