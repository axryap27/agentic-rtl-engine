"""
Regression for the traffic-light FSM run (run 9a77ce279bfb) that produced a
`partial` artifact: cocotb FAILED vector 0 (expected state=0, got 2) and the
refinement critic REJECTED a sanctioned reset action. Three independent root
causes, all confirmed offline:

  RC1 — ACTIVE-LOW RESET POLARITY DROPPED IN CODEGEN.
        summary.reset_active_low=True / reset_port="rst_n", but the emitted
        Verilog hardcoded `if (rst_n)` (active-HIGH) — it reset when rst_n=1
        (reset INACTIVE). The polarity flag was read in Stage 1 but never threaded
        into the reverse bridge or Compiler 2. Fix: thread reset_active_low so the
        bridge emits `IF rst_n = 0 THEN` and Compiler 2 emits `if (!rst_n)`.

  RC2 — AGENT 1 MODELLED clk AS A TOGGLING TEST-VECTOR INPUT, violating the
        cocotb generator's 1-tick-per-vector contract. The harness owns a
        free-running Clock and does exactly ONE RisingEdge per vector, so a spec
        that toggles clk 0,1,0,1 (expecting half-rate advance) mismatches.
        Fix: (PRIMARY) a clock-contract block in Agent 1's system prompt;
        (DEFENSIVE) the generator no longer drives clk per-vector.

  RC3 — REFINEMENT CRITIC FALSE-REJECTED the Initialization-introduced Reset
        action ("no abstract counterpart", "references rst not in abstract spec",
        "asynchronous override path"). But that Reset is a sanctioned Table-1
        refinement (a universal hardware reset primitive). Fix: a NARROW carve-out
        in the critic SYSTEM prompt accepting a reset action that forces every
        state variable to its declared init value (and nothing else), while
        preserving every genuine check.

These tests reproduce RC1 through the REAL bridge + Compiler 2 (not a hand-written
module), drive the corrected 1-tick-per-vector summary through the REAL cocotb
generator + runner, and pin the RC2 prompt / RC3 carve-out text plus the
genuine-reject halt.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pipeline.refinement.bridge import (
    formal_spec_to_engine_spec,
    engine_spec_to_rtl_tla,
)
from pipeline.refinement.engine import _replay_chain
from pipeline.compilers.compiler2 import RTLTLACompiler, verify_banlist
from pipeline.schemas.tla_schema import FormalSpec


# --- The captured FSM FormalSpec from run 9a77ce279bfb -----------------------
# (status / critic_verdict fields stripped — this is the clean spec body.)

CAPTURED_FSM_SPEC = {
    "module_name": "traffic_light_fsm",
    "description": (
        "A finite state machine that cycles through traffic light states: "
        "Red (0) -> Green (2) -> Yellow (1) -> Red (0). Active-low reset forces Red."
    ),
    "variables": {"state": {"type": "Nat", "width": 2}},
    "initial": {"state": "0"},
    "transitions": [
        {"label": "RedToGreen",    "condition": "rst_n = 1 AND state = 0",
         "updates": {"state": "2"}},
        {"label": "GreenToYellow", "condition": "rst_n = 1 AND state = 2",
         "updates": {"state": "1"}},
        {"label": "YellowToRed",   "condition": "rst_n = 1 AND state = 1",
         "updates": {"state": "0"}},
    ],
    "invariants": ["state = 0 OR state = 1 OR state = 2", "state /= 3"],
    "raw_tla": None,
}

# The GOOD refinement chain (Initialization + Iteration x3) — exactly the first
# four steps of the captured refinement_chain.json (before the replay-discontinuity
# duplicate restart). Replayed deterministically to reach RTL-style.
GOOD_CHAIN = [
    {"rule_name": "Initialization",
     "params": {"reset_values": {"state": "0"}, "reset_action_name": "Reset"}},
    {"rule_name": "Iteration", "params": {"action_name": "RedToGreen"}},
    {"rule_name": "Iteration", "params": {"action_name": "GreenToYellow"}},
    {"rule_name": "Iteration", "params": {"action_name": "YellowToRed"}},
]


def _refined_fsm_engine_spec() -> dict:
    """Replay the good chain on the captured spec to the RTL-style engine spec."""
    es = formal_spec_to_engine_spec(FormalSpec.model_validate(CAPTURED_FSM_SPEC))
    return _replay_chain(es, GOOD_CHAIN)


# ---------------------------------------------------------------------------
# RC1 (a) — codegen reset polarity unit test
# ---------------------------------------------------------------------------

def test_rc1_active_low_emits_negated_reset():
    """reset_active_low=True must emit `IF rst_n = 0 THEN` (bridge) and
    `if (!rst_n)` (Compiler 2); active-high (default) must stay `if (rst_n)`.
    """
    refined = _refined_fsm_engine_spec()

    # Active-low path.
    tla_low = engine_spec_to_rtl_tla(
        refined, "traffic_light_fsm",
        port_widths={"rst_n": 1}, reset_port="rst_n", reset_active_low=True,
    )
    assert "IF rst_n = 0 THEN" in tla_low, tla_low
    assert "IF rst_n = 1 THEN" not in tla_low, tla_low
    v_low = RTLTLACompiler(
        tla_low, reset_port="rst_n", reset_active_low=True
    ).compile("traffic_light_fsm")
    verify_banlist(v_low)  # `!rst_n` must not trip the SystemVerilog banlist.
    assert "if (!rst_n) begin" in v_low, v_low
    # The reset port is a real input, never a bogus register.
    assert "input  rst_n" in v_low or "input rst_n" in v_low, v_low
    assert "output reg" not in v_low.split("rst_n")[0] or "reg rst_n" not in v_low

    # Active-high path (default reset_active_low=False) — must NOT negate.
    tla_hi = engine_spec_to_rtl_tla(
        refined, "traffic_light_fsm",
        port_widths={"rst_n": 1}, reset_port="rst_n",
    )
    assert "IF rst_n = 1 THEN" in tla_hi, tla_hi
    v_hi = RTLTLACompiler(tla_hi, reset_port="rst_n").compile("traffic_light_fsm")
    verify_banlist(v_hi)
    assert "if (rst_n) begin" in v_hi, v_hi
    assert "if (!rst_n)" not in v_hi, v_hi


# ---------------------------------------------------------------------------
# RC1 + RC2 (b) — HEADLINE offline cocotb end-to-end
# ---------------------------------------------------------------------------

# The corrected, 1-tick-per-vector expected sequence. The cocotb generator's
# reset preamble asserts rst_n=0 (state->0), then deasserts rst_n=1 and applies
# ONE rising edge — during which the FSM already advances 0->2. So when vector 0
# runs, state is already 2 and that vector's edge advances 2->1. Hence the
# harness-observed post-edge sequence for 10 vectors (clk held constant, rst_n=1)
# is [1,0,2,1,0,2,1,0,2,1]. This is what the REAL generator produces — derived by
# simulating the emitted active-low module against the generator's actual preamble,
# NOT hand-assumed.
FSM_EXPECTED_SEQUENCE = [1, 0, 2, 1, 0, 2, 1, 0, 2, 1]

CORRECTED_FSM_SUMMARY = {
    "module_name": "traffic_light_fsm",
    "description": "Traffic-light FSM, 1 clock tick per vector (clk held at 1).",
    "ports": [
        {"name": "clk",   "direction": "input",  "width": 1},
        {"name": "rst_n", "direction": "input",  "width": 1},
        {"name": "state", "direction": "output", "width": 2},
    ],
    # clk held CONSTANT at 1 every vector (RC2 contract); one edge per vector.
    "test_vectors": [
        {"inputs": {"clk": 1, "rst_n": 1}, "expected": {"state": s}}
        for s in FSM_EXPECTED_SEQUENCE
    ],
    "reset_port": "rst_n",
    "reset_active_low": True,
    "status": "success",
}


def test_rc1_rc2_fsm_passes_cocotb(tmp_path):
    """End-to-end closure for RC1+RC2: the active-low Verilog emitted by the REAL
    bridge + Compiler 2 from the captured FSM spec, driven by a testbench the REAL
    cocotb generator produces from a corrected 1-tick-per-vector summary, passes
    cocotb. Skipped gracefully when the sim toolchain is absent (like the counter
    repro).
    """
    pytest.importorskip("cocotb", reason="cocotb not installed")
    if shutil.which("iverilog") is None or shutil.which("vvp") is None:
        pytest.skip("iverilog/vvp not installed")

    from pipeline.cocotb.generator import generate_testbench
    from pipeline.cocotb.runner import run_testbench
    from pipeline.schemas.summary_schema import SpecSummary

    # RC1: emit the active-low module through the real bridge + Compiler 2.
    refined = _refined_fsm_engine_spec()
    tla = engine_spec_to_rtl_tla(
        refined, "traffic_light_fsm",
        port_widths={"rst_n": 1}, reset_port="rst_n", reset_active_low=True,
    )
    verilog = RTLTLACompiler(
        tla, reset_port="rst_n", reset_active_low=True
    ).compile("traffic_light_fsm")
    rtl_path = tmp_path / "output.v"
    rtl_path.write_text(verilog)

    # RC2: generate the testbench from the corrected 1-tick-per-vector summary.
    summary = SpecSummary.model_validate(CORRECTED_FSM_SUMMARY)
    tb_path = tmp_path / "02_testbench.py"
    generate_testbench(summary, tb_path)

    # RC2 defensive change: the generator must NOT drive clk per-vector.
    tb_src = tb_path.read_text()
    assert "dut.clk.value" not in tb_src, (
        "generator must not drive clk per-vector (harness owns the clock)"
    )

    result = run_testbench(tb_path, rtl_path, "traffic_light_fsm")
    assert result["status"] == "pass", json.dumps(result, indent=2)[:1500]


# ---------------------------------------------------------------------------
# RC3 (c) — critic carve-out present AND genuine reject still halts
# ---------------------------------------------------------------------------

def test_rc3_carveout_language_present():
    """The pass6_checker SYSTEM prompt must carry the sanctioned-reset carve-out
    AND still restate the genuine checks it must not relax.
    """
    from pipeline.refinement_templates import pass6_checker

    sys_prompt = pass6_checker.SYSTEM
    # Carve-out is present and names the Initialization rule.
    assert "SANCTIONED REFINEMENT" in sys_prompt
    assert "Initialization" in sys_prompt
    assert "DO NOT RE-LITIGATE" in sys_prompt
    # Collapse line-wrap whitespace so wrapped phrases match as substrings.
    flat = " ".join(sys_prompt.split())
    # The three false-reject legs from the live verdict are explicitly neutralized.
    assert "no abstract counterpart" in flat
    assert "stuttering step" in flat
    assert "unmapped reset signal" in flat
    assert "clocked=false" in flat
    # The genuine checks are NOT relaxed — narrowing clauses are restated.
    assert "first-wins" in flat
    assert "dropped or weakened a guard" in flat
    assert "OTHER than a variable's declared initial/reset value" in flat


def _first_wins_reject(abstract, refined):
    """A GENUINE reject the carve-out must NOT suppress (first-wins collapse)."""
    return {
        "verdict": "reject",
        "issues": [
            "A non-reset transition collapsed multi-branch next-state logic into "
            "a single first-wins assignment, dropping a guard."
        ],
        "reasoning": "Behavioral preservation fails: a real first-wins collapse.",
    }


def test_rc3_genuine_reject_still_halts(tmp_path, monkeypatch):
    """The carve-out is prompt-only; the stage3 critic GATE and the fail-closed
    normaliser are untouched. A genuine reject must still write `partial` and emit
    NO output.v, so the router halts.
    """
    from pipeline.nodes import stage3
    from pipeline.agents import agent3

    run_id = "repro_fsm_genuine_reject"
    art = tmp_path / "artifacts" / run_id
    art.mkdir(parents=True, exist_ok=True)
    (art / "01_summary.json").write_text(json.dumps(CORRECTED_FSM_SUMMARY))
    monkeypatch.chdir(tmp_path)

    spec = FormalSpec.model_validate(CAPTURED_FSM_SPEC)
    monkeypatch.setattr(agent3, "generate_formal_spec", lambda summary: spec)

    # A competent applicability-driven picker (mirrors the counter repro helper):
    # establish reset, then clock each colored transition.
    def picker(applicable_rules, spec, *, system_prompt=None):
        names = {r["name"] for r in applicable_rules}
        reset_action = spec.get("reset_action")
        if "Initialization" in names:
            needs_reset = reset_action is None or any(
                v.get("reset_value") is None for v in spec.get("variables", [])
            )
            if needs_reset:
                return {
                    "rule_name": "Initialization",
                    "params": {
                        "reset_values": {
                            v["name"]: "0" for v in spec.get("variables", [])
                        },
                        "reset_action_name": "Reset",
                    },
                }
        if "Iteration" in names:
            for a in spec.get("actions", []):
                if a["name"] != (reset_action or "Reset") and not a.get("clocked", False):
                    return {"rule_name": "Iteration",
                            "params": {"action_name": a["name"]}}
        raise ValueError("simulated decline: no constructive rule for this pass")

    monkeypatch.setattr(agent3, "pick_rule", picker)
    # GENUINE reject — must halt despite the new carve-out.
    monkeypatch.setattr(stage3, "_run_refinement_critic", _first_wins_reject)

    state = {"run_id": run_id, "retry_counts": {}, "halt": False,
             "last_diagnosis": None}
    stage3.run_stage3(state)

    rtl = json.loads((art / "03_rtl_output.json").read_text())
    assert rtl["status"] == "partial", (
        f"a genuine reject must halt with partial, got {rtl.get('status')}"
    )
    assert not (art / "output.v").exists(), (
        "no Verilog may be emitted when the critic genuinely rejects"
    )
    # The formal-spec artifact records the verdict for debugging.
    formal = json.loads((art / "02_formal_spec.json").read_text())
    assert formal.get("critic_verdict", {}).get("verdict") == "reject"


# ---------------------------------------------------------------------------
# RC2 (d) — Agent 1 clock-contract prompt text present
# ---------------------------------------------------------------------------

def test_rc2_agent1_prompt_has_clock_contract():
    """Agent 1's system prompt must carry the 1-tick-per-vector clock contract."""
    from pipeline.agents import agent1

    prompt = agent1._SYSTEM_PROMPT
    assert "CLOCK CONTRACT" in prompt
    assert "EXACTLY ONE rising clock edge" in prompt
    assert "hold it constant at 1" in prompt
    # Explicitly forbid the toggling pattern that caused the half-rate mismatch.
    assert "0,1,0,1" in prompt
    assert "do NOT assume the design advances every other vector" in prompt
