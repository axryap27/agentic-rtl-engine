"""
Regression tests for combinational-output support and the FIFO design class.

The FIFO is the first design with a COMBINATIONAL output: the full/empty flags
must reflect current occupancy, so they are continuous `assign`s, not registers.
A Transition can be marked `combinational=True`; its target signals become wires
(born concrete, never clocked, never reset) emitted as CombinationalLogic.

This file pins, for the combinational mechanism:
  schema     — Transition.combinational, defaulting False (backward compatible).
  bridge     — a combinational transition becomes a combinational ACTION; its
               target signals are born concrete (abstract=False) + combinational.
  engine     — is_rtl_style accepts an unclocked combinational action and a
               reset-less combinational variable, WITHOUT relaxing ordinary regs.
  Iteration  — never clocks a combinational action (is_applicable + apply no-op).
  Init       — never resets a combinational variable; not kept applicable by one.
  Compiler 2 — a combinational variable emits as an `assign` (output wire).

and for the FIFO end to end (bridge -> engine -> Compiler 2): the memory array,
the we-gated indexed write, the registered read, the occupancy counter's flat
ELSE-IF chain, and the combinational full/empty flags, lint-clean.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from pipeline.schemas.tla_schema import FormalSpec, Transition
from pipeline.compilers.compiler2 import RTLTLACompiler, verify_banlist
from pipeline.refinement.bridge import formal_spec_to_engine_spec, engine_spec_to_rtl_tla
from pipeline.refinement.engine import _replay_chain, is_rtl_style, run as engine_run, RULE_REGISTRY
from pipeline.refinement.rules.iteration import Iteration
from pipeline.refinement.rules.initialization import Initialization
from pipeline.refinement.rules.alternation import Alternation
from pipeline.refinement.rules.sequential_composition import SequentialComposition
from tests.fixtures.medium_designs import (
    fifo_formal_spec, fifo_picker_sequence, _FIFO_DEPTH,
)

_FIFO_PORT_WIDTHS = {"clk": 1, "reset": 1, "wr_en": 1, "rd_en": 1, "din": 8}


def _converge(formal, seq, run_id, max_steps=24):
    """Drive the real engine to RTL-style with an applicability/no-op-aware picker."""
    rbn = {r.__class__.__name__: r for r in RULE_REGISTRY}

    def picker(applicable, s):
        names = {r["name"] for r in applicable}
        for n, p in seq:
            if n in names and rbn[n].apply(s, p) != s:
                return {"rule_name": n, "params": p}
        return {"rule_name": applicable[0]["name"], "params": {}}

    return engine_run(formal_spec_to_engine_spec(formal), picker, run_id=run_id, max_steps=max_steps)


def _fifo_verilog():
    refined = _converge(fifo_formal_spec(), fifo_picker_sequence(), "fifo_codegen_test")
    rtl = engine_spec_to_rtl_tla(refined, "fifo", port_widths=_FIFO_PORT_WIDTHS, reset_port="reset")
    return refined, RTLTLACompiler(rtl, reset_port="reset").compile(module_name="fifo")


# ---------------------------------------------------------------------------
# Combinational-output mechanism (a small counter + a combinational flag)
# ---------------------------------------------------------------------------

def _cnt_max_spec():
    return FormalSpec(
        module_name="cnt_max", description="counter + combinational is_max flag",
        variables={"cnt": {"type": "Nat", "width": 2}, "is_max": {"type": "Bit", "width": 1}},
        initial={"cnt": "0"},
        transitions=[
            {"label": "Tick", "condition": "TRUE", "updates": {"cnt": "(cnt + 1) % 4"}},
            {"label": "Flags", "condition": "TRUE", "combinational": True,
             "updates": {"is_max": "cnt = 3"}},
        ],
        invariants=[],
    )


_CNT_SEQ = [("Initialization", {"reset_values": {"cnt": "0"}, "reset_action_name": "Reset"}),
            ("Iteration", {"action_name": "Tick"})]


def test_transition_combinational_field_defaults_false():
    assert Transition(label="x", condition="TRUE", updates={}).combinational is False
    assert Transition(label="x", condition="TRUE", updates={}, combinational=True).combinational is True


def test_bridge_marks_combinational_action_and_born_concrete_var():
    eng = formal_spec_to_engine_spec(_cnt_max_spec())
    flags = next(a for a in eng["actions"] if a["name"] == "Flags")
    assert flags["combinational"] is True and flags["clocked"] is False
    is_max = next(v for v in eng["variables"] if v["name"] == "is_max")
    assert is_max["combinational"] is True
    assert is_max["abstract"] is False          # born concrete (a wire, not refined)
    cnt = next(v for v in eng["variables"] if v["name"] == "cnt")
    assert cnt["combinational"] is False and cnt["abstract"] is True


def test_is_rtl_style_accepts_combinational_without_clock_or_reset():
    refined = _replay_chain(formal_spec_to_engine_spec(_cnt_max_spec()),
                            [{"rule_name": n, "params": p} for n, p in _CNT_SEQ])
    is_max = next(v for v in refined["variables"] if v["name"] == "is_max")
    assert is_max["reset_value"] is None and is_max["clocked"] is False  # a wire
    flags = next(a for a in refined["actions"] if a["name"] == "Flags")
    assert flags["clocked"] is False
    assert is_rtl_style(refined) is True


def test_is_rtl_style_still_requires_clock_for_ordinary_action():
    """The rule-5 carve-out is for combinational actions ONLY — an ordinary
    unclocked register action must STILL block RTL-style."""
    refined = _replay_chain(formal_spec_to_engine_spec(_cnt_max_spec()),
                            [{"rule_name": "Initialization",
                              "params": {"reset_values": {"cnt": "0"}, "reset_action_name": "Reset"}}])
    # Tick is not yet clocked and is NOT combinational -> not RTL-style.
    assert is_rtl_style(refined) is False


def test_iteration_skips_combinational_action():
    eng = formal_spec_to_engine_spec(_cnt_max_spec())
    it = Iteration()
    # Flags is combinational -> Iteration is not applicable to it alone, and
    # applying it is a no-op (never clocks a wire).
    flags_clocked = it.apply(eng, {"action_name": "Flags"})
    assert flags_clocked == eng                       # no-op
    # Tick (ordinary) is still iterable.
    assert it.is_applicable(eng) is True


def test_alternation_and_seqcomp_apply_no_op_on_combinational_action():
    """A stray live pick naming a combinational action by name must be a NO-OP in
    apply() (the engine's no-op guard then excludes it). Without this, Alternation/
    SequentialComposition would corrupt a flag into self-referential RTL that
    iverilog accepts — is_applicable's exclusion alone does NOT protect apply(),
    since it returns True on the strength of the register actions."""
    eng = formal_spec_to_engine_spec(fifo_formal_spec())
    alt = Alternation().apply(eng, {
        "action_name": "Flags",
        "branches": [{"guard": "count = 4", "updates": [{"variable": "full", "expression": "1"}]}],
    })
    assert alt == eng, "Alternation mutated a combinational action"
    seq = SequentialComposition().apply(eng, {
        "action_name": "Flags",
        "steps": [{"name": "s0", "guard": "TRUE", "updates": [{"variable": "full", "expression": "1"}]}],
    })
    assert seq == eng, "SequentialComposition mutated a combinational action"
    # The Flags action stays a clean combinational assign — no branches/steps.
    flags = next(a for a in alt["actions"] if a["name"] == "Flags")
    assert not flags.get("branches") and not flags.get("sequential_steps")


def test_initialization_does_not_reset_combinational_var():
    eng = formal_spec_to_engine_spec(_cnt_max_spec())
    after = _replay_chain(eng, [{"rule_name": "Initialization", "params": {
        "reset_values": {"cnt": "0", "is_max": "0"}, "reset_action_name": "Reset"}}])
    is_max = next(v for v in after["variables"] if v["name"] == "is_max")
    assert is_max["reset_value"] is None
    reset = next(a for a in after["actions"] if a["name"] == "Reset")
    assert all(u["variable"] != "is_max" for u in reset["updates"])
    # And Initialization is not kept applicable by the un-reset combinational var.
    assert Initialization().is_applicable(after) is False


def test_compiler2_emits_combinational_assign():
    refined = _replay_chain(formal_spec_to_engine_spec(_cnt_max_spec()),
                            [{"rule_name": n, "params": p} for n, p in _CNT_SEQ])
    rtl = engine_spec_to_rtl_tla(refined, "cnt_max", port_widths={"clk": 1, "reset": 1}, reset_port="reset")
    v = RTLTLACompiler(rtl, reset_port="reset").compile(module_name="cnt_max")
    assert "assign is_max = cnt == 3;" in v
    assert "output is_max" in v and "output reg is_max" not in v
    assert "output reg [1:0] cnt" in v


# ---------------------------------------------------------------------------
# FIFO end to end (codegen)
# ---------------------------------------------------------------------------

def test_fifo_converges_with_existing_rules():
    refined, _ = _fifo_verilog()
    assert is_rtl_style(refined) is True
    clocked = {a["name"] for a in refined["actions"] if a.get("clocked")}
    assert clocked == {"Write", "Read", "UpdateCount"}     # Flags stays combinational
    comb = {v["name"] for v in refined["variables"] if v.get("combinational")}
    assert comb == {"full", "empty"}


def test_fifo_codegen_shape():
    _, v = _fifo_verilog()
    assert f"reg  [7:0] mem [0:{_FIFO_DEPTH - 1}];" in v   # memory array
    assert "mem[wptr] <=" in v                              # indexed write
    assert "dout <= " in v                                  # registered read
    assert "assign full = count == 4;" in v                # combinational flags
    assert "assign empty = count == 0;" in v
    assert "always @(posedge clk)" in v
    assert "if (reset)" in v
    # full/empty are output wires, not registers.
    assert "output full" in v and "output reg full" not in v
    assert "output empty" in v and "output reg empty" not in v
    verify_banlist(v)


def test_fifo_count_chain_is_flat_else_if():
    """The occupancy counter must be one nested ternary (flat ELSE-IF priority
    chain), with no leaked IF/THEN/ELSE keyword."""
    _, v = _fifo_verilog()
    assert "count <= (" in v
    for kw in (" IF ", " THEN ", " ELSE "):
        assert kw not in v, f"leaked TLA+ keyword {kw!r} in:\n{v}"


@pytest.mark.skipif(not shutil.which("iverilog"), reason="iverilog not installed")
def test_fifo_lints_clean(tmp_path):
    _, v = _fifo_verilog()
    p = tmp_path / "fifo.v"
    p.write_text(v)
    r = subprocess.run(["iverilog", "-Wall", "-t", "null", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, f"lint failed:\n{r.stdout}\n{r.stderr}\n\n{p.read_text()}"
