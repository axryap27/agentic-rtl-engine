#!/usr/bin/env python3.11
"""
Agentic RTL Engine — pipeline entry point.

Usage:
    python3.11 main.py                         # run with default 2-bit counter spec
    python3.11 main.py "your NL spec here"    # run with a custom NL spec
    python3.11 main.py --clean-artifacts [N]   # prune all but the N newest runs (default 10)

What this script does:
1. Creates a date-stamped run directory under artifacts/<YYYY-MM-DD>/<HHMMSS>-<hash>/.
2. Writes the NL prompt to 00_nl_spec.json (seeds the artifact chain).
3. Invokes the LangGraph pipeline.
4. Renames the run dir to fold in the module name, refreshes artifacts/latest,
   and prints a summary of the terminal result.

Environment: copy .env.example to .env and fill in credentials before running.
"""

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # must happen before any pipeline imports that read env vars

from pipeline.graph import get_graph
from pipeline.state import PipelineState
from pipeline.run_dirs import new_run_id, finalize_run_dir, clean_artifacts

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
    # Housekeeping mode: prune old runs and exit (no pipeline invocation).
    if len(sys.argv) > 1 and sys.argv[1] == "--clean-artifacts":
        keep = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 10
        deleted, kept = clean_artifacts(keep=keep)
        print(f"[main] removed {deleted} old run(s), kept {kept}")
        return

    # Determine NL spec: from CLI arg or default
    if len(sys.argv) > 1:
        nl_prompt = " ".join(sys.argv[1:])
    else:
        nl_prompt = _DEFAULT_SPEC
        print("[main] Using default 2-bit counter spec.")

    # Create a date-stamped run ID and artifact directory. run_id is a relative
    # path ("YYYY-MM-DD/HHMMSS-<hash>"); the module name is spliced into the leaf
    # after the run completes (finalize_run_dir).
    run_id = new_run_id()
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

    # Fold the module name into the run dir leaf and refresh artifacts/latest.
    # (Safe post-run: the graph is done, so no in-flight paths reference the dir.)
    artifact_dir, run_id = finalize_run_dir(artifact_dir)

    # Report terminal result
    print("\n[main] Pipeline complete.")
    print(f"[main] Run dir: {artifact_dir}/  (artifacts/latest -> this run)")
    print(f"[main] Final retry counts: {final_state.get('retry_counts', {})}")

    eval_path = artifact_dir / "04_evaluation.json"
    rtl_path = artifact_dir / "03_rtl_output.json"

    if eval_path.exists():
        eval_data = json.loads(eval_path.read_text())
        status = eval_data.get("status", "unknown")
        print(f"[main] Evaluation status: {status}")
        if status == "success":
            # cocotb passed against the spec-derived reference. Any Agent-1/spec
            # vector disagreement is still recorded on 04_evaluation.json and
            # 02_vector_check.json — it is just not shouted to the terminal.
            print("[main] SUCCESS — RTL passed cocotb testbench.")
            soak = eval_data.get("soak")
            if soak and soak.get("status") == "failed":
                print(
                    f"[main] SOAK FAILURE — the RTL diverged from the refined spec "
                    f"on the {soak.get('cycles')}-cycle random soak "
                    f"({soak.get('num_failed_vectors')} failing vector(s); seed "
                    f"{soak.get('seed')}). This is a genuine spec-vs-RTL bug "
                    f"(codegen/composition), NOT an Agent error — review 04_soak.json."
                )
            elif soak and soak.get("status") == "success":
                print(
                    f"[main] Soak: RTL matched the spec on {soak.get('cycles')} "
                    f"random cycles (seed {soak.get('seed')})."
                )
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
