#!/usr/bin/env python3.11
"""
Agentic RTL Engine — pipeline entry point.

Usage:
    python3.11 main.py                         # run with default 2-bit counter spec
    python3.11 main.py "your NL spec here"    # run with a custom NL spec

What this script does:
1. Creates a unique run directory under artifacts/<run_id>/.
2. Writes the NL prompt to 00_nl_spec.json (seeds the artifact chain).
3. Invokes the LangGraph pipeline.
4. Prints a summary of the terminal result.

Environment: copy .env.example to .env and fill in credentials before running.
"""

import json
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # must happen before any pipeline imports that read env vars

from pipeline.graph import get_graph
from pipeline.state import PipelineState

# ---------------------------------------------------------------------------
# Default specification
# ---------------------------------------------------------------------------

_DEFAULT_SPEC = """\
Design a synchronous 2-bit binary up-counter in Verilog-2001.
The counter has a clock (clk), an active-high synchronous reset (rst), and a
2-bit output (count). On every rising edge of clk the counter increments by 1,
wrapping from 3 back to 0. When rst is asserted the counter resets to 0 on the
next rising clock edge.
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Determine NL spec: from CLI arg or default
    if len(sys.argv) > 1:
        nl_prompt = " ".join(sys.argv[1:])
    else:
        nl_prompt = _DEFAULT_SPEC
        print("[main] Using default 2-bit counter spec.")

    # Create a unique run ID and artifact directory
    run_id = uuid.uuid4().hex[:12]
    artifact_dir = Path("artifacts") / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    print(f"[main] Run ID: {run_id}")
    print(f"[main] Artifacts: {artifact_dir}/")

    # Seed the artifact chain: write 00_nl_spec.json
    nl_artifact = artifact_dir / "00_nl_spec.json"
    nl_artifact.write_text(json.dumps({"prompt": nl_prompt}, indent=2))
    print(f"[main] Wrote {nl_artifact}")

    # Build initial state
    initial_state: PipelineState = {
        "run_id": run_id,
        "retry_counts": {},
        "halt": False,
    }

    # Run the pipeline
    print("[main] Starting pipeline...")
    graph = get_graph()
    final_state = graph.invoke(initial_state)

    # Report terminal result
    print("\n[main] Pipeline complete.")
    print(f"[main] Final retry counts: {final_state.get('retry_counts', {})}")

    eval_path = artifact_dir / "04_evaluation.json"
    rtl_path = artifact_dir / "03_rtl_output.json"

    if eval_path.exists():
        eval_data = json.loads(eval_path.read_text())
        status = eval_data.get("status", "unknown")
        print(f"[main] Evaluation status: {status}")
        if status == "success":
            print("[main] SUCCESS — RTL passed cocotb testbench.")
        else:
            print(f"[main] FAIL — {eval_data.get('error', 'no detail')}")
    elif rtl_path.exists():
        rtl_data = json.loads(rtl_path.read_text())
        status = rtl_data.get("status", "unknown")
        print(f"[main] RTL status: {status} (evaluation did not run)")
        if status == "success":
            print(f"[main] Verilog written to: {rtl_data.get('verilog_path', 'unknown')}")
    else:
        print("[main] Pipeline halted before RTL generation.")
        # Print the most recent error we can find
        for filename in ["02_formal_spec.json", "01_summary.json"]:
            p = artifact_dir / filename
            if p.exists():
                data = json.loads(p.read_text())
                if data.get("status") == "error":
                    print(f"[main] Error in {filename}: {data.get('error', '')[:200]}")
                break


if __name__ == "__main__":
    main()
