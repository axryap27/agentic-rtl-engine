"""
End-to-end proof of the VERIFIED-DERIVATION chain.

Every prior FSMD test (test_fsmd_multiplier) hands the engine an already-scheduled
multiplier and only adds reset + clocking. THIS file proves the engine can take an
ABSTRACT Morgan spec statement — one transition whose postcondition is
``product = a * b`` over a still-abstract ``product`` — and DERIVE the verified,
compilable, cocotb-passing FSMD multiplier, with the soundness coming from a real
proof, not from trusting the proposer:

  derivation — the chain LoopIntroduction -> ScheduleHandshakeFSM -> Initialization
               converges to RTL-style. LoopIntroduction discharges the iteration-
               rule obligations (O1 init establishes the invariant, O2 body
               maintains it and decreases the variant, O3 exit establishes
               product = a*b) by EXHAUSTIVE PROOF over the input domain; the
               refinement_chain records the discharged obligations — this recorded
               {O1,O2,O3} = True audit IS the verified-derivation certificate.
  codegen    — the DERIVED RTL (via the mechanical scheduler) is banlist-clean,
               has the same shape as the hand-written FSMD, lints clean, and PASSes
               a REAL cocotb run on the multiplier stimulus — the SAME asserts as
               test_fsmd_multiplier::test_multiplier_cocotb.
  soundness  — the NEGATIVE control: a picker that proposes a WRONG invariant
               (product + mplier*mcand = a + b) makes LoopIntroduction a NO-OP
               (the obligation kernel rejects it), so the engine cannot converge —
               the chain never reaches RTL-style. The kernel rejecting a bad
               refinement, observed end to end.

The cocotb interface is identical to the concrete multiplier (same ports, stimulus,
expected trace), so the DERIVED RTL is verified against the very same vectors.
"""

from __future__ import annotations

import copy
import shutil
import subprocess

import pytest

from pipeline.compilers.compiler2 import RTLTLACompiler, verify_banlist
from pipeline.refinement.bridge import formal_spec_to_engine_spec, engine_spec_to_rtl_tla
from pipeline.refinement.engine import (
    is_rtl_style, run as engine_run, RULE_REGISTRY, RefinementStall,
)
from tests.fixtures.medium_designs import (
    abstract_multiplier_formal_spec,
    abstract_multiplier_picker_sequence,
    abstract_multiplier_summary,
    _ABS_MUL_LOOP_PARAMS,
)

_HAVE_COCOTB = (shutil.which("cocotb-config") is not None
                and shutil.which("iverilog") is not None)

# The cocotb interface is 8-bit (the obligation PROOF uses 6-bit for speed; the
# invariant is width-generic and count loads 8, so the derived datapath is 8-bit).
_MUL_PORT_WIDTHS = {"clk": 1, "reset": 1, "start": 1, "a": 8, "b": 8}


def _make_picker(sequence):
    """An applicability/no-op-aware picker over a fixed derivation sequence.

    For each step it returns the first listed (rule, params) that is BOTH applicable
    AND not a no-op on the current spec. The no-op guard is load-bearing for the
    negative control: a LoopIntroduction whose obligations FAIL returns an unchanged
    deepcopy, so this picker skips it (it never advances the chain) — exactly how the
    engine experiences the kernel's rejection.
    """
    rule_by_name = {r.__class__.__name__: r for r in RULE_REGISTRY}

    def picker(applicable, spec):
        names = {r["name"] for r in applicable}
        for name, params in sequence:
            if name in names and rule_by_name[name].apply(spec, params) != spec:
                return {"rule_name": name, "params": params}
        # No productive listed step — fall through to the first applicable rule so
        # the engine makes SOME move (and ultimately stalls if nothing converges).
        return {"rule_name": applicable[0]["name"], "params": {}}

    return picker


def _derive(run_id, sequence=None, max_steps=24):
    """Drive the REAL engine from the abstract product=a*b spec to RTL-style."""
    seq = sequence if sequence is not None else abstract_multiplier_picker_sequence()
    eng0 = formal_spec_to_engine_spec(abstract_multiplier_formal_spec())
    return engine_run(eng0, _make_picker(seq), run_id=run_id, max_steps=max_steps)


def _derived_verilog(run_id="verified_deriv_codegen"):
    refined = _derive(run_id)
    rtl = engine_spec_to_rtl_tla(refined, "shift_add_multiplier",
                                 port_widths=_MUL_PORT_WIDTHS, reset_port="reset")
    v = RTLTLACompiler(rtl, reset_port="reset").compile(module_name="shift_add_multiplier")
    return refined, v


# ---------------------------------------------------------------------------
# Derivation — the chain converges and records the discharged obligations
# ---------------------------------------------------------------------------

