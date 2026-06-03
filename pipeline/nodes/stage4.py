"""
Stage 4 node — cocotb simulation runner.

Reads:  artifacts/<run_id>/03_rtl_output.json       (Verilog path + module name)
        artifacts/<run_id>/02_testbench_meta.json    (testbench path from Stage 2)
Writes: artifacts/<run_id>/04_evaluation.json       (status: success | error)

On cocotb failure the graph routes back to Stage 3 for an Agent 3
`revise_on_cocotb` call (managed by the LangGraph conditional edge).
"""

import json
import traceback
from pathlib import Path

from pipeline.schemas.envelope import write_artifact, write_error
from pipeline.state import PipelineState

try:
    from pipeline.cocotb.runner import run_testbench
    _RUNNER_AVAILABLE = True
except Exception:
    _RUNNER_AVAILABLE = False


def run_stage4(state: PipelineState) -> PipelineState:
    """
    LangGraph node for Stage 4.

    Runs the cocotb testbench against the generated Verilog.
    Always writes 04_evaluation.json before returning.
    """
    run_id = state["run_id"]
    artifact_dir = Path("artifacts") / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    eval_path = artifact_dir / "04_evaluation.json"

    # Load RTL artifact
    rtl_meta_path = artifact_dir / "03_rtl_output.json"
    try:
        rtl_data = json.loads(rtl_meta_path.read_text())
        if rtl_data.get("status") not in ("success", "partial"):
            _write_error(eval_path, f"Stage 3 did not produce usable RTL: {rtl_data.get('status')}")
            return state
        verilog_path = Path(rtl_data["verilog_path"])
        module_name = rtl_data["module_name"]
    except Exception as exc:
        _write_error(eval_path, f"Failed to read 03_rtl_output.json: {exc}\n{traceback.format_exc()}")
        return state

    # Load testbench artifact
    tb_meta_path = artifact_dir / "02_testbench_meta.json"
    try:
        tb_data = json.loads(tb_meta_path.read_text())
        if tb_data.get("status") != "success":
            _write_error(eval_path, f"Stage 2 did not produce a testbench: {tb_data.get('status')}")
            return state
        testbench_path = Path(tb_data["testbench_path"])
    except Exception as exc:
        _write_error(eval_path, f"Failed to read 02_testbench_meta.json: {exc}\n{traceback.format_exc()}")
        return state

    if not _RUNNER_AVAILABLE:
        _write_error(eval_path, "pipeline.cocotb.runner could not be imported")
        return state

    # Run simulation
    try:
        result = run_testbench(testbench_path, verilog_path, module_name)
        # Validate the status envelope (BUG-13) before writing.
        if result.get("status") == "pass":
            write_artifact(eval_path, {"status": "success"})
        else:
            write_artifact(eval_path, {
                "status":         "error",
                "phase":          result.get("phase", "unknown"),
                "error":          result.get("error", "Unknown simulation failure"),
                "failed_vectors": result.get("failed_vectors", []),
                "raw":            result.get("raw", ""),
            })
    except Exception as exc:
        _write_error(eval_path, f"run_testbench raised: {exc}\n{traceback.format_exc()}")

    return state


def _write_error(path: Path, message: str) -> None:
    # Routed through the validated envelope helper (BUG-13).
    write_error(path, message)
