"""
Regression tests for the FSMD design class — an 8x8 sequential shift-add
multiplier (control FSM + multi-cycle datapath + start/done handshake).

This is the first design that SEQUENCES a datapath over many clocks behind a
handshake (every prior design completes one transaction per cycle). It proves:

  refinement — a single clocked `Step` transition + a combinational `done`
               converges to RTL-style with ONLY Initialization + Iteration
               (no new rule); the FSM, the guarded datapath chains, and the
               handshake all ride inside the existing Tier-1 machinery.
  codegen    — the shift/bit primitives are expressed arithmetically
               (mplier%2 for the low bit, mcand*2 / mplier/2 for the shifts —
               the pipeline has no <<, >>, or bit-select), and Compiler 2 emits
               clean Verilog-2001 (guarded ternary chains + a combinational
               assign for done), lint-clean.
  function   — the independent spec_sim, and a REAL cocotb run on the generated
               RTL, both compute the correct 16-bit product across operand pairs
               including the 255*255 maximum and zero operands, with `done`
               pulsing on the right cycle.

Multi-cycle verification is what the spec-derived golden vectors unlocked: each
multiply spans 10 vectors (1 start + 8 BUSY + 1 DONE->IDLE) and spec_sim derives
the per-cycle product/done from the start/operand stimulus.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from pipeline.compilers.compiler2 import RTLTLACompiler, verify_banlist
from pipeline.refinement.bridge import formal_spec_to_engine_spec, engine_spec_to_rtl_tla
from pipeline.refinement.engine import is_rtl_style, run as engine_run, RULE_REGISTRY
from pipeline.cocotb.spec_sim import derive_expected
from tests.fixtures.medium_designs import (
    multiplier_formal_spec, multiplier_picker_sequence, multiplier_summary,
)

_HAVE_COCOTB = (shutil.which("cocotb-config") is not None
                and shutil.which("iverilog") is not None)

_MUL_PORT_WIDTHS = {"clk": 1, "reset": 1, "start": 1, "a": 8, "b": 8}


def _converge(run_id="mul_test", max_steps=24):
    """Drive the real engine to RTL-style with an applicability/no-op-aware picker."""
    rbn = {r.__class__.__name__: r for r in RULE_REGISTRY}
    seq = multiplier_picker_sequence()

    def picker(applicable, s):
        names = {r["name"] for r in applicable}
        for n, p in seq:
            if n in names and rbn[n].apply(s, p) != s:
                return {"rule_name": n, "params": p}
        return {"rule_name": applicable[0]["name"], "params": {}}

    return engine_run(formal_spec_to_engine_spec(multiplier_formal_spec()),
                      picker, run_id=run_id, max_steps=max_steps)


def _multiplier_verilog():
    refined = _converge()
    rtl = engine_spec_to_rtl_tla(refined, "multiplier",
                                 port_widths=_MUL_PORT_WIDTHS, reset_port="reset")
    return refined, RTLTLACompiler(rtl, reset_port="reset").compile(module_name="multiplier")


# ---------------------------------------------------------------------------
# Refinement
# ---------------------------------------------------------------------------

def test_multiplier_converges_with_existing_rules():
    """A single clocked Step + a combinational done reaches RTL-style with only
    Initialization + Iteration — the simplest medium-design refinement."""
    refined = _converge()
    assert is_rtl_style(refined) is True
    clocked = {a["name"] for a in refined["actions"] if a.get("clocked")}
    assert clocked == {"Step"}                      # one clocked datapath step
    comb = {v["name"] for v in refined["variables"] if v.get("combinational")}
    assert comb == {"done"}                         # done is combinational


# ---------------------------------------------------------------------------
# Codegen
# ---------------------------------------------------------------------------

def test_multiplier_codegen_shape():
    _, v = _multiplier_verilog()
    # 16-bit product accumulator + a combinational done flag.
    assert "output reg [15:0] product" in v
    assert "assign done = state == 2;" in v
    assert "output done" in v and "output reg done" not in v
    # the arithmetic shift/bit primitives survive translation
    assert "mcand * 2" in v                          # shift mcand left
    assert "mplier / 2" in v                         # shift mplier right
    assert "mplier % 2" in v                          # low-bit test
    assert "product + mcand" in v                     # conditional accumulate
    assert "always @(posedge clk)" in v
    assert "if (reset)" in v
    verify_banlist(v)


def test_multiplier_chains_are_flat_else_if():
    """Every guarded next-state must be one nested ternary (flat ELSE-IF chain),
    with no leaked TLA+ IF/THEN/ELSE keyword."""
    _, v = _multiplier_verilog()
    assert "state <= (" in v and "product <= (" in v
    for kw in (" IF ", " THEN ", " ELSE "):
        assert kw not in v, f"leaked TLA+ keyword {kw!r} in:\n{v}"


@pytest.mark.skipif(not shutil.which("iverilog"), reason="iverilog not installed")
def test_multiplier_lints_clean(tmp_path):
    _, v = _multiplier_verilog()
    p = tmp_path / "multiplier.v"
    p.write_text(v)
    r = subprocess.run(["iverilog", "-Wall", "-t", "null", str(p)],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"lint failed:\n{r.stdout}\n{r.stderr}\n\n{p.read_text()}"


# ---------------------------------------------------------------------------
# Function — independent spec_sim and a real cocotb run
# ---------------------------------------------------------------------------

def _stim_for(pairs):
    """One start cycle + 9 idle cycles per (a,b); done pulses at index 8 of each."""
    stim = []
    for a, b in pairs:
        stim.append({"start": 1, "a": a, "b": b})
        stim.extend({"start": 0, "a": 0, "b": 0} for _ in range(9))
    return stim


def test_multiplier_spec_sim_computes_products():
    """The independent interpreter computes the correct 16-bit product, with done
    pulsing on the completion cycle, across normal/maximum/zero operands."""
    refined = _converge()
    pairs = [(12, 11), (255, 255), (0, 200), (1, 1), (7, 9)]
    stim = _stim_for(pairs)
    out = derive_expected(refined, stim, ["product", "done"],
                          reset_port="reset", reset_active_low=False)
    for i, (a, b) in enumerate(pairs):
        done_v = out[i * 10 + 8]                      # completion cycle of block i
        assert done_v == {"product": a * b, "done": 1}, (
            f"{a}*{b}: got {done_v}")
        # done is low on the cycle before completion (still BUSY)
        assert out[i * 10 + 7]["done"] == 0


def test_multiplier_handshake_accepts_start_in_done_back_to_back():
    """REGRESSION (first live run): a start pulse that lands in the 1-cycle DONE
    state must RELOAD (back-to-back), not be silently dropped. The hardened load
    guard fires on start while NOT busy — IDLE or DONE. Here the second start
    lands exactly in the first multiply's DONE cycle (index 9, since done pulses
    at index 8); both multiplies must complete with correct products.

    With the old IDLE-only guard the second start was eaten by the DONE->IDLE
    transition and the product stayed stuck at the first result — the exact live
    failure (255*255 never ran). This test would catch that regression."""
    refined = _converge()
    a1, b1, a2, b2 = 13, 11, 255, 255
    stim = (
        [{"start": 1, "a": a1, "b": b1}] + [{"start": 0, "a": 0, "b": 0}] * 8  # done @ v8
        + [{"start": 1, "a": a2, "b": b2}] + [{"start": 0, "a": 0, "b": 0}] * 9  # start @ v9 (DONE)
    )
    out = derive_expected(refined, stim, ["product", "done"],
                          reset_port="reset", reset_active_low=False)
    done_cycles = [i for i, o in enumerate(out) if o.get("done") == 1]
    assert done_cycles == [8, 17], (
        f"expected done pulses at v8 and v17 (back-to-back), got {done_cycles} — "
        "a single pulse means the second start was dropped (the live bug)")
    assert out[8]["product"] == a1 * b1     # 13*11 = 143
    assert out[17]["product"] == a2 * b2    # 255*255 = 65025, the multiply the live run dropped


@pytest.mark.skipif(not _HAVE_COCOTB, reason="iverilog + cocotb-config required")
def test_multiplier_cocotb(tmp_path):
    """Headline proof: the generated RTL PASSes a REAL cocotb run, driven by the
    Stage-2 generator off the fixture stimulus and asserted against the spec
    reference (product + done per cycle, three multiplications incl. 255*255)."""
    from pipeline.cocotb.generator import generate_testbench
    from pipeline.cocotb.runner import run_testbench

    _, v = _multiplier_verilog()
    vp = tmp_path / "output.v"
    vp.write_text(v)
    tb = tmp_path / "02_testbench.py"
    generate_testbench(multiplier_summary(), tb)
    result = run_testbench(tb, vp, "multiplier")
    assert result.get("status") == "pass", (
        "multiplier RTL failed cocotb (FSM + multi-cycle datapath):\n"
        f"phase={result.get('phase')} error={result.get('error')}\n"
        f"{result.get('raw', '')[-2000:]}"
    )