def test_abstract_spec_statement_makes_loop_introduction_applicable():
    """The bridge marks the spec-statement target abstract AND carries the marker
    onto the action, so LoopIntroduction fires on the freshly-bridged abstract spec
    (and ScheduleHandshakeFSM does not yet — there is no verified loop to schedule)."""
    from pipeline.refinement.rules.loop_introduction import LoopIntroduction
    from pipeline.refinement.rules.schedule_handshake_fsm import ScheduleHandshakeFSM
    eng0 = formal_spec_to_engine_spec(abstract_multiplier_formal_spec())
    act = next(a for a in eng0["actions"] if a["name"] == "Multiply")
    assert act.get("spec_statement") is True
    assert act.get("postcondition") == "product = a * b"
    product = next(v for v in eng0["variables"] if v["name"] == "product")
    assert product["abstract"] is True
    assert LoopIntroduction().is_applicable(eng0) is True
    assert ScheduleHandshakeFSM().is_applicable(eng0) is False


def test_verified_derivation_converges_to_rtl_style():
    """abstract product=a*b -> LoopIntroduction -> ScheduleHandshakeFSM ->
    Initialization converges to RTL-style: one clocked datapath action (Multiply,
    now the scheduled FSMD), the combinational `done`, and the derived loop
    registers + control state, all concrete."""
    refined = _derive("verified_deriv_converge")
    assert is_rtl_style(refined) is True

    clocked = {a["name"] for a in refined["actions"] if a.get("clocked")}
    assert clocked == {"Multiply"}             # the scheduled datapath step

    comb = {v["name"] for v in refined["variables"] if v.get("combinational")}
    assert comb == {"done"}                    # done is combinational

    # the derived loop registers + control state are all present and concrete
    var_by_name = {v["name"]: v for v in refined["variables"]}
    for name in ("product", "mcand", "mplier", "count", "state"):
        assert name in var_by_name, f"missing derived register {name}"
        assert var_by_name[name]["abstract"] is False, f"{name} left abstract"
    # the abstract spec-statement marker is gone (it became a concrete datapath)
    mult = next(a for a in refined["actions"] if a["name"] == "Multiply")
    assert "spec_statement" not in mult
    assert "loop" not in mult                  # the scheduler cleared the marker


def test_refinement_records_discharged_obligations():
    """THE VERIFIED-DERIVATION PROOF. The refinement audit recorded on the derived
    action must show all three iteration-rule obligations DISCHARGED — by exhaustive
    proof over the input domain — not merely asserted. This recorded {O1,O2,O3}=True
    is the certificate that the loop is a SOUND refinement of product = a*b."""
    refined = _derive("verified_deriv_obligations")
    mult = next(a for a in refined["actions"] if a["name"] == "Multiply")
    audit = mult["refinement"]
    assert audit["obligations"] == {"O1": True, "O2": True, "O3": True}, (
        f"obligations not all discharged: {audit['obligations']}")
    assert audit["mode"] == "exhaustive-proof"
    assert audit["cases_checked"] == 4096       # 2^(6+6) (a,b) pairs
    assert audit["invariant"] == "product + mplier * mcand = a * b"
    assert audit["variant"] == "count"


def test_refinement_chain_records_loop_introduction_with_obligations():
    """The on-disk refinement_chain records LoopIntroduction as the first derivation
    step with its full params, and replaying it reproduces the discharged-obligation
    audit — the proof is reconstructible from the artifact, not just in memory."""
    from pipeline.refinement.engine import _load_chain, _replay_chain

    run_id = "verified_deriv_chain"
    refined = _derive(run_id)
    chain = _load_chain(run_id)
    names = [step["rule_name"] for step in chain]
    assert names == ["LoopIntroduction", "ScheduleHandshakeFSM", "Initialization"]
    assert chain[0]["params"]["invariant"] == "product + mplier * mcand = a * b"

    # replay from the abstract spec reproduces the same discharged obligations
    eng0 = formal_spec_to_engine_spec(abstract_multiplier_formal_spec())
    replayed = _replay_chain(eng0, chain)
    mult = next(a for a in replayed["actions"] if a["name"] == "Multiply")
    assert mult["refinement"]["obligations"] == {"O1": True, "O2": True, "O3": True}


# ---------------------------------------------------------------------------
# Codegen — the DERIVED RTL is clean and lints (mirrors test_fsmd_multiplier)
# ---------------------------------------------------------------------------

