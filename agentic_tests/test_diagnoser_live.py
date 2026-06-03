"""
Live tests for the diagnoser agent — classifies a cocotb failure as a spec
fault or a refinement fault.

Transport: OpenAI-compatible proxy (same creds as Agent 1). Gated:
marker `live_llm` + RUN_LIVE_LLM=1 + proxy keys.

`diagnose(run_id)` reads artifacts off disk, so each test seeds a temp
artifacts/<run_id>/ directory with the failure context, then asserts the
classification is one of the two legal routing signals. Build failures are
classified WITHOUT an LLM call (asserted in the deterministic suite); these
live tests focus on the LLM-driven test-failure path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.agents import agent_diagnoser


# diagnose() never raises in production: on any API failure it degrades to a
# {"failure_type": "spec", ...} fallback so the router always has a signal. In a
# LIVE test that silent degradation must surface as a FAILURE — otherwise a broken
# proxy (wrong LLM_MODEL, 404, timeout) reads as green and these "live" tests pass
# without ever reaching the model. These sentinels appear only in the fallback
# explanations, never in a genuine LLM classification.
_FALLBACK_SENTINELS = (
    "Diagnoser LLM call failed",
    "Could not read 04_evaluation.json",
)


def _assert_live_classification(result: dict) -> None:
    """Fail if diagnose() returned its error fallback instead of a real answer."""
    explanation = result.get("explanation") or ""
    for sentinel in _FALLBACK_SENTINELS:
        assert sentinel not in explanation, (
            f"diagnose() returned its error fallback, not a live classification: "
            f"{result!r}. The LLM call did not succeed — check LLM_MODEL / proxy "
            f"reachability. A green assertion here would hide a broken proxy."
        )


def _seed_run(tmp_path: Path, run_id: str, eval_data: dict,
              formal_data: dict, chain: list) -> None:
    """Write the three artifacts diagnose() reads into a temp artifacts dir."""
    art = tmp_path / "artifacts" / run_id
    art.mkdir(parents=True, exist_ok=True)
    (art / "04_evaluation.json").write_text(json.dumps(eval_data, indent=2))
    (art / "02_formal_spec.json").write_text(json.dumps(formal_data, indent=2))
    (art / "refinement_chain.json").write_text(json.dumps(chain, indent=2))


@pytest.fixture
def _in_tmp_cwd(tmp_path, monkeypatch):
    """diagnose() resolves artifacts/ relative to CWD; run inside a temp dir."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_diagnose_returns_legal_classification(require_proxy, _in_tmp_cwd):
    """A test-phase failure must classify as exactly 'spec' or 'refinement'."""
    run_id = "diag_legal"
    _seed_run(
        _in_tmp_cwd, run_id,
        eval_data={
            "status": "error", "phase": "test",
            "error": "1 test failed",
            "failed_vectors": [
                {"test": "test_counter", "error_type": "AssertionError",
                 "error_msg": "vector 0: expected count=1, got 0"},
            ],
            "raw": "vector 0: expected count=1, got 0",
        },
        formal_data={
            "module_name": "counter", "description": "2-bit counter",
            "variables": {"count": {"type": "Nat", "width": 2}},
            "initial": {"count": "0"},
            "transitions": [{"label": "Count", "condition": "en = 1",
                             "updates": {"count": "(count + 1) % 4"}}],
            "invariants": [],
        },
        chain=[{"rule_name": "Initialization", "params": {}},
               {"rule_name": "Assignment", "params": {}}],
    )

    result = agent_diagnoser.diagnose(run_id)

    _assert_live_classification(result)  # fail (not pass) if the API call degraded to fallback
    assert isinstance(result, dict)
    assert result.get("failure_type") in ("spec", "refinement"), result
    assert "explanation" in result and result["explanation"]


def test_diagnose_wrong_reset_value_leans_refinement(require_proxy, _in_tmp_cwd):
    """A failure whose signature is 'right structure, wrong constant' (reset to
    1 instead of 0) is the textbook refinement fault. We assert a legal label
    and surface the model's reasoning; we don't hard-fail on the exact class
    since classification is a judgment call, but the explanation should engage
    with the reset value."""
    run_id = "diag_reset"
    _seed_run(
        _in_tmp_cwd, run_id,
        eval_data={
            "status": "error", "phase": "test",
            "error": "reset mismatch",
            "failed_vectors": [
                {"test": "test_counter", "error_type": "AssertionError",
                 "error_msg": "after reset: expected count=0, got 1"},
            ],
            "raw": "after reset: expected count=0, got 1",
        },
        formal_data={
            "module_name": "counter", "description": "2-bit counter",
            "variables": {"count": {"type": "Nat", "width": 2}},
            "initial": {"count": "0"},
            "transitions": [{"label": "Count", "condition": "en = 1",
                             "updates": {"count": "(count + 1) % 4"}}],
            "invariants": [],
        },
        chain=[{"rule_name": "Initialization",
                "params": {"reset_values": {"count": "1"}}}],  # wrong reset value
    )

    result = agent_diagnoser.diagnose(run_id)
    _assert_live_classification(result)  # don't let a 404 masquerade as a 'spec' verdict
    assert result.get("failure_type") in ("spec", "refinement"), result


def test_diagnose_never_raises_on_missing_artifacts(require_proxy, _in_tmp_cwd):
    """diagnose() promises to never raise — on a run_id with no artifacts it
    should still return a legal default ('spec') so routing always has a signal."""
    result = agent_diagnoser.diagnose("nonexistent_run")
    assert result.get("failure_type") == "spec", result
