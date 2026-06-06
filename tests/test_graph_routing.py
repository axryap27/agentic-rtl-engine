"""
LangGraph control-plane tests — deterministic, OFFLINE (no live LLM).

Covers the gaps G06 (routing + write-before-return) and G07 ('partial' RTL
routing) from docs/test_suite_problems.md.

The pipeline routes SOLELY on the ``status`` field of each artifact JSON on
disk (CLAUDE.md): conditional edge functions never look at Python return values
or exceptions. So this file pins three control-plane invariants without ever
touching a model:

  1. ``_read_status`` crash-shield — never raises; returns ``'error'`` for a
     missing file, invalid JSON, and a status-less JSON object.
  2. The ``_route_after_*`` tables — the success→advance / partial,error,
     missing→halt mapping that decides converge-vs-halt.
  3. write-before-return — every stage node writes a status-bearing artifact
     even when its LLM/agent/generator/runner dependency RAISES, so the router
     can always act (an unwritten/status-less artifact crashes the router).
  4. The pass6_checker refinement-correctness critic GATE: a 'reject' verdict
     writes a non-success 03_rtl_output.json (status 'partial') with NO
     output.v, so routing halts; 'accept' / no-verdict proceeds to 'success'.

NO live LLM call happens here:
  * stage1/stage3 LLM entry points are monkeypatched to raise (write-before-
    return) or stubbed (critic-gate path).
  * The critic-gate tests stub the single mock boundary
    ``pipeline.nodes.stage3._run_refinement_critic`` and drive refinement with
    an APPLICABILITY-DRIVEN, IDEMPOTENT ``pick_rule`` stub (NOT a monotonic
    counter — stage3 calls engine.run ~5x sharing one picker; a counter would
    exhaust and stall the pass, silently masking the test).

Subprocess tools (iverilog/verilator/tlc) are NOT required: TLC is best-effort
inside stage3 (skipped when absent) and these tests never elaborate Verilog.

Run with:
    python3.11 -m pytest tests/test_graph_routing.py -q
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

# Ensure the project root is on sys.path (mirrors the other test files).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Import production modules at top level, BEFORE any test changes cwd. The
# `pipeline` package resolves relative to the project root; if we let a test
# chdir(tmp) first and then imported, the import would fail with
# ModuleNotFoundError. Importing here freezes the module objects so per-test
# monkeypatch.chdir() is safe.
import pipeline.graph as graph
import pipeline.nodes.stage1 as stage1
import pipeline.nodes.stage2 as stage2
import pipeline.nodes.stage3 as stage3
import pipeline.nodes.stage4 as stage4
import pipeline.nodes.diagnose as diagnose
from pipeline.schemas.tla_schema import FormalSpec


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_state(run_id: str, retry_counts: dict | None = None,
                last_diagnosis: str | None = None) -> dict:
    """Build a thin PipelineState dict (no design data — per state.py)."""
    return {
        "run_id": run_id,
        "retry_counts": retry_counts or {},
        "halt": False,
        "last_diagnosis": last_diagnosis,
    }


def _write_artifact(run_id: str, filename: str, payload) -> Path:
    """Write a raw artifact JSON under artifacts/<run_id>/ (relative to cwd).

    Uses json.dump directly (NOT pipeline.schemas.envelope.write_artifact) so we
    can deliberately write status-less / invalid-status payloads that the
    envelope validator would reject — those are exactly the crash-shield cases.
    """
    adir = Path("artifacts") / run_id
    adir.mkdir(parents=True, exist_ok=True)
    path = adir / filename
    path.write_text(json.dumps(payload))
    return path


def _dff_formal_spec() -> FormalSpec:
    """Hand-built D flip-flop FormalSpec (mirrors tests/test_dff.py).

    q follows the free data input d; sync reset to 0. Refines to RTL-style via
    the idempotent sequence below, so it can drive stage3's full multi-pass
    refinement offline.
    """
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


# Known-good refinement sequence for the DFF (mirrors tests/test_dff.py).
# The stub below picks the FIRST entry whose rule name is currently applicable —
# it relies on each rule's is_applicable() going False after it fires, so it is
# safe to share across stage3's ~5 engine.run passes. See agent-memory:
# feedback_stage3_multipass_stub.md (do NOT use a monotonic step counter).
_DFF_SEQUENCE: list[tuple[str, dict]] = [
    ("Initialization",
     {"reset_values": {"q": "0"}, "reset_action_name": "Reset"}),
    ("Assignment",
     {"action_name": "Capture", "updates": [{"variable": "q", "expression": "d"}]}),
    ("Iteration",
     {"action_name": "Capture"}),
]


def _idempotent_pick(applicable_rules, spec, *, system_prompt=None):
    """Applicability-driven, IDEMPOTENT pick_rule stub.

    Pick the first _DFF_SEQUENCE entry whose rule name is in applicable_rules.
    No internal counter, so re-invoking across stage3's per-pass + catch-all
    engine.run calls never exhausts. Accepts the `system_prompt` kwarg because
    stage3's pass-pick wrappers forward it.
    """
    names = {r["name"] for r in applicable_rules}
    for rule_name, params in _DFF_SEQUENCE:
        if rule_name in names:
            return {"rule_name": rule_name, "params": params}
    return {"rule_name": applicable_rules[0]["name"], "params": {}}


# ===========================================================================
# 1. _read_status crash-shield — never raises, returns 'error' on bad input
# ===========================================================================

def test_read_status_missing_file_returns_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # No artifacts/ dir at all.
    assert graph._read_status("no_such_run", "01_summary.json") == "error"


def test_read_status_missing_file_in_existing_run_returns_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "artifacts" / "run").mkdir(parents=True)
    # Dir exists but the specific artifact file does not.
    assert graph._read_status("run", "03_rtl_output.json") == "error"


def test_read_status_invalid_json_returns_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_artifact("run", "bad.json", None)  # placeholder; overwrite below
    (tmp_path / "artifacts" / "run" / "bad.json").write_text("{ this is not json")
    assert graph._read_status("run", "bad.json") == "error"


def test_read_status_no_status_key_returns_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_artifact("run", "nostatus.json", {"module_name": "dff", "foo": 1})
    assert graph._read_status("run", "nostatus.json") == "error"


def test_read_status_valid_status_passes_through(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_artifact("run", "ok.json", {"status": "success"})
    assert graph._read_status("run", "ok.json") == "success"


def test_read_status_never_raises(tmp_path, monkeypatch):
    """The crash-shield contract: _read_status must NEVER propagate an exception
    (a raising router crashes the run to zero RTL — G06)."""
    monkeypatch.chdir(tmp_path)
    adir = tmp_path / "artifacts" / "run"
    adir.mkdir(parents=True)
    # A directory where a file is expected → read_text() raises IsADirectoryError
    # internally; the shield must still return 'error', not propagate.
    (adir / "weird.json").mkdir()
    assert graph._read_status("run", "weird.json") == "error"


# ===========================================================================
# 2. _route_after_* tables (status on disk → routing label)
# ===========================================================================

# --- _route_after_stage3 (CONFIRMED contract: only 'success' advances) ------

@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"status": "success", "module_name": "dff", "verilog_path": "x", "verilog": "..."}, "advance"),
        ({"status": "partial", "error": "unrefined fallback"}, "halt"),
        ({"status": "error", "error": "compiler 2 failed"}, "halt"),
        ({"module_name": "dff"}, "halt"),        # status-less
    ],
)
def test_route_after_stage3_table(tmp_path, monkeypatch, payload, expected):
    monkeypatch.chdir(tmp_path)
    run_id = "s3"
    _write_artifact(run_id, "03_rtl_output.json", payload)
    assert graph._route_after_stage3(_make_state(run_id)) == expected


def test_route_after_stage3_missing_file_halts(tmp_path, monkeypatch):
    """No 03_rtl_output.json on disk → _read_status returns 'error' → halt
    (the router must never advance on an absent RTL artifact)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "artifacts" / "s3_missing").mkdir(parents=True)
    assert graph._route_after_stage3(_make_state("s3_missing")) == "halt"


