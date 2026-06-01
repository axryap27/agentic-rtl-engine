"""
Stage 1 node — Agent 1: NL → SpecSummary (JSON(S)).

Reads:  artifacts/<run_id>/00_nl_spec.json   (written by main.py at run start)
Writes: artifacts/<run_id>/01_summary.json   (SpecSummary JSON(S))

CANONICAL ARTIFACT MAP (authoritative — resolves CLAUDE.md vs schema docstring conflict):

  00_nl_spec.json       — the raw NL prompt, written by main.py at run start.
                          Contains {"prompt": str}. NOT produced by any stage.
  01_summary.json       — JSON(S) / SpecSummary. Written by Stage 1 (this node).
                          Read by Stage 2 (cocotb generator) and Stage 3.
  02_formal_spec.json   — JSON(TLA) / FormalSpec. Written by Stage 3 (Agent 3).
                          Read by Compiler 1, Refinement Engine.
  03_rtl_output.json    — Verilog-2001 RTL. Written by Compiler 2.
                          status: success | partial | error
  04_evaluation.json    — cocotb run result. Written by Stage 4 (runner).
                          status: success | error
  refinement_chain.json — Ordered rule applications. Written by Refinement Engine.

Note: CLAUDE.md calls file 01 "formal_spec" but Stage 1 only produces a
SpecSummary, not a FormalSpec. We use "01_summary.json" to match the content.
The "02_pluscal_impl.json" in CLAUDE.md does not exist in the 3-agent design;
the formal spec is written directly to 02_formal_spec.json by Agent 3 (Stage 3).
"""

import json
import traceback
from pathlib import Path

from pipeline.state import PipelineState

try:
    from pipeline.agents import agent1
    _AGENT1_AVAILABLE = True
except Exception:
    _AGENT1_AVAILABLE = False


def run_stage1(state: PipelineState) -> PipelineState:
    """
    LangGraph node for Stage 1.

    Reads 00_nl_spec.json, calls Agent 1, writes 01_summary.json.
    Always writes the artifact (even on failure) so the router never crashes.
    """
    run_id = state["run_id"]
    artifact_dir = Path("artifacts") / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    out_path = artifact_dir / "01_summary.json"

    # Read input
    nl_path = artifact_dir / "00_nl_spec.json"
    try:
        nl_data = json.loads(nl_path.read_text())
        prompt = nl_data["prompt"]
    except Exception as exc:
        _write_error(out_path, f"Failed to read 00_nl_spec.json: {exc}")
        return state

    if not _AGENT1_AVAILABLE:
        _write_error(out_path, "pipeline.agents.agent1 could not be imported")
        # Increment retry counter so the edge function knows how many attempts
        # have been made. Nodes own state writes; edge functions only route.
        state["retry_counts"]["stage1"] = state["retry_counts"].get("stage1", 0) + 1
        return state

    # Call Agent 1
    try:
        summary = agent1.run(prompt)
        artifact = summary.model_dump()
        artifact["status"] = "success"
        out_path.write_text(json.dumps(artifact, indent=2))
    except Exception as exc:
        _write_error(out_path, f"Agent 1 failed: {exc}\n{traceback.format_exc()}")
        # Increment retry counter on failure so the edge function can decide
        # whether to route to retry or halt without mutating state itself.
        state["retry_counts"]["stage1"] = state["retry_counts"].get("stage1", 0) + 1

    return state


def _write_error(path: Path, message: str) -> None:
    path.write_text(json.dumps({"status": "error", "error": message}, indent=2))
