"""LangGraph pipeline graph with conditional retry/halt edges.

Routing logic
-------------
After each stage node returns, a corresponding router function inspects
``state["halt"]`` and ``state["retry_counts"]`` to decide where to go next:

    halt == True   → END    (max retries exceeded; pipeline stops)
    stage failed   → re-enter the same stage (retry)
    stage ok       → advance to the next stage

Stage failure is detected by checking the JSON artifact written by the node:
if ``"status"`` is ``"failed"`` we route back.  ``"partial"`` is treated as
a recoverable state (advance with a warning) because downstream stages handle
partial inputs with conservative defaults.

Stage 3 has an additional short-circuit: if ``lint_passed == false`` the
node itself sets ``retry_counts["stage3"] > 0`` before returning, so the
router catches it on the very next tick without needing to re-read the file.
"""

from __future__ import annotations

import json
from pathlib import Path

from langgraph.graph import END, StateGraph

from pipeline.nodes.stage1 import stage1_node
from pipeline.nodes.stage2 import stage2_node
from pipeline.nodes.stage3 import stage3_node
from pipeline.nodes.stage4 import stage4_node
from pipeline.state import PipelineState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _artifact_status(run_id: str, filename: str) -> str:
    """Read the ``status`` field from an artifact JSON; return 'missing' if unreadable."""
    try:
        path = Path("artifacts") / run_id / filename
        data = json.loads(path.read_text())
        return data.get("status", "missing")
    except Exception:
        return "missing"


def _lint_passed(run_id: str) -> bool:
    """Return True when the stage3 artifact has lint_passed == true."""
    try:
        path = Path("artifacts") / run_id / "03_rtl_output.json"
        data = json.loads(path.read_text())
        return bool(data.get("lint_passed", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Router functions
# Each router receives the full state and returns a node name (string) or END.
# ---------------------------------------------------------------------------

def _route_after_stage1(state: PipelineState) -> str:
    if state.get("halt"):
        return END
    status = _artifact_status(state["run_id"], "01_formal_spec.json")
    if status == "failed":
        # Re-enter stage1 for a retry (the node increments retry_counts).
        return "stage1"
    # "success" or "partial" both proceed.
    return "stage2"


def _route_after_stage2(state: PipelineState) -> str:
    if state.get("halt"):
        return END
    status = _artifact_status(state["run_id"], "02_pluscal_impl.json")
    if status == "failed":
        return "stage2"
    return "stage3"


def _route_after_stage3(state: PipelineState) -> str:
    if state.get("halt"):
        return END
    status = _artifact_status(state["run_id"], "03_rtl_output.json")
    # "partial" means lint failed but we still have a Verilog file.
    # The node already bumped retry_counts; route back for the lint-error retry.
    if status == "failed" or status == "partial":
        retry_count = state.get("retry_counts", {}).get("stage3", 0)
        from pipeline.nodes.stage3 import MAX_RETRIES as S3_MAX
        if retry_count < S3_MAX:
            return "stage3"
        # Exhausted retries on partial/failed; proceed to evaluation anyway.
    return "stage4"


def _route_after_stage4(state: PipelineState) -> str:
    if state.get("halt"):
        return END
    return END


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_graph():
    workflow = StateGraph(PipelineState)

    workflow.add_node("stage1", stage1_node)
    workflow.add_node("stage2", stage2_node)
    workflow.add_node("stage3", stage3_node)
    workflow.add_node("stage4", stage4_node)

    workflow.set_entry_point("stage1")

    workflow.add_conditional_edges(
        "stage1",
        _route_after_stage1,
        {
            "stage1": "stage1",
            "stage2": "stage2",
            END: END,
        },
    )

    workflow.add_conditional_edges(
        "stage2",
        _route_after_stage2,
        {
            "stage2": "stage2",
            "stage3": "stage3",
            END: END,
        },
    )

    workflow.add_conditional_edges(
        "stage3",
        _route_after_stage3,
        {
            "stage3": "stage3",
            "stage4": "stage4",
            END: END,
        },
    )

    workflow.add_conditional_edges(
        "stage4",
        _route_after_stage4,
        {END: END},
    )

    return workflow.compile()