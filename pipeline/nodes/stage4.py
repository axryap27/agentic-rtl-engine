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

    # ---- Spec-derived golden-vector cross-check (false-red removal) ----
    # Agent 1 hand-computes the golden vectors; on deep sequential designs its
    # arithmetic is fragile (the live FIFO run failed a CORRECT RTL on one bad
    # vector). Re-derive the expected outputs from the REFINED spec via an
    # independent interpreter so a correct RTL is never failed by a wrong Agent-1
    # vector, and surface any Agent-1/spec disagreement in 02_vector_check.json.
    # Best-effort: on any issue we run Agent 1's original testbench unchanged.
    vector_check = None
    try:
        from pipeline.cocotb.vector_check import apply_spec_derived_vectors
        vc = apply_spec_derived_vectors(artifact_dir)
        if vc is not None:
            testbench_path = vc["testbench_path"]
            vector_check = vc
    except Exception:
        vector_check = None

    # Run simulation
    try:
        result = run_testbench(testbench_path, verilog_path, module_name)
        # Validate the status envelope (BUG-13) before writing.
        if result.get("status") == "pass":
            eval_artifact = {"status": "success"}
            # cocotb passed against the SPEC-DERIVED reference. If Agent 1's golden
            # vectors DISAGREED with the spec, the RTL matches the spec but not
            # Agent 1 — EITHER an Agent-1 arithmetic slip (a false red this feature
            # avoided) OR a spec/intent bug. Record it so the pass is NOT reported
            # as clean and a possible spec bug is visible, not silently shipped
            # green. (status stays 'success' so routing is unchanged; the signal is
            # carried on the artifact + surfaced by main.py.)
            if vector_check is not None and not vector_check.get("agreed", True):
                report = vector_check.get("report", {})
                eval_artifact["vector_disagreement"] = report.get("disagreements", [])
                eval_artifact["vector_check_note"] = (
                    "cocotb passed against spec-derived expecteds, but Agent 1's "
                    "golden vectors disagree with the spec at the listed vectors — "
                    "either an Agent-1 vector error (a false red avoided) or a "
                    "spec/intent bug. Review 02_vector_check.json."
                )
            write_artifact(eval_path, eval_artifact)
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
