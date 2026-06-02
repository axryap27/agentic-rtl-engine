"""
LangGraph orchestration for the agentic-rtl-engine pipeline.

=== CANONICAL ARTIFACT MAP ===
(Resolves the conflict between CLAUDE.md's table and schema-file docstrings.
 This mapping is authoritative for all LangGraph routing decisions.)

  File                        Written by          Content / Pydantic model
  -----------------------------------------------------------------------
  00_nl_spec.json             main.py             {"prompt": str}  (not a stage output)
  01_summary.json             Stage 1 (Agent 1)   SpecSummary  (JSON(S))
  02_testbench_meta.json      Stage 2 (Agent 2)   {"status", "testbench_path"}
  02_testbench.py             Stage 2             cocotb testbench source (no schema)
  02_formal_spec.json         Stage 3 (Agent 3)   FormalSpec   (JSON(TLA))
  03_rtl_output.json          Stage 3 (Compiler2) {"status","module_name","verilog_path","verilog"}
  04_evaluation.json          Stage 4 (cocotb)    {"status"} | {"status","error","phase",...}
  04_diagnosis.json           Diagnose node        {"status","failure_type","explanation"}
  refinement_chain.json       Refinement Engine   [{rule_name, params}]  (not routed on)

Note: CLAUDE.md's "02_pluscal_impl.json" does not exist in the 3-agent design.
The formal spec (JSON(TLA)) written by Agent 3 is 02_formal_spec.json.
CLAUDE.md's "01_formal_spec.json" label refers to the SpecSummary here named
01_summary.json — the SpecSummaryArtifact docstring ("00_nl_spec.json") is
wrong about which file holds it; Stage 1 writes 01_summary.json.

=== ROUTING LOGIC ===
Every conditional edge function reads the `status` field from the artifact JSON
on disk. It never routes on Python return values or exceptions.

  Stage 1 → success: advance to stage2 (sequential; stage2 → stage3)
           → error (retry_counts["stage1"] < _MAX_STAGE1_RETRIES): retry stage1
           → error (exhausted): halt

  Stage 3 TLC loop → managed inside the stage3 node (up to 3 retries).
  Stage 3 → success: advance to stage4
           → partial: advance to stage4 (best-effort)
           → error: halt

  Stage 4 → success: terminal (done)
           → error (retry_counts["stage4_cocotb"] < _MAX_COCOTB_RETRIES): diagnose
           → error (exhausted): halt

  Diagnose → last_diagnosis == "spec":        stage3_revise_cocotb
           → last_diagnosis == "refinement":  stage3_backtrack_refinement

  stage3_revise_cocotb / stage3_backtrack_refinement → advance to stage4 | halt
"""

import json
from pathlib import Path

from langgraph.graph import StateGraph, END

from pipeline.state import PipelineState
from pipeline.nodes.stage1 import run_stage1
from pipeline.nodes.stage2 import run_stage2
from pipeline.nodes.stage3 import (
    run_stage3,
    run_stage3_revise_cocotb,
    run_stage3_backtrack_refinement,
)
from pipeline.nodes.stage4 import run_stage4
from pipeline.nodes.diagnose import run_diagnose

# ---------------------------------------------------------------------------
# Retry limits
# ---------------------------------------------------------------------------

_MAX_STAGE1_RETRIES = 1        # Agent 1 one-shot; one retry on parse failure
_MAX_COCOTB_RETRIES = 2        # total revision + backtrack attempts (Stage 4 failure)


# ---------------------------------------------------------------------------
# Artifact status reader
# ---------------------------------------------------------------------------

def _read_status(run_id: str, filename: str) -> str:
    """Read the 'status' field from an artifact JSON. Returns 'error' if unreadable."""
    path = Path("artifacts") / run_id / filename
    try:
        data = json.loads(path.read_text())
        return data.get("status", "error")
    except Exception:
        return "error"


# ---------------------------------------------------------------------------
# Conditional edge functions
# (These are the ONLY places that read artifact status — nodes never route)
# ---------------------------------------------------------------------------

