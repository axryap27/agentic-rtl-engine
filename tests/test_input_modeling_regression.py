"""
Regression tests for the live accumulator run (2026-06-08, run 223427-d7a921).

That run reached cocotb PASS but was a FALSE GREEN: Agent 3 modelled the data
input `din` and the enable `en` as STATE VARIABLES, so the reverse bridge emitted
them as `output reg`. The design therefore had no way to receive `din`, yet cocotb
— which force-drives the mis-declared output nets — passed anyway. The same run
also tripped the deferred revise-replay discontinuity (the cocotb-revise re-entry
appended a fresh chain onto the stale prefix, breaking replay).

Three fixes, each pinned here:

  Fix 1 — deterministic port-direction gate (pipeline/nodes/stage3.py). After
          codegen, the emitted module's port directions are checked against the
          spec summary; a summary `input` emitted as `output` is a violation that
          downgrades the RTL artifact to non-success instead of shipping it.

  Fix 2 — Agent 3 spec-authoring prompt (pipeline/agents/agent3.py) now forbids
          modelling data/control inputs as "variables".

  Fix 3 — the cocotb-revise path (run_stage3_revise_cocotb) clears the stale
          refinement chain (preserving it as *_pre_revise.json) so the re-authored
          spec produces a self-contained, replayable refinement_chain.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipeline.nodes.stage3 as stage3
from pipeline.agents import agent3
from pipeline.nodes.stage3 import (
    _parse_module_port_directions,
    _summary_port_directions,
    _verify_port_directions,
    run_stage3,
    run_stage3_revise_cocotb,
)
from pipeline.refinement.bridge import formal_spec_to_engine_spec
from pipeline.refinement.engine import _replay_chain, _spec_hash
from pipeline.schemas.summary_schema import SpecSummary
from pipeline.schemas.tla_schema import FormalSpec
from tests.fixtures.medium_designs import (
    accumulator_formal_spec,
    accumulator_picker_sequence,
    accumulator_summary,
)


# ===========================================================================
# Captured real artifacts from the live run (verbatim shapes)
# ===========================================================================

# The exact module header the live run emitted: din/en wrongly `output reg`.
_LIVE_BUGGY_VERILOG = """\
`timescale 1ns / 1ps

module accumulator_8bit (
    input  clk,
    input  rst_n,
    output reg [7:0] acc,
    output reg [7:0] din,
    output reg en
);

    always @(posedge clk) begin
        if (!rst_n) begin
            acc <= 0;
        end else begin
            acc <= ((rst_n == 1 && en == 1)) ? ((acc + din) % 256) : (acc);
        end
    end

endmodule
"""

# A correct accumulator interface: din/en are inputs.
_CORRECT_VERILOG = """\
`timescale 1ns / 1ps

module accumulator_8bit (
    input  clk,
    input  rst_n,
    input  en,
    input  [7:0] din,
    output reg [7:0] acc
);
    always @(posedge clk) begin
        if (!rst_n) acc <= 0;
        else if (en) acc <= acc + din;
    end
endmodule
"""

# The correct interface the summary promises (din/en are inputs).
_ACC_EXPECTED_DIRS = {
    "clk": "input",
    "rst_n": "input",
    "en": "input",
    "din": "input",
    "acc": "output",
}


# The FormalSpec exactly as Agent 3 produced it live: din/en promoted to
# variables, each self-holding in every action ("din" -> "din").
def _buggy_accumulator_formal_spec() -> FormalSpec:
    return FormalSpec(
        module_name="accumulator_8bit",
        description="8-bit accumulator; din/en wrongly modelled as state variables.",
        variables={
            "acc": {"type": "Nat", "width": 8},
            "din": {"type": "Nat", "width": 8},
            "en": {"type": "Bit", "width": 1},
        },
        initial={"acc": "0", "din": "0", "en": "0"},
        transitions=[
            {"label": "Reset", "condition": "rst_n = 0",
             "updates": {"acc": "0", "din": "din", "en": "en"}},
            {"label": "Accumulate", "condition": "rst_n = 1 AND en = 1",
             "updates": {"acc": "(acc + din) % 256", "din": "din", "en": "en"}},
            {"label": "Hold", "condition": "rst_n = 1 AND en = 0",
             "updates": {"acc": "acc", "din": "din", "en": "en"}},
        ],
        invariants=["acc >= 0 AND acc <= 255"],
    )


def _accumulator_input_summary() -> SpecSummary:
    """Correct Stage-1 summary: din/en ARE inputs (matches Agent 1's live output)."""
    return SpecSummary(
        module_name="accumulator_8bit",
        description="8-bit accumulator; sync active-low reset + enable.",
        ports=[
            {"name": "clk", "direction": "input", "width": 1},
            {"name": "rst_n", "direction": "input", "width": 1},
            {"name": "en", "direction": "input", "width": 1},
            {"name": "din", "direction": "input", "width": 8},
            {"name": "acc", "direction": "output", "width": 8},
        ],
        test_vectors=[
            {"inputs": {"en": 1, "din": 5}, "expected": {"acc": 5}},
        ],
        reset_port="rst_n",
        reset_active_low=True,
    )


# ===========================================================================
# Fix 1 (a) — the gate's parser + checker (unit, on real captured data)
# ===========================================================================

def test_parser_reads_directions_from_live_module():
    dirs = _parse_module_port_directions(_LIVE_BUGGY_VERILOG)
    assert dirs == {
        "clk": "input", "rst_n": "input",
        "acc": "output", "din": "output", "en": "output",
    }


def test_gate_flags_input_emitted_as_output():
    """The headline: din/en declared input but emitted output → violations."""
    violations = _verify_port_directions(_LIVE_BUGGY_VERILOG, _ACC_EXPECTED_DIRS)
    assert violations, "gate must flag the din/en input-as-output mismatch"
    blob = " ".join(violations)
    assert "din" in blob and "en" in blob
    # acc/clk/rst_n are correct and must NOT be flagged.
    assert "'acc'" not in blob and "'clk'" not in blob and "'rst_n'" not in blob


def test_gate_passes_correct_interface():
    assert _verify_port_directions(_CORRECT_VERILOG, _ACC_EXPECTED_DIRS) == []


def test_gate_failsoft_on_unknown_contract_but_loud_on_unparseable_output():
    """Asymmetric by design (verification finding): an UNKNOWN contract (summary
    unreadable → empty expected) is a genuine no-op, but a KNOWN contract whose
    emitted header cannot be parsed must fail LOUD — 'unable to verify' must never
    pass as 'verified clean'."""
    # Unknown contract → no-op.
    assert _verify_port_directions(_LIVE_BUGGY_VERILOG, {}) == []
    # Known contract + unparseable output → loud (refuses to certify).
    loud = _verify_port_directions("not verilog at all", _ACC_EXPECTED_DIRS)
    assert loud and "could not parse" in loud[0]


def test_summary_port_directions_reads_artifact(tmp_path):
    art = Path("artifacts") / "pd_summary"
    art.mkdir(parents=True, exist_ok=True)
    data = _accumulator_input_summary().model_dump()
    data["status"] = "success"
    (art / "01_summary.json").write_text(json.dumps(data))
    assert _summary_port_directions(art) == _ACC_EXPECTED_DIRS
    # Missing summary → {} (fail-soft).
    assert _summary_port_directions(Path("artifacts") / "nope") == {}


# ===========================================================================
# Fix 1 (b) — integration: the gate catches the live false-green end to end
# ===========================================================================

def _buggy_spec_picker(applicable, spec, *, system_prompt=None):
    """Spec-inspecting picker that drives the buggy 3-action spec to RTL-style.

    Initialization first; then Iteration on each not-yet-clocked non-Reset action
    (Accumulate, then Hold) — the live trajectory. Keyed on the spec's own action
    state rather than a fixed counter, so re-picks after a strike stay correct.
    """
    names = {r["name"] for r in applicable}
    if "Initialization" in names:
        return {"rule_name": "Initialization", "params": {
            "reset_values": {"acc": "0", "din": "0", "en": "0"},
            "reset_action_name": "Reset",
        }}
    if "Iteration" in names:
        for action in spec.get("actions", []):
            nm = action.get("name")
            if nm and nm != "Reset" and not action.get("clocked"):
                return {"rule_name": "Iteration", "params": {"action_name": nm}}
    return {"rule_name": sorted(names)[0], "params": {}}


def test_gate_blocks_false_green_through_run_stage3(tmp_path, monkeypatch):
    """Replicates run 223427: a buggy spec (din/en as variables) must NOT reach
    03_rtl_output 'success' — the gate downgrades it with port_direction_errors."""
    # Agent boundaries → buggy spec, spec-inspecting picker, no-revise, accept.
    monkeypatch.setattr(agent3, "generate_formal_spec",
                        lambda s: _buggy_accumulator_formal_spec())
    monkeypatch.setattr(agent3, "pick_rule", _buggy_spec_picker)
    monkeypatch.setattr(agent3, "revise_on_tlc", lambda spec, errs: spec)
    monkeypatch.setattr(stage3, "_run_refinement_critic",
                        lambda a, c: {"verdict": "accept", "issues": [], "reasoning": "test"})

    run_id = "pd_gate_e2e"
    art = Path("artifacts") / run_id
    art.mkdir(parents=True, exist_ok=True)
    summ = _accumulator_input_summary().model_dump()
    summ["status"] = "success"
    (art / "01_summary.json").write_text(json.dumps(summ))

    state = {"run_id": run_id, "retry_counts": {}, "halt": False}
    run_stage3(state)

    rtl = json.loads((art / "03_rtl_output.json").read_text())
    assert rtl["status"] != "success", (
        "false green: a din/en-as-output interface reached success.\n" + json.dumps(rtl)[:600]
    )
    assert "port_direction_errors" in rtl
    blob = " ".join(rtl["port_direction_errors"])
    assert "din" in blob and "en" in blob


# ===========================================================================
# Fix 2 — Agent 3 prompt forbids modelling inputs as variables
# ===========================================================================

def test_agent3_prompt_forbids_input_as_variable():
    p = agent3._SYSTEM_PROMPT
    assert "FREE INPUT" in p
    assert "LITMUS" in p
    assert '"x" -> "x"' in p
    # Still keeps the older clock/reset carve-out.
    assert "clock or the reset signal" in p


# ===========================================================================
# Fix 3 — the cocotb-revise path yields a replayable chain (no discontinuity)
# ===========================================================================

def _name_picker(sequence):
    """Applicability-driven picker (first sequence entry whose rule is applicable)."""
    def picker(applicable, spec, *, system_prompt=None):
        names = {r["name"] for r in applicable}
        for rule_name, params in sequence:
            if rule_name in names:
                return {"rule_name": rule_name, "params": params}
        return {"rule_name": sorted(names)[0], "params": {}}
    return picker


def test_revise_clears_stale_chain_for_replayable_result(tmp_path, monkeypatch):
    """A cocotb-revise re-authors the spec; the resulting refinement_chain.json
    must be self-contained and replayable (contiguous hashes), and the stale chain
    must be preserved as refinement_chain_pre_revise.json — not appended onto."""
    # Revise returns the CORRECT accumulator spec (din/en as free inputs).
    monkeypatch.setattr(agent3, "revise_on_cocotb", lambda spec, log: accumulator_formal_spec())
    monkeypatch.setattr(agent3, "revise_on_tlc", lambda spec, errs: spec)
    monkeypatch.setattr(agent3, "pick_rule", _name_picker(accumulator_picker_sequence()))
    monkeypatch.setattr(stage3, "_run_refinement_critic",
                        lambda a, c: {"verdict": "accept", "issues": [], "reasoning": "test"})

    run_id = "revise_replay"
    art = Path("artifacts") / run_id
    art.mkdir(parents=True, exist_ok=True)

    summ = accumulator_summary().model_dump()
    summ["status"] = "success"
    (art / "01_summary.json").write_text(json.dumps(summ))
    # A prior (buggy) formal spec on disk — revise will replace it.
    fs = _buggy_accumulator_formal_spec().model_dump()
    fs["status"] = "success"
    (art / "02_formal_spec.json").write_text(json.dumps(fs))
    (art / "04_evaluation.json").write_text(json.dumps(
        {"status": "error", "error": "mismatch", "phase": "test", "failed_vectors": [], "raw": ""}))

    # A STALE chain from the old run (the prefix that the bug appended onto).
    stale_chain = [
        {"step": 0, "rule_name": "Initialization",
         "params": {"reset_values": {"acc": "0"}, "reset_action_name": "Reset"},
         "pre_hash": "stale000", "post_hash": "stale111"},
        {"step": 1, "rule_name": "Iteration", "params": {"action_name": "Accumulate"},
         "pre_hash": "stale111", "post_hash": "stale222"},
    ]
    (art / "refinement_chain.json").write_text(json.dumps(stale_chain, indent=2))

    state = {"run_id": run_id, "retry_counts": {}, "halt": False}
    run_stage3_revise_cocotb(state)

    # The stale chain is preserved (suffixed by attempt #1), not silently dropped.
    preserved = json.loads((art / "refinement_chain_pre_revise_1.json").read_text())
    assert preserved == stale_chain

    # The new chain replays from the persisted (revised) spec: contiguous hashes,
    # and step 0 starts at the revised initial spec's hash.
    chain = json.loads((art / "refinement_chain.json").read_text())
    assert chain, "engine wrote no chain after revise"
    assert not any(s["pre_hash"].startswith("stale") for s in chain), (
        "stale steps leaked into the post-revise chain (discontinuity bug)"
    )

    revised_on_disk = json.loads((art / "02_formal_spec.json").read_text())
    initial = formal_spec_to_engine_spec(FormalSpec.model_validate(revised_on_disk))
    assert chain[0]["pre_hash"] == _spec_hash(initial), (
        "chain is not replayable from the persisted revised spec"
    )
    for prev, cur in zip(chain, chain[1:]):
        assert cur["pre_hash"] == prev["post_hash"], "non-contiguous (non-replayable) chain"

    # Replaying the chain from the revised initial spec must not raise.
    _replay_chain(initial, chain)
