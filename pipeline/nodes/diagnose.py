"""
Diagnose node — classifies a cocotb failure and writes the routing signal.

Reads:  artifacts/<run_id>/04_evaluation.json
        artifacts/<run_id>/02_formal_spec.json
        artifacts/<run_id>/refinement_chain.json
Writes: artifacts/<run_id>/04_diagnosis.json

Sets state["last_diagnosis"] to "spec" or "refinement".
The downstream edge function (_route_after_diagnose in graph.py) reads this
field to decide whether to route to stage3_revise_cocotb or
stage3_backtrack_refinement.
"""

import json
import traceback
from pathlib import Path

from pipeline.schemas.envelope import write_artifact
from pipeline.state import PipelineState


def run_diagnose(state: PipelineState) -> PipelineState:
    """
    LangGraph node: classify the cocotb failure, write 04_diagnosis.json,
    and store the result in state["last_diagnosis"].

    Always sets state["last_diagnosis"] (defaults to "spec" on any error)
    so the edge function always has a valid routing signal.
    """
    run_id = state["run_id"]
    artifact_dir = Path("artifacts") / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    diag_path = artifact_dir / "04_diagnosis.json"

    try:
        from pipeline.agents import agent_diagnoser
        result = agent_diagnoser.diagnose(run_id)
        # Validate the status envelope (BUG-13) before writing.
        write_artifact(diag_path, {"status": "success", **result})
        state["last_diagnosis"] = result.get("failure_type", "spec")
    except Exception as exc:
        msg = f"Diagnoser node failed: {exc}\n{traceback.format_exc()}"
        write_artifact(diag_path, {
            "status":       "error",
            "error":        msg,
            "failure_type": "spec",
            "explanation":  "Diagnoser crashed; defaulting to spec revision.",
        })
        state["last_diagnosis"] = "spec"

    return state
