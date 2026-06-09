"""
Regression tests for the second live accumulator run (2026-06-09, run 114009-883d7e).

That run did NOT false-green (RC5 + RC4 from the prior run both held: Agent 3 modelled
only `acc` as a variable, and the port-direction gate caught the bad output). Instead it
exposed two NEW deterministic defects, diagnosed + fixed via an ultracode workflow:

  PRIMARY — refinement STALL on a pure register-hold action.
    The FormalSpec had a dedicated `Hold` action (`acc' = acc`). is_rtl_style() required
    EVERY non-reset action to be clocked, but the live Rule Picker never iterated Hold,
    so the engine backtracked to empty and stalled -> fell back to abstract Compiler-1
    TLA+ -> Compiler 2 degenerated `acc` into a bare `input` with an empty module body.
    Fix is a verified PAIR (each alone is insufficient):
      (1) engine.is_rtl_style skips identity-only holds (no longer require them clocked);
      (2) bridge.engine_spec_to_rtl_tla drops identity-only actions from CombinationalLogic
          (else the un-iterated hold double-drives the register -> MultiDriverError).

  SECONDARY — the word operator `mod` (masked by the stall).
    Agent 3 wrote `(acc + din) mod 256`. `mod` is not valid TLA+ and was not translated,
    so it leaked a phantom `input mod` port and emitted invalid Verilog. Fix: fold
    `mod` -> `%` at the same word-boundary as AND/OR/NOT (bridge + a defensive copy in
    Compiler 2), plus an Agent 3 prompt nudge.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from pipeline.compilers.compiler2 import RTLTLACompiler
from pipeline.refinement.bridge import (
    _is_identity_hold,
    _scan_identifiers,
    _translate_bool_words,
    engine_spec_to_rtl_tla,
    formal_spec_to_engine_spec,
)
from pipeline.refinement.engine import _replay_chain, is_rtl_style
from pipeline.schemas.tla_schema import FormalSpec

_PORT_WIDTHS = {"clk": 1, "rst_n": 1, "en": 1, "din": 8, "acc": 8}

# A 2-step chain that clocks ONLY Accumulate (Hold left un-iterated) — exactly what the
# live Rule Picker committed. Before the fix this stalled / MultiDriverError'd.
_CHAIN = [
    {"rule_name": "Initialization",
     "params": {"reset_values": {"acc": "0"}, "reset_action_name": "Reset"}},
    {"rule_name": "Iteration", "params": {"action_name": "Accumulate"}},
]


def _accumulator_with_hold_spec() -> FormalSpec:
    """The shape Agent 3 authored live: 3 actions incl. a dedicated identity Hold,
    and `mod` (the word) in the Accumulate update."""
    return FormalSpec(
        module_name="accumulator_8bit",
        description="8-bit accumulator with a dedicated Hold action and `mod` modulo.",
        variables={"acc": {"type": "Nat", "width": 8}},
        initial={"acc": "0"},
        transitions=[
            {"label": "Reset", "condition": "rst_n = 0", "updates": {"acc": "0"}},
            {"label": "Accumulate", "condition": "rst_n = 1 AND en = 1",
             "updates": {"acc": "(acc + din) mod 256"}},
            {"label": "Hold", "condition": "rst_n = 1 AND en = 0",
             "updates": {"acc": "acc"}},
        ],
        invariants=["acc >= 0 AND acc <= 255"],
    )


def _build_verilog() -> str:
    eng = formal_spec_to_engine_spec(_accumulator_with_hold_spec())
    refined = _replay_chain(eng, _CHAIN)
    rtl = engine_spec_to_rtl_tla(
        refined, "accumulator_8bit", port_widths=_PORT_WIDTHS,
        reset_port="rst_n", reset_active_low=True,
    )
    return RTLTLACompiler(rtl, reset_port="rst_n", reset_active_low=True).compile(
        module_name="accumulator_8bit"
    )


# ---------------------------------------------------------------------------
# _is_identity_hold predicate (the shared primitive of both halves of the fix)
# ---------------------------------------------------------------------------

def test_is_identity_hold_predicate():
    assert _is_identity_hold({"name": "Hold", "updates": [{"variable": "acc", "expression": "acc"}]}) is True
    assert _is_identity_hold({"name": "Hold", "updates": [{"variable": "acc", "expression": " acc "}]}) is True
    assert _is_identity_hold({"name": "Acc", "updates": [{"variable": "acc", "expression": "(acc + din) % 256"}]}) is False
    assert _is_identity_hold({"name": "Empty", "updates": []}) is False
    # An action carrying branches/sequential_steps is real logic, never a pure hold.
    assert _is_identity_hold({"name": "Alt", "updates": [{"variable": "acc", "expression": "acc"}], "branches": [{}]}) is False


# ---------------------------------------------------------------------------
# PRIMARY — the pair fix: converges (no stall) AND compiles clean (no double-drive)
# ---------------------------------------------------------------------------

def test_identity_hold_converges_and_compiles_clean():
    """Pins BOTH halves of the pair (each alone fails):
      - is_rtl_style relaxation: the assert below is False without it (-> stall);
      - bridge comb-filter: compile() raises MultiDriverError without it.
    """
    eng = formal_spec_to_engine_spec(_accumulator_with_hold_spec())
    refined = _replay_chain(eng, _CHAIN)

    # (1) Hold stays unclocked, yet the spec is RTL-style — convergence no longer
    # depends on the picker iterating the redundant Hold.
    clocked = {a["name"]: a.get("clocked", False) for a in refined["actions"]}
    assert clocked["Accumulate"] is True
    assert clocked["Hold"] is False
    assert is_rtl_style(refined) is True

    # (2) Codegen does not double-drive acc; the module is correct, not degenerate.
    verilog = _build_verilog()
    assert "output reg [7:0] acc" in verilog       # acc is the OUTPUT register...
    assert "input  acc" not in verilog             # ...never a bare input (the bug)
    assert "din" in verilog and "en" in verilog    # data input + enable present
    assert "always @(posedge clk)" in verilog      # real clocked logic, not empty
    assert "% 256" in verilog                       # mod -> % reached codegen
    assert " mod " not in verilog                   # no leaked word operator


# ---------------------------------------------------------------------------
# SECONDARY — mod -> % word-operator translation
# ---------------------------------------------------------------------------

def test_mod_word_operator_translated():
    assert _translate_bool_words("(acc + din) mod 256") == "(acc + din) % 256"
    # no phantom 'mod' identifier survives to the free-input scanner
    assert "mod" not in _scan_identifiers(_translate_bool_words("(acc + din) mod 256"))
    assert _scan_identifiers(_translate_bool_words("(acc + din) mod 256")) == {"acc", "din"}
    # word-boundary safe: substrings of identifiers are untouched
    assert _translate_bool_words("modcount + 1") == "modcount + 1"
    assert _translate_bool_words("a_mod + model") == "a_mod + model"
    # idempotent on already-symbolic input
    assert _translate_bool_words("(a + b) % 4") == "(a + b) % 4"
    # repeats handled
    assert _translate_bool_words("a mod 4 mod 2") == "a % 4 % 2"


# ---------------------------------------------------------------------------
# Full-stack lint guard (the byte-level check that would have caught both bugs)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("iverilog"), reason="iverilog not installed")
def test_identity_hold_accumulator_lints_clean(tmp_path):
    verilog = _build_verilog()
    p = tmp_path / "acc.v"
    p.write_text(verilog)
    r = subprocess.run(
        ["iverilog", "-Wall", "-t", "null", str(p)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"lint failed:\n{r.stdout}\n{r.stderr}\n\n{verilog}"
