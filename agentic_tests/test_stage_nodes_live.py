"""
Live tests for the LLM-driven stage NODES (not just the agent functions).

These verify the node-level contract that the deterministic suite can't reach
without real LLMs:
  - the node writes its status-bearing artifact before returning (the
    artifact-write contract LangGraph routes on),
  - the artifact validates against the status envelope,
  - the produced content matches what the next stage expects to read.

Covered here:
  Stage 1 node (run_stage1)  — proxy creds only (Agent 1).
  Stage 3 node (run_stage3)  — proxy + Anthropic creds (reads Stage 1 output,
                               calls Agent 3, Compiler 1, refinement, Compiler 2).

NOT covered here (deferred until the Agent 3 key is wired and we decide scope):
  - full end-to-end graph runs (main.py / build_graph().invoke),
  - live refinement-engine convergence tests.
See agentic_tests/README.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.nodes.stage1 import run_stage1
from pipeline.nodes.stage3 import run_stage3
from pipeline.schemas.envelope import ArtifactEnvelope
from pipeline.state import PipelineState


def _fresh_state(run_id: str) -> PipelineState:
    return {
        "run_id": run_id,
        "retry_counts": {},
        "halt": False,
        "last_diagnosis": None,
    }


@pytest.fixture
def _in_tmp_cwd(tmp_path, monkeypatch):
    """Stage nodes resolve artifacts/ relative to CWD; isolate in a temp dir."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _seed_nl(tmp_path: Path, run_id: str, prompt: str) -> Path:
    art = tmp_path / "artifacts" / run_id
    art.mkdir(parents=True, exist_ok=True)
    (art / "00_nl_spec.json").write_text(json.dumps({"prompt": prompt}))
    return art


# ---------------------------------------------------------------------------
# Stage 1 node
# ---------------------------------------------------------------------------

def test_stage1_node_writes_valid_summary_artifact(require_proxy, _in_tmp_cwd, counter_prompt):
    """run_stage1 must write a status=success 01_summary.json that validates
    against the envelope and re-parses as a SpecSummary."""
    run_id = "stage1_ok"
    art = _seed_nl(_in_tmp_cwd, run_id, counter_prompt)

    run_stage1(_fresh_state(run_id))

    out = art / "01_summary.json"
    assert out.exists(), "Stage 1 must write its artifact (artifact-write contract)"
    data = json.loads(out.read_text())

    # Envelope validity (BUG-13): status must be a legal literal.
    ArtifactEnvelope.model_validate(data)
    assert data["status"] == "success", data

    # Content must re-parse as the SpecSummary the next stages consume.
    from pipeline.schemas.summary_schema import SpecSummary
    SpecSummary.model_validate(data)


def test_stage1_node_writes_artifact_even_on_bad_input(require_proxy, _in_tmp_cwd):
    """If 00_nl_spec.json is missing, run_stage1 must STILL write a status=error
    artifact (never leave the router without a signal). No LLM call happens on
    this path, but it's gated with the rest of the live suite for cohesion."""
    run_id = "stage1_noinput"
    (_in_tmp_cwd / "artifacts" / run_id).mkdir(parents=True, exist_ok=True)
    # Deliberately do NOT write 00_nl_spec.json.

    run_stage1(_fresh_state(run_id))

    out = _in_tmp_cwd / "artifacts" / run_id / "01_summary.json"
    assert out.exists(), "must write an error artifact when input is missing"
    data = json.loads(out.read_text())
    ArtifactEnvelope.model_validate(data)
    assert data["status"] == "error", data


# ---------------------------------------------------------------------------
# Stage 3 node — needs BOTH transports (reads Stage 1 output, calls Agent 3)
# ---------------------------------------------------------------------------

def test_stage3_node_produces_rtl_artifacts(require_proxy, require_anthropic,
                                             _in_tmp_cwd, counter_prompt):
    """End-to-end through the Stage 3 node: seed a real Stage 1 summary, run
    Stage 3, and assert it writes a valid formal-spec artifact and an
    rtl-output artifact with a routable status.

    This is the closest thing to a live integration test in this suite, but it
    stops at Stage 3 (no cocotb run), so it isolates the spec->refine->compile
    path that Agent 3 drives.
    """
    run_id = "stage3_e2e"
    art = _seed_nl(_in_tmp_cwd, run_id, counter_prompt)

    # Produce a real Stage 1 summary first (Stage 3 reads 01_summary.json).
    run_stage1(_fresh_state(run_id))
    assert (art / "01_summary.json").exists()
    assert json.loads((art / "01_summary.json").read_text())["status"] == "success"

    run_stage3(_fresh_state(run_id))

    # Both Stage 3 artifacts must exist and carry a legal envelope status.
    formal = art / "02_formal_spec.json"
    rtl = art / "03_rtl_output.json"
    assert formal.exists(), "Stage 3 must write 02_formal_spec.json"
    assert rtl.exists(), "Stage 3 must write 03_rtl_output.json"

    formal_data = json.loads(formal.read_text())
    rtl_data = json.loads(rtl.read_text())
    ArtifactEnvelope.model_validate(formal_data)
    ArtifactEnvelope.model_validate(rtl_data)

    # On the happy path the RTL artifact should carry the Verilog and a module.
    if rtl_data["status"] == "success":
        assert rtl_data.get("verilog"), "success RTL artifact must include Verilog text"
        assert "module" in rtl_data["verilog"], "emitted text should be a Verilog module"
        assert rtl_data.get("module_name"), rtl_data


def test_stage3_node_always_writes_artifacts_on_failure(require_proxy, _in_tmp_cwd):
    """If Stage 1 output is absent, run_stage3 must still write status=error
    artifacts rather than crash the router. Proxy-gated (no Agent 3 call is
    reached on this early-abort path)."""
    run_id = "stage3_noinput"
    (_in_tmp_cwd / "artifacts" / run_id).mkdir(parents=True, exist_ok=True)
    # No 01_summary.json seeded.

    run_stage3(_fresh_state(run_id))

    rtl = _in_tmp_cwd / "artifacts" / run_id / "03_rtl_output.json"
    assert rtl.exists(), "Stage 3 must write an error artifact when input is missing"
    ArtifactEnvelope.model_validate(json.loads(rtl.read_text()))
