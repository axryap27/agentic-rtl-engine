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
  04_evaluation.json          Stage 4 (cocotb)    {"status"} | {"status","error"}
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
           → error (retry_counts["stage4_cocotb"] < 2): revise_on_cocotb (re-run stage3)
           → error (exhausted): halt
"""

import json
from pathlib import Path

from langgraph.graph import StateGraph, END

from pipeline.state import PipelineState
from pipeline.nodes.stage1 import run_stage1
from pipeline.nodes.stage2 import run_stage2
from pipeline.nodes.stage3 import run_stage3
from pipeline.nodes.stage4 import run_stage4

# ---------------------------------------------------------------------------
# Retry limits
# ---------------------------------------------------------------------------

_MAX_STAGE1_RETRIES = 1        # Agent 1 one-shot; one retry on parse failure
_MAX_COCOTB_RETRIES = 2        # Agent 3 revise_on_cocotb (Stage 4 failure)


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
# Stage 3 revise-on-cocotb wrapper
# ---------------------------------------------------------------------------

def run_stage3_revise_cocotb(state: PipelineState) -> PipelineState:
    """
    Wraps Stage 3 for the cocotb-failure retry path.

    Injects the cocotb sim log from 04_evaluation.json and calls
    agent3.revise_on_cocotb before re-running the formal branch.
    This node runs in place of the normal stage3 node on retry.

    Increments retry_counts["stage4_cocotb"] before doing any work so the
    edge function after stage3 (re-routed here) has the correct count if it
    needs to halt on the next failure.
    """
    run_id = state["run_id"]
    # Increment cocotb retry counter in the node (not in the edge function)
    # so it persists in the state returned to LangGraph.
    state["retry_counts"]["stage4_cocotb"] = (
        state["retry_counts"].get("stage4_cocotb", 0) + 1
    )
    artifact_dir = Path("artifacts") / run_id

    formal_path = artifact_dir / "02_formal_spec.json"
    rtl_path = artifact_dir / "03_rtl_output.json"

    # Load sim log from the failed evaluation
    eval_path = artifact_dir / "04_evaluation.json"
    sim_log = ""
    try:
        eval_data = json.loads(eval_path.read_text())
        sim_log = eval_data.get("error", "")
    except Exception:
        pass

    # Load the current formal spec
    try:
        from pipeline.schemas.tla_schema import FormalSpec
        spec_data = json.loads(formal_path.read_text())
        spec = FormalSpec.model_validate(spec_data)
    except Exception as exc:
        _write_node_error(rtl_path, f"revise_on_cocotb: cannot load FormalSpec: {exc}")
        return state

    # Revise via Agent 3
    try:
        from pipeline.agents import agent3
        revised = agent3.revise_on_cocotb(spec, sim_log)
        # Overwrite the formal spec so stage3 picks up the revision
        artifact = revised.model_dump()
        artifact["status"] = "success"
        formal_path.write_text(json.dumps(artifact, indent=2))
    except Exception as exc:
        _write_node_error(rtl_path, f"revise_on_cocotb failed: {exc}")
        return state

    # Re-run the full formal branch with the revised spec
    return run_stage3(state)


def _write_node_error(path: Path, message: str) -> None:
    path.write_text(json.dumps({"status": "error", "error": message}, indent=2))


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
    # retry_counts["stage4_cocotb"] is incremented by run_stage3_revise_cocotb
    # before it re-invokes stage3 (the revise path). Edge functions only route.
    retries = state["retry_counts"].get("stage4_cocotb", 0)
    if retries < _MAX_COCOTB_RETRIES:
        return "revise"
    return "halt"


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
        done | stage3_revise_cocotb → stage3 loop | halt

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
    builder.add_node("stage3_revise_cocotb", run_stage3_revise_cocotb)
    builder.add_node("stage4", run_stage4)

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

    # Stage 4 → conditional: done | revise (cocotb failure retry) | halt
    builder.add_conditional_edges(
        "stage4",
        _route_after_stage4,
        {
            "done": END,
            "revise": "stage3_revise_cocotb",
            "halt": END,
        },
    )

    # Cocotb-revision path: revise formal spec then re-run stage3
    builder.add_conditional_edges(
        "stage3_revise_cocotb",
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
