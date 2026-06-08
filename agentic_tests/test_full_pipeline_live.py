"""
Live full-pipeline test — the end-to-end milestone.

This is the deferred "full-pipeline tests" item from agentic_tests/README.md:
build_graph().invoke(...) running an NL prompt all the way through to cocotb.

COST + GATING: this makes REAL API calls on BOTH transports and costs money. It
is triple-gated like the rest of agentic_tests/ — auto-stamped `live_llm` marker
(conftest) + RUN_LIVE_LLM=1 + the required keys. Stage 1 and the diagnoser use
the OpenAI-compatible proxy (require_proxy); Agent 3 uses the Anthropic key
directly (require_anthropic). A plain `pytest` run collects but never executes
it (deselected by the marker).

It exercises the Agent-3 budget guard implicitly: a single graph run may make
several Agent-3 calls (formal-spec generation plus, if TLC or cocotb fails, the
revision loop), all billed against Agent 3's own Anthropic account under the
configured cap (default $100). Kept to ONE run on the 2-bit counter to bound cost.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

import pipeline.graph
from pipeline.schemas.envelope import ArtifactEnvelope
from pipeline.state import PipelineState

# This full end-to-end live run is the most expensive test in the suite — it drives
# the WHOLE graph (Agent 1 + Agent 3 + diagnoser + cocotb) live, so a single run can
# cost a lot of Agent-3 tokens. It is gated behind an EXTRA explicit opt-in on top of
# the usual live gates: `pytest agentic_tests -m live_llm` SKIPS it unless
# RUN_FULL_PIPELINE_LIVE=1 is also set. (It still carries the live_llm marker, so a
# plain `pytest` never collects it.)
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_FULL_PIPELINE_LIVE") != "1",
    reason="expensive full-pipeline live run; set RUN_FULL_PIPELINE_LIVE=1 to include it",
)


# SystemVerilog tokens that must NOT appear in the emitted Verilog-2001 (CLAUDE.md
# "Verilog output constraints"). Spaced/padded so `module` does not match
# `always_comb` etc. and substring false-positives are avoided.
_SYSTEMVERILOG_TOKENS = ("logic ", "always_ff", "always_comb", "always_latch", " initial ")


def _seed_nl(tmp_path: Path, run_id: str, prompt: str) -> Path:
    art = tmp_path / "artifacts" / run_id
    art.mkdir(parents=True, exist_ok=True)
    (art / "00_nl_spec.json").write_text(json.dumps({"prompt": prompt}))
    return art


def _fresh_state(run_id: str) -> PipelineState:
    return {
        "run_id": run_id,
        "retry_counts": {},
        "halt": False,
        "last_diagnosis": None,
    }


def test_live_full_pipeline_reaches_cocotb(require_proxy, require_anthropic,
                                           tmp_path, monkeypatch, counter_prompt):
    """NL prompt -> Stage1 -> Stage2 -> Stage3 -> Stage4, end to end, live.

    Contract assertions first (every stage honoured the artifact-write +
    envelope contract and emitted Verilog-2001), then the milestone assertion
    last (cocotb actually PASSED).
    """
    # Tool guards: SKIP (not error) when the simulation toolchain is absent —
    # Stage 4 shells out to iverilog/vvp via cocotb. Mirrors the deterministic
    # suite's cocotb-roundtrip guards.
    pytest.importorskip("cocotb", reason="cocotb not installed")
    if shutil.which("iverilog") is None:
        pytest.skip("iverilog not installed (Stage 4 cocotb runner needs it)")
    if shutil.which("vvp") is None:
        pytest.skip("vvp not installed (Stage 4 cocotb runner needs it)")

    run_id = "full_pipeline_counter"
    art = _seed_nl(tmp_path, run_id, counter_prompt)

    # Stage nodes resolve artifacts/ relative to CWD; isolate in the temp dir.
    monkeypatch.chdir(tmp_path)

    graph = pipeline.graph.get_graph()
    graph.invoke(_fresh_state(run_id))

    # ---- Contract: Stage 1 summary ----
    summary = art / "01_summary.json"
    assert summary.exists(), "Stage 1 must write 01_summary.json"
    summary_data = json.loads(summary.read_text())
    ArtifactEnvelope.model_validate(summary_data)
    assert summary_data["status"] == "success", summary_data

    # ---- Contract: Stage 3 formal spec ----
    formal = art / "02_formal_spec.json"
    assert formal.exists(), "Stage 3 must write 02_formal_spec.json"
    ArtifactEnvelope.model_validate(json.loads(formal.read_text()))

    # ---- Contract: Stage 3 RTL output ----
    rtl = art / "03_rtl_output.json"
    assert rtl.exists(), "Stage 3 must write 03_rtl_output.json"
    rtl_data = json.loads(rtl.read_text())
    ArtifactEnvelope.model_validate(rtl_data)

    if rtl_data["status"] == "success":
        verilog_file = art / "output.v"
        assert verilog_file.exists(), "success RTL output must produce output.v"
        verilog = verilog_file.read_text()
        assert "module" in verilog, "emitted output.v must contain a Verilog module"
        # Verilog-2001 only — no SystemVerilog constructs (CLAUDE.md constraint).
        for tok in _SYSTEMVERILOG_TOKENS:
            assert tok not in verilog, (
                f"output.v contains SystemVerilog token {tok!r}; "
                "Stage 3 must emit Verilog-2001 only"
            )

    # ---- Milestone: cocotb PASS ----
    # Only assert when the run actually reached Stage 4 (Stage 3 advanced on
    # 'success'; 'partial'/'error' halts before cocotb — see graph routing).
    evaluation = art / "04_evaluation.json"
    if evaluation.exists():
        eval_data = json.loads(evaluation.read_text())
        ArtifactEnvelope.model_validate(eval_data)
        assert eval_data["status"] == "success", (
            "live full pipeline did not reach cocotb PASS — inspect "
            f"artifacts/{run_id}/ (04_evaluation.json, output.v). "
            f"evaluation={json.dumps(eval_data, indent=2, default=str)}"
        )
    else:
        # No 04_evaluation.json means Stage 3 did not advance to Stage 4 (the
        # RTL output was 'partial' or 'error'). That is a real failure of the
        # end-to-end milestone, not a skip — surface it with the artifacts to
        # inspect. (A gated live test SHOULD fail loudly when the thesis breaks.)
        pytest.fail(
            "live full pipeline never reached Stage 4 (no 04_evaluation.json) — "
            f"Stage 3 status was {rtl_data['status']!r}; inspect "
            f"artifacts/{run_id}/ (03_rtl_output.json)."
        )
