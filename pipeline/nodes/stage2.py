"""
Stage 2 node — deterministic cocotb testbench generator.

Reads: artifacts/<run_id>/01_summary.json   (SpecSummary from Stage 1)
Writes: artifacts/<run_id>/02_testbench.py  (cocotb testbench source)
        artifacts/<run_id>/02_testbench_meta.json  (status artifact for router)

This stage is fully deterministic (template-based, no LLM call). The generator
lives in pipeline/cocotb/generator.py. (Historically specced as "Agent 2"; that
LLM agent was retired in favor of pure templating — there is no agent2.py.)
"""

import json
import traceback
from pathlib import Path

from pipeline.schemas.envelope import write_artifact, write_error
from pipeline.state import PipelineState
from pipeline.schemas.summary_schema import SpecSummary

try:
    from pipeline.cocotb.generator import generate_testbench
    _GEN_AVAILABLE = True
except Exception:
    _GEN_AVAILABLE = False


def run_stage2(state: PipelineState) -> PipelineState:
    """
    LangGraph node for Stage 2.

    Reads 01_summary.json, generates cocotb testbench, writes status artifact.
    Always writes the status artifact so the router never crashes.
    """
    run_id = state["run_id"]
    artifact_dir = Path("artifacts") / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    meta_path = artifact_dir / "02_testbench_meta.json"
    tb_path = artifact_dir / "02_testbench.py"

    # Read Stage 1 output
    summary_path = artifact_dir / "01_summary.json"
    try:
        data = json.loads(summary_path.read_text())
        if data.get("status") != "success":
            _write_error(meta_path, f"Stage 1 did not succeed: {data.get('status')}")
            return state
        summary = SpecSummary.model_validate(data)
    except Exception as exc:
        _write_error(meta_path, f"Failed to load SpecSummary: {exc}\n{traceback.format_exc()}")
        return state

    if not _GEN_AVAILABLE:
        _write_error(meta_path, "pipeline.cocotb.generator could not be imported")
        return state

    try:
        generate_testbench(summary, tb_path)
        # Validate the status envelope (BUG-13) before writing.
        write_artifact(meta_path, {
            "status": "success",
            "testbench_path": str(tb_path),
        })
    except Exception as exc:
        _write_error(meta_path, f"Testbench generation failed: {exc}\n{traceback.format_exc()}")

    return state


def _write_error(path: Path, message: str) -> None:
    # Routed through the validated envelope helper (BUG-13).
    write_error(path, message)
