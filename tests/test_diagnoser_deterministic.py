"""
Deterministic tests for the failure diagnoser (pipeline/agents/agent_diagnoser.py)
and the diagnose node (pipeline/nodes/diagnose.py).

NO LLM, NO network, NO real API keys. The OpenAI client boundary is monkeypatched
to a fake (``agent_diagnoser._get_client``); the build-phase short-circuit asserts
the LLM is never even constructed.

cwd discipline (per the graph-test-chdir memory): import every pipeline module at
the TOP of this file, while cwd is still the project root, so the ``pipeline``
package resolves and the frozen module objects survive per-test chdir. Then each
test does ``monkeypatch.chdir(tmp_path)`` FIRST and creates artifacts/<run_id>/
under tmp_path. Artifact JSON is written RAW (json.dump), not via the schemas,
so malformed inputs can be crafted.

The ledger is isolated to a tmp file (USAGE_LOG_PATH) in the LLM-path tests so
log_usage never touches the real artifacts/usage/ ledger.

Run with:
    python3.11 -m pytest tests/test_diagnoser_deterministic.py -q
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

# Project root on sys.path, then import pipeline modules at top level (BEFORE
# any chdir). The diagnose node imports agent_diagnoser LAZILY inside the
# function, so to mock its diagnose() we patch the agent_diagnoser module
# attribute, not a node-level alias.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pipeline.agents.agent_diagnoser as agent_diagnoser
import pipeline.nodes.diagnose as diagnose
from pipeline.schemas.envelope import ArtifactEnvelope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_artifact(run_id: str, filename: str, payload: dict | list) -> Path:
    """Write a RAW artifact JSON under artifacts/<run_id>/ (relative to cwd).

    Uses json.dump directly so a test can craft malformed / status-less inputs
    that the schema layer would reject.
    """
    adir = Path("artifacts") / run_id
    adir.mkdir(parents=True, exist_ok=True)
    path = adir / filename
    path.write_text(json.dumps(payload))
    return path


def _make_fake_client(content: str, *, called_flag: dict | None = None):
    """Build a fake OpenAI client mimicking the response shape diagnose() reads.

    ``client.chat.completions.create(**kwargs)`` returns an object whose
    ``choices[0].message.content`` is ``content`` and whose ``usage`` is None.
    If ``called_flag`` is given, sets called_flag["called"] = True on invocation.
    """
    def create(**kwargs):
        if called_flag is not None:
            called_flag["called"] = True
        message = types.SimpleNamespace(content=content)
        choice = types.SimpleNamespace(message=message)
        return types.SimpleNamespace(choices=[choice], usage=None)

    completions = types.SimpleNamespace(create=create)
    chat = types.SimpleNamespace(completions=completions)
    return types.SimpleNamespace(chat=chat)


@pytest.fixture
def isolate_ledger(tmp_path, monkeypatch):
    """Route log_usage to a tmp file so the real ledger is untouched."""
    monkeypatch.setenv("USAGE_LOG_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.delenv("USAGE_LOG_DIR", raising=False)


def _make_state(run_id: str) -> dict:
    """A thin PipelineState dict (no design data — per state.py)."""
    return {
        "run_id": run_id,
        "retry_counts": {},
        "halt": False,
        "last_diagnosis": None,
    }


# ---------------------------------------------------------------------------
# 1. phase == "build" -> "spec" with NO LLM call (build short-circuit).
# ---------------------------------------------------------------------------

def test_build_phase_short_circuits_no_llm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def _boom():
        raise AssertionError("_get_client must not be called on a build failure")

    monkeypatch.setattr(agent_diagnoser, "_get_client", _boom)

    _write_artifact("r1", "04_evaluation.json", {
        "status": "error", "phase": "build", "error": "iverilog: syntax error",
    })

    result = agent_diagnoser.diagnose("r1")
    assert result["failure_type"] == "spec"


# ---------------------------------------------------------------------------
# 2. diagnose NEVER raises on unreadable input (missing / malformed file).
# ---------------------------------------------------------------------------

def test_diagnose_missing_evaluation_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # No 04_evaluation.json at all -> except path -> "spec", no raise.
    result = agent_diagnoser.diagnose("r1")
    assert result["failure_type"] == "spec"


def test_diagnose_malformed_evaluation_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    adir = Path("artifacts") / "r1"
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "04_evaluation.json").write_text("this is not json {{{")
    result = agent_diagnoser.diagnose("r1")
    assert result["failure_type"] == "spec"


# ---------------------------------------------------------------------------
# 3. phase == "test", out-of-vocab failure_type coerced to "spec".
# ---------------------------------------------------------------------------

def test_test_phase_out_of_vocab_coerced_to_spec(tmp_path, monkeypatch, isolate_ledger):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setattr(
        agent_diagnoser, "_get_client",
        lambda: _make_fake_client('{"failure_type":"banana","explanation":"x"}'),
    )

    _write_artifact("r1", "04_evaluation.json", {
        "status": "error", "phase": "test",
        "failed_vectors": [{"cycle": 3, "expected": 0, "got": 1}],
        "raw": "ASSERTION FAILED at cycle 3",
    })
    _write_artifact("r1", "02_formal_spec.json", {"module_name": "counter"})
    _write_artifact("r1", "refinement_chain.json", [])

    result = agent_diagnoser.diagnose("r1")
    assert result["failure_type"] == "spec"


# ---------------------------------------------------------------------------
# 4. phase == "test", valid "refinement" passes through unchanged.
# ---------------------------------------------------------------------------

def test_test_phase_refinement_passes_through(tmp_path, monkeypatch, isolate_ledger):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setattr(
        agent_diagnoser, "_get_client",
        lambda: _make_fake_client(
            '{"failure_type":"refinement","explanation":"wrong reset value"}'),
    )

    _write_artifact("r1", "04_evaluation.json", {
        "status": "error", "phase": "test",
        "failed_vectors": [{"cycle": 0, "expected": 0, "got": 1}],
        "raw": "reset asserted but q=1",
    })
    _write_artifact("r1", "02_formal_spec.json", {"module_name": "counter"})
    _write_artifact("r1", "refinement_chain.json", [{"rule": "Initialization"}])

    result = agent_diagnoser.diagnose("r1")
    assert result["failure_type"] == "refinement"
    assert result["explanation"] == "wrong reset value"


# ---------------------------------------------------------------------------
# 5. phase missing / unknown takes the LLM "test" path (NOT build short-circuit).
# ---------------------------------------------------------------------------

def test_missing_phase_takes_llm_path(tmp_path, monkeypatch, isolate_ledger):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_MODEL", "test-model")
    flag = {"called": False}
    monkeypatch.setattr(
        agent_diagnoser, "_get_client",
        lambda: _make_fake_client('{"failure_type":"spec","explanation":"x"}',
                                  called_flag=flag),
    )

    # No "phase" key at all -> defaults to "test" -> LLM path.
    _write_artifact("r1", "04_evaluation.json", {
        "status": "error",
        "failed_vectors": [],
        "raw": "mismatch",
    })
    _write_artifact("r1", "02_formal_spec.json", {})
    _write_artifact("r1", "refinement_chain.json", [])

    result = agent_diagnoser.diagnose("r1")
    assert flag["called"] is True, "missing phase must NOT short-circuit the build path"
    assert result["failure_type"] == "spec"


def test_unknown_phase_takes_llm_path(tmp_path, monkeypatch, isolate_ledger):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_MODEL", "test-model")
    flag = {"called": False}
    monkeypatch.setattr(
        agent_diagnoser, "_get_client",
        lambda: _make_fake_client('{"failure_type":"spec","explanation":"x"}',
                                  called_flag=flag),
    )

    _write_artifact("r1", "04_evaluation.json", {
        "status": "error", "phase": "unknown",
        "failed_vectors": [],
        "raw": "mismatch",
    })
    _write_artifact("r1", "02_formal_spec.json", {})
    _write_artifact("r1", "refinement_chain.json", [])

    result = agent_diagnoser.diagnose("r1")
    assert flag["called"] is True, "unknown phase must NOT short-circuit the build path"
    assert result["failure_type"] == "spec"


# ---------------------------------------------------------------------------
# 6. run_diagnose NODE — success path (build phase, no LLM needed).
# ---------------------------------------------------------------------------

def test_run_diagnose_node_success_writes_artifact(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    # Build-phase failure -> diagnose short-circuits to "spec" without an LLM.
    _write_artifact("r1", "04_evaluation.json", {
        "status": "error", "phase": "build", "error": "iverilog: boom",
    })

    state = _make_state("r1")
    out = diagnose.run_diagnose(state)

    diag_path = Path("artifacts") / "r1" / "04_diagnosis.json"
    assert diag_path.exists()
    data = json.loads(diag_path.read_text())
    # Validates as a legal status envelope.
    ArtifactEnvelope.model_validate(data)
    assert data["status"] == "success"
    assert data["failure_type"] == "spec"
    assert out["last_diagnosis"] == "spec"


# ---------------------------------------------------------------------------
# 7. run_diagnose NODE — crash path still writes a valid artifact + sets state.
# ---------------------------------------------------------------------------

def test_run_diagnose_node_crash_still_writes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def _boom(run_id):
        raise RuntimeError("boom")

    # diagnose node imports agent_diagnoser lazily; patch the module attribute.
    monkeypatch.setattr(agent_diagnoser, "diagnose", _boom)

    state = _make_state("r1")
    out = diagnose.run_diagnose(state)  # must not propagate the RuntimeError

    diag_path = Path("artifacts") / "r1" / "04_diagnosis.json"
    assert diag_path.exists()
    data = json.loads(diag_path.read_text())
    ArtifactEnvelope.model_validate(data)
    assert data["status"] == "error"
    assert data["failure_type"] == "spec"
    assert out["last_diagnosis"] == "spec"