def test_derived_codegen_shape():
    _, v = _derived_verilog()
    assert "output reg [15:0] product" in v
    assert "assign done = state == 2;" in v
    assert "output done" in v and "output reg done" not in v
    # the arithmetic shift/bit primitives survive translation
    assert "mcand * 2" in v                          # shift mcand left
    assert "mplier / 2" in v                          # shift mplier right
    assert "mplier % 2" in v                          # low-bit test
    assert "product + mcand" in v                     # conditional accumulate
    assert "always @(posedge clk)" in v
    assert "if (reset)" in v
    verify_banlist(v)


def test_derived_chains_are_flat_else_if():
    """Every guarded next-state is one nested ternary (flat ELSE-IF chain), with no
    leaked TLA+ IF/THEN/ELSE keyword — including the FLATTENED conditional body
    (the product accumulate)."""
    _, v = _derived_verilog()
    assert "state <= (" in v and "product <= (" in v
    for kw in (" IF ", " THEN ", " ELSE "):
        assert kw not in v, f"leaked TLA+ keyword {kw!r} in:\n{v}"


@pytest.mark.skipif(not shutil.which("iverilog"), reason="iverilog not installed")
def test_derived_lints_clean(tmp_path):
    _, v = _derived_verilog()
    p = tmp_path / "shift_add_multiplier.v"
    p.write_text(v)
    r = subprocess.run(["iverilog", "-Wall", "-t", "null", str(p)],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"lint failed:\n{r.stdout}\n{r.stderr}\n\n{p.read_text()}"


# ---------------------------------------------------------------------------
# Function — a REAL cocotb run on the DERIVED RTL (same asserts as the FSMD test)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAVE_COCOTB, reason="iverilog + cocotb-config required")
def test_derived_multiplier_cocotb(tmp_path):
    """Headline proof: the VERIFIED-DERIVED RTL PASSes a REAL cocotb run, driven by
    the Stage-2 generator off the (identical) multiplier stimulus and asserted
    against the spec reference (product + done per cycle, three multiplications
    incl. 255*255). Same asserts as test_fsmd_multiplier::test_multiplier_cocotb,
    but the RTL was DERIVED from product=a*b, not hand-written."""
    from pipeline.cocotb.generator import generate_testbench
    from pipeline.cocotb.runner import run_testbench

    _, v = _derived_verilog("verified_deriv_cocotb")
    vp = tmp_path / "output.v"
    vp.write_text(v)
    tb = tmp_path / "02_testbench.py"
    generate_testbench(abstract_multiplier_summary(), tb)
    result = run_testbench(tb, vp, "shift_add_multiplier")
    assert result.get("status") == "pass", (
        "verified-derived multiplier RTL failed cocotb:\n"
        f"phase={result.get('phase')} error={result.get('error')}\n"
        f"{result.get('raw', '')[-2000:]}"
    )


# ---------------------------------------------------------------------------
# Soundness — the kernel rejecting a BAD refinement, end to end (NEGATIVE control)
# ---------------------------------------------------------------------------

def test_wrong_invariant_makes_loop_introduction_a_noop():
    """A picker that proposes a WRONG invariant (product + mplier*mcand = a + b, not
    the shift-add identity) fails the obligations, so LoopIntroduction returns an
    unchanged deepcopy — a no-op. This is the per-rule basis for the engine-level
    stall below."""
    from pipeline.refinement.rules.loop_introduction import LoopIntroduction
    bad_params = copy.deepcopy(_ABS_MUL_LOOP_PARAMS)
    bad_params["invariant"] = "product + mplier * mcand = a + b"
    eng0 = formal_spec_to_engine_spec(abstract_multiplier_formal_spec())
    assert LoopIntroduction().apply(eng0, bad_params) == eng0


def test_wrong_invariant_does_not_reach_rtl_style():
    """END-TO-END NEGATIVE CONTROL: a picker whose LoopIntroduction carries the wrong
    invariant cannot advance the chain (the kernel rejects it -> no-op), so the
    engine cannot reach RTL-style and stalls. The chain does NOT converge — the
    obligation kernel rejecting an unsound refinement, observed end to end.

    Contrast with test_verified_derivation_converges_to_rtl_style: the ONLY
    difference is the invariant, and it is the difference between a verified
    derivation and a rejected one."""
    bad_params = copy.deepcopy(_ABS_MUL_LOOP_PARAMS)
    bad_params["invariant"] = "product + mplier * mcand = a + b"
    bad_sequence = (
        [("LoopIntroduction", bad_params)]
        + abstract_multiplier_picker_sequence()[1:]   # schedule + init, unchanged
    )
    with pytest.raises(RefinementStall):
        _derive("verified_deriv_negative", sequence=bad_sequence)

    # And, directly: the spec the picker can actually produce never satisfies the
    # RTL-style predicate (nothing past the rejected LoopIntroduction can fire).
    eng0 = formal_spec_to_engine_spec(abstract_multiplier_formal_spec())
    assert is_rtl_style(eng0) is False