def test_route_after_stage3_partial_does_not_advance(tmp_path, monkeypatch):
    """G07 regression guard: 'partial' RTL (built from the UNREFINED spec, or a
    rejected refinement) must NOT advance to cocotb — it would vacuously pass."""
    monkeypatch.chdir(tmp_path)
    _write_artifact("g07", "03_rtl_output.json", {"status": "partial"})
    assert graph._route_after_stage3(_make_state("g07")) == "halt"


# --- _route_after_stage1 ----------------------------------------------------

def test_route_after_stage1_success_advances(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_artifact("s1", "01_summary.json", {"status": "success"})
    assert graph._route_after_stage1(_make_state("s1")) == "advance"


@pytest.mark.parametrize("count", [0, 1])
def test_route_after_stage1_error_under_limit_retries(tmp_path, monkeypatch, count):
    """error with retries <= _MAX_STAGE1_RETRIES (1) → retry."""
    monkeypatch.chdir(tmp_path)
    _write_artifact("s1", "01_summary.json", {"status": "error"})
    state = _make_state("s1", retry_counts={"stage1": count})
    assert graph._route_after_stage1(state) == "retry"


def test_route_after_stage1_error_at_limit_halts(tmp_path, monkeypatch):
    """error with retries > _MAX_STAGE1_RETRIES → halt (counter is bounded)."""
    monkeypatch.chdir(tmp_path)
    _write_artifact("s1", "01_summary.json", {"status": "error"})
    state = _make_state("s1", retry_counts={"stage1": graph._MAX_STAGE1_RETRIES + 1})
    assert graph._route_after_stage1(state) == "halt"


def test_route_after_stage1_missing_artifact_treated_as_error(tmp_path, monkeypatch):
    """A missing 01_summary.json reads as 'error'; with the counter exhausted it
    halts rather than crashing the router."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "artifacts" / "s1m").mkdir(parents=True)
    state = _make_state("s1m", retry_counts={"stage1": graph._MAX_STAGE1_RETRIES + 1})
    assert graph._route_after_stage1(state) == "halt"


# --- _route_after_stage4 ----------------------------------------------------

def test_route_after_stage4_success_done(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_artifact("s4", "04_evaluation.json", {"status": "success"})
    assert graph._route_after_stage4(_make_state("s4")) == "done"


@pytest.mark.parametrize("count", [0, 1])
def test_route_after_stage4_error_under_limit_diagnoses(tmp_path, monkeypatch, count):
    """cocotb failure with retries < _MAX_COCOTB_RETRIES (2) → diagnose."""
    monkeypatch.chdir(tmp_path)
    _write_artifact("s4", "04_evaluation.json", {"status": "error"})
    state = _make_state("s4", retry_counts={"stage4_cocotb": count})
    assert graph._route_after_stage4(state) == "diagnose"


def test_route_after_stage4_error_at_limit_halts(tmp_path, monkeypatch):
    """cocotb retry counter is bounded: at the limit → halt (no infinite loop)."""
    monkeypatch.chdir(tmp_path)
    _write_artifact("s4", "04_evaluation.json", {"status": "error"})
    state = _make_state("s4", retry_counts={"stage4_cocotb": graph._MAX_COCOTB_RETRIES})
    assert graph._route_after_stage4(state) == "halt"


def test_route_after_stage4_missing_artifact_halts_when_exhausted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "artifacts" / "s4m").mkdir(parents=True)
    state = _make_state("s4m", retry_counts={"stage4_cocotb": graph._MAX_COCOTB_RETRIES})
    assert graph._route_after_stage4(state) == "halt"


# --- _route_after_diagnose (routes on state, not on disk) -------------------

def test_route_after_diagnose_refinement_backtracks():
    assert graph._route_after_diagnose(
        _make_state("d", last_diagnosis="refinement")) == "backtrack"


def test_route_after_diagnose_spec_revises():
    assert graph._route_after_diagnose(
        _make_state("d", last_diagnosis="spec")) == "revise_spec"


def test_route_after_diagnose_none_defaults_to_revise():
    """A missing/None diagnosis defaults to spec revision (the safe default)."""
    assert graph._route_after_diagnose(
        _make_state("d", last_diagnosis=None)) == "revise_spec"


# ===========================================================================
# 3. write-before-return — each node writes a status-bearing artifact even
#    when its LLM/agent/generator/runner dependency RAISES.
# ===========================================================================

def test_stage1_writes_error_artifact_when_agent_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_id = "wbr_s1"
    _write_artifact(run_id, "00_nl_spec.json", {"prompt": "a 2-bit counter"})
    state = _make_state(run_id)
    with mock.patch.object(stage1.agent1, "run", side_effect=RuntimeError("boom")):
        stage1.run_stage1(state)
    art = json.loads((tmp_path / "artifacts" / run_id / "01_summary.json").read_text())
    assert art["status"] == "error"
    # The router routes on this artifact; the counter must have advanced so the
    # bounded-retry logic in _route_after_stage1 can eventually halt.
    assert state["retry_counts"]["stage1"] == 1


def test_stage2_writes_error_artifact_when_generator_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_id = "wbr_s2"
    _write_artifact(run_id, "01_summary.json", {
        "status": "success", "module_name": "dff", "description": "d",
        "ports": [], "test_vectors": [],
    })
    state = _make_state(run_id)
    with mock.patch.object(stage2, "generate_testbench", side_effect=RuntimeError("boom")):
        stage2.run_stage2(state)
    art = json.loads((tmp_path / "artifacts" / run_id / "02_testbench_meta.json").read_text())
    assert art["status"] == "error"


def test_stage3_writes_error_artifact_when_agent_raises(tmp_path, monkeypatch):
    """If Agent 3's generate_formal_spec raises, BOTH 02_formal_spec.json and the
    routed 03_rtl_output.json must be written 'error' (the router reads 03)."""
    monkeypatch.chdir(tmp_path)
    run_id = "wbr_s3"
    _write_artifact(run_id, "01_summary.json", {
        "status": "success", "module_name": "dff", "description": "d",
        "ports": [], "test_vectors": [],
    })
    state = _make_state(run_id)
    with mock.patch.object(stage3._agent3, "generate_formal_spec",
                           side_effect=RuntimeError("boom")):
        stage3.run_stage3(state)
    rtl = json.loads((tmp_path / "artifacts" / run_id / "03_rtl_output.json").read_text())
    formal = json.loads((tmp_path / "artifacts" / run_id / "02_formal_spec.json").read_text())
    assert rtl["status"] == "error"
    assert formal["status"] == "error"


def test_stage4_writes_error_artifact_when_runner_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_id = "wbr_s4"
    adir = tmp_path / "artifacts" / run_id
    _write_artifact(run_id, "03_rtl_output.json", {
        "status": "success", "module_name": "dff",
        "verilog_path": str(adir / "output.v"), "verilog": "module dff; endmodule",
    })
    _write_artifact(run_id, "02_testbench_meta.json", {
        "status": "success", "testbench_path": str(adir / "tb.py"),
    })
    state = _make_state(run_id)
    with mock.patch.object(stage4, "run_testbench", side_effect=RuntimeError("boom")):
        stage4.run_stage4(state)
    art = json.loads((adir / "04_evaluation.json").read_text())
    assert art["status"] == "error"


def test_diagnose_writes_error_artifact_when_diagnoser_raises(tmp_path, monkeypatch):
    """The diagnoser node must always write 04_diagnosis.json AND set
    last_diagnosis (default 'spec') so the edge function has a routing signal."""
    monkeypatch.chdir(tmp_path)
    run_id = "wbr_diag"
    (tmp_path / "artifacts" / run_id).mkdir(parents=True)
    state = _make_state(run_id)
    # diagnose imports agent_diagnoser lazily inside the function, so patch the
    # module attribute it resolves at call time.
    import pipeline.agents.agent_diagnoser as agent_diagnoser
    with mock.patch.object(agent_diagnoser, "diagnose", side_effect=RuntimeError("boom")):
        diagnose.run_diagnose(state)
    art = json.loads((tmp_path / "artifacts" / run_id / "04_diagnosis.json").read_text())
    assert art["status"] == "error"
    assert state["last_diagnosis"] == "spec"


def test_node_error_artifacts_route_to_halt(tmp_path, monkeypatch):
    """End-to-end of the invariant: a node that fails writes an 'error' artifact,
    and the matching router maps it to a non-advancing label. Proves the
    write-before-return contract actually feeds the routing layer (G06)."""
    monkeypatch.chdir(tmp_path)
    run_id = "wbr_route"
    _write_artifact(run_id, "01_summary.json", {
        "status": "success", "module_name": "dff", "description": "d",
        "ports": [], "test_vectors": [],
    })
    state = _make_state(run_id)
    with mock.patch.object(stage3._agent3, "generate_formal_spec",
                           side_effect=RuntimeError("boom")):
        stage3.run_stage3(state)
    # 03_rtl_output.json is now 'error' → _route_after_stage3 must halt.
    assert graph._route_after_stage3(state) == "halt"


# ===========================================================================
# 4. pass6 refinement-correctness critic GATE routing
#    (mock boundary: pipeline.nodes.stage3._run_refinement_critic)
# ===========================================================================

def _drive_stage3_from_spec(tmp_path, monkeypatch, run_id, critic_return):
    """Drive stage3._run_stage3_from_spec offline on the DFF spec.

    Stubs:
      * agent3.pick_rule       → idempotent, applicability-driven (no live call)
      * agent3.revise_on_tlc   → identity (defensive; TLC is absent so unused)
      * stage3._run_refinement_critic → critic_return (accept/reject/None)
    Returns (rtl_dict, formal_dict, output_v_exists).
    """
    monkeypatch.chdir(tmp_path)
    adir = tmp_path / "artifacts" / run_id
    adir.mkdir(parents=True)
    spec = _dff_formal_spec()
    state = _make_state(run_id)

    with mock.patch.object(stage3._agent3, "pick_rule", side_effect=_idempotent_pick), \
         mock.patch.object(stage3._agent3, "revise_on_tlc", side_effect=lambda s, e: s), \
         mock.patch.object(stage3, "_run_refinement_critic", return_value=critic_return):
        stage3._run_stage3_from_spec(state, spec)

    rtl = json.loads((adir / "03_rtl_output.json").read_text())
    formal = json.loads((adir / "02_formal_spec.json").read_text())
    return rtl, formal, (adir / "output.v").exists()


def test_critic_reject_writes_partial_and_no_output_v(tmp_path, monkeypatch):
    """A 'reject' verdict halts compilation: 03_rtl_output.json is 'partial'
    (so _route_after_stage3 halts) and NO output.v is emitted."""
    rtl, formal, has_v = _drive_stage3_from_spec(
        tmp_path, monkeypatch, "critic_reject",
        critic_return={"verdict": "reject", "issues": ["x"], "reasoning": "y"},
    )
    assert rtl["status"] == "partial"
    assert not has_v, "rejected refinement must NOT emit output.v"
    # The honest reason is on disk for debugging.
    assert "REJECT" in rtl.get("error", "").upper()
    # The verdict is recorded on the formal-spec artifact. Its presence (vs a
    # `refinement_error` key) confirms refinement actually RAN and the critic
    # fired — not the G07 unrefined-fallback path masquerading as a reject.
    assert formal.get("critic_verdict", {}).get("verdict") == "reject"
    assert "refinement_error" not in formal, (
        "refinement stalled instead of reaching the critic gate — the pick_rule "
        "stub likely exhausted (see feedback_stage3_multipass_stub.md)"
    )


def test_critic_reject_route_halts(tmp_path, monkeypatch):
    """The whole point of the 'reject' gate: routing must halt, not advance."""
    run_id = "critic_reject_route"
    _drive_stage3_from_spec(
        tmp_path, monkeypatch, run_id,
        critic_return={"verdict": "reject", "issues": ["x"], "reasoning": "y"},
    )
    # cwd is now tmp_path (set by _drive_stage3_from_spec).
    assert graph._route_after_stage3(_make_state(run_id)) == "halt"


def test_critic_accept_proceeds_to_success(tmp_path, monkeypatch):
    """An 'accept' verdict proceeds: status 'success', output.v written,
    routing advances."""
    rtl, formal, has_v = _drive_stage3_from_spec(
        tmp_path, monkeypatch, "critic_accept",
        critic_return={"verdict": "accept", "issues": [], "reasoning": "ok"},
    )
    assert rtl["status"] == "success"
    assert has_v, "accepted refinement must emit output.v"
    assert graph._route_after_stage3(_make_state("critic_accept")) == "advance"


def test_critic_none_verdict_proceeds_to_success(tmp_path, monkeypatch):
    """A None verdict means 'critic unavailable — proceed' (an unavailable
    critic must not halt an otherwise-clean run). Status 'success', advances."""
    rtl, formal, has_v = _drive_stage3_from_spec(
        tmp_path, monkeypatch, "critic_none", critic_return=None,
    )
    assert rtl["status"] == "success"
    assert has_v
    assert graph._route_after_stage3(_make_state("critic_none")) == "advance"


# ---------------------------------------------------------------------------
# Direct-execution mode (mirrors tests/test_dff.py convention)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