def _route_after_stage1(state: PipelineState) -> str:
    status = _read_status(state["run_id"], "01_summary.json")
    if status == "success":
        return "advance"
    # retry_counts["stage1"] is incremented by the stage1 node on each failure
    # before returning, so this edge function only reads it for routing decisions.
    # Check: retries <= _MAX_STAGE1_RETRIES because the node already incremented
    # before we get here (first failure → count=1, first retry allowed if ≤ limit).
    retries = state["retry_counts"].get("stage1", 0)
    if retries <= _MAX_STAGE1_RETRIES:
        return "retry"
    return "halt"


def _route_after_stage3(state: PipelineState) -> str:
    status = _read_status(state["run_id"], "03_rtl_output.json")
    if status in ("success", "partial"):
        return "advance"
    return "halt"


def _route_after_stage4(state: PipelineState) -> str:
    status = _read_status(state["run_id"], "04_evaluation.json")
    if status == "success":
        return "done"
    retries = state["retry_counts"].get("stage4_cocotb", 0)
    if retries < _MAX_COCOTB_RETRIES:
        return "diagnose"
    return "halt"


def _route_after_diagnose(state: PipelineState) -> str:
    """Route to spec revision or refinement backtrack based on diagnoser output."""
    diagnosis = state.get("last_diagnosis", "spec")
    if diagnosis == "refinement":
        return "backtrack"
    return "revise_spec"


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_graph() -> StateGraph:
    """
    Build and return the compiled LangGraph StateGraph.

    Node execution order (sequential in LangGraph 0.2 single-thread mode):

        stage1
          ↓ (on success)
        stage2   (writes testbench artifact; no LLM call — fast)
          ↓
        stage3   (Agent 3: formal spec → TLC loop → refinement → Verilog)
          ↓ (on success or partial)
        stage4   (cocotb runner)
          ↓
        done | diagnose | halt
                  ↓
        stage3_revise_cocotb (spec fault)
        stage3_backtrack_refinement (refinement fault)
          ↓ (on success or partial)
        stage4

    Note: stage2 and stage3 run sequentially, not truly in parallel. This is
    intentional for LangGraph 0.2 compatibility — stage4 needs both their
    artifacts, and the sequential order guarantees both are written before
    stage4 starts. stage2 is fast (deterministic template expansion; no LLM).
    """
    builder = StateGraph(PipelineState)

    # Register nodes
    builder.add_node("stage1", run_stage1)
    builder.add_node("stage2", run_stage2)
    builder.add_node("stage3", run_stage3)
    builder.add_node("stage4", run_stage4)
    builder.add_node("diagnose", run_diagnose)
    builder.add_node("stage3_revise_cocotb", run_stage3_revise_cocotb)
    builder.add_node("stage3_backtrack_refinement", run_stage3_backtrack_refinement)

    # Entrypoint
    builder.set_entry_point("stage1")

    # Stage 1 → conditional: advance (success) | retry | halt
    builder.add_conditional_edges(
        "stage1",
        _route_after_stage1,
        {
            "advance": "stage2",
            "retry": "stage1",
            "halt": END,
        },
    )

    # Stage 2 always advances to stage3 (its own errors are written to disk;
    # stage3 checks the testbench artifact and degrades gracefully if missing).
    builder.add_edge("stage2", "stage3")

    # Stage 3 → conditional: advance (RTL ready) | halt
    builder.add_conditional_edges(
        "stage3",
        _route_after_stage3,
        {
            "advance": "stage4",
            "halt": END,
        },
    )

    # Stage 4 → conditional: done | diagnose (cocotb failure) | halt
    builder.add_conditional_edges(
        "stage4",
        _route_after_stage4,
        {
            "done": END,
            "diagnose": "diagnose",
            "halt": END,
        },
    )

    # Diagnose → conditional: spec revision | refinement backtrack
    builder.add_conditional_edges(
        "diagnose",
        _route_after_diagnose,
        {
            "revise_spec": "stage3_revise_cocotb",
            "backtrack":   "stage3_backtrack_refinement",
        },
    )

    # Both revision paths re-enter stage4 after generating new RTL
    builder.add_conditional_edges(
        "stage3_revise_cocotb",
        _route_after_stage3,
        {
            "advance": "stage4",
            "halt": END,
        },
    )

    builder.add_conditional_edges(
        "stage3_backtrack_refinement",
        _route_after_stage3,
        {
            "advance": "stage4",
            "halt": END,
        },
    )

    return builder.compile()


# Singleton compiled graph
_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph
