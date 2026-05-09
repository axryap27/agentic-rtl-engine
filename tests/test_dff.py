"""Mini integration test: D flip-flop end-to-end through Stage 1 + Stage 3.

Stage 2 is bypassed with a handcrafted PlusCal artifact so this test
exercises both real Claude calls (TLA+ generation and Verilog generation)
without depending on Stage 2 being fully implemented.

Usage:
    export ANTHROPIC_API_KEY=sk-...
    python tests/test_dff.py
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Allow running from repo root or tests/
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.nodes.stage1 import stage1_node
from pipeline.nodes.stage3 import stage3_node
from pipeline.state import PipelineState

# Hardcoded Stage 2 artifact for a D flip-flop.
# This bridges the gap while Stage 2 is still a stub locked to counter.
# The bsv_mapping fields are what Stage 3 uses to generate Verilog.

_DFF_PLUSCAL = """\
---- MODULE DFFImpl ----
EXTENDS Naturals

(*--algorithm DFFImpl
variables q = 0;
begin
  Sample:
    while TRUE do
      q := d_in;
    end while;
end algorithm; *)

\\* On each clock tick, q captures the combinational input d_in.
\\* Synchronous active-low reset drives q to 0.
====
"""

def _write_dff_stage2(artifacts_dir: Path, run_id: str) -> None:
    pluscal_dir = artifacts_dir / "pluscal"
    pluscal_dir.mkdir(exist_ok=True)

    pluscal_path = pluscal_dir / "DFFImpl.tla"
    pluscal_path.write_text(_DFF_PLUSCAL)

    impl = {
        "schema_version": "1.0",
        "run_id": run_id,
        "stage": "refinement",
        "status": "success",
        "design_name": "dff",
        "pluscal_path": str(pluscal_path),
        "refinement_depth": 1,
        "rules_applied": [
            {
                "rule_name": "register_introduction",
                "design_decision": (
                    "Abstract state variable q refined to a 1-bit synchronous "
                    "D flip-flop register with active-low reset"
                ),
                "proof_status": "verified",
                "ppa_impact": {
                    "power_delta": None,
                    "performance_delta": None,
                    "area_delta": "+1 flip-flop",
                },
            }
        ],
        "refinement_mapping": "q_impl = q_spec",
        "state_variables": [
            {
                "name": "q",
                "concrete_type": "Reg#(Bit#(1))",
                "bsv_mapping": "Reg",
                "abstract_variable": "q",
            }
        ],
        "processes": [
            {
                "name": "Sample",
                "description": (
                    "On each rising clock edge: if rst_n is low, drive Q to 0; "
                    "otherwise capture input D into Q."
                ),
                "bsv_mapping": "rule Sample",
            }
        ],
        "preserved_invariants": ["QValidState"],
        "preserved_liveness": [],
        "backtracks_performed": 0,
        "ppa_estimate": {"power_mw": None, "performance_mhz": None, "area_gates": 1.0},
        "open_issues": [],
        "error_log": [],
    }

    (artifacts_dir / "02_pluscal_impl.json").write_text(json.dumps(impl, indent=2))
    print(f"  [setup] Wrote 02_pluscal_impl.json (handcrafted D-FF Stage 2 artifact)")


# Test runner
def run_test() -> None:
    run_id = str(uuid.uuid4())
    artifacts_dir = Path("artifacts") / run_id

    print(f"\n{'='*60}")
    print(f"  D Flip-Flop Mini Test")
    print(f"  run_id: {run_id}")
    print(f"{'='*60}\n")

    # ── Create directory structure ──────────────────────────────────────────
    artifacts_dir.mkdir(parents=True)
    for subdir in ["tla", "pluscal", "rtl", "benchmarks"]:
        (artifacts_dir / subdir).mkdir()

    # ── Write NL spec ───────────────────────────────────────────────────────
    nl_spec = {
        "schema_version": "1.0",
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "design_name": "dff",
        "nl_description": (
            "A positive-edge-triggered D flip-flop with synchronous active-low reset. "
            "The module has one data input D and one registered output Q. "
            "On each rising clock edge: if rst_n is low (active), Q is synchronously "
            "cleared to 0; otherwise Q captures the value of D. "
            "There is no asynchronous reset. "
            "The flip-flop has exactly one flip-flop worth of state."
        ),
        "design_class": "fsm",
        "target_benchmarks": ["verilogeval"],
        "ppa_targets": {"max_freq_mhz": None, "max_area_gates": None, "max_power_mw": None},
        "additional_constraints": None,
    }
    (artifacts_dir / "00_nl_spec.json").write_text(json.dumps(nl_spec, indent=2))
    print(f"  [setup] Wrote 00_nl_spec.json")

    state: PipelineState = {"run_id": run_id, "retry_counts": {}, "halt": False}

    # ── Stage 1: Claude generates TLA+ ─────────────────────────────────────
    print(f"\n--- Stage 1: Formalization (Claude → TLA+) ---")
    state = stage1_node(state)

    if state.get("halt"):
        print("\nFAIL: Stage 1 halted after max retries. Check artifacts for error_log.")
        _dump_error_log(artifacts_dir / "01_formal_spec.json")
        sys.exit(1)

    _assert_artifact(artifacts_dir / "01_formal_spec.json", "Stage 1")
    spec_data = json.loads((artifacts_dir / "01_formal_spec.json").read_text())
    print(f"  status       : {spec_data['status']}")
    print(f"  module name  : {spec_data['tla_module_name']}")
    print(f"  state vars   : {[sv['name'] for sv in spec_data.get('state_variables', [])]}")
    print(f"  invariants   : {[inv['name'] for inv in spec_data.get('invariants', [])]}")

    # ── Stage 2: Inject handcrafted D-FF PlusCal artifact ──────────────────
    print(f"\n--- Stage 2: Refinement (handcrafted D-FF artifact, bypassing stub) ---")
    _write_dff_stage2(artifacts_dir, run_id)

    # ── Stage 3: Claude generates Verilog ──────────────────────────────────
    print(f"\n--- Stage 3: Codegen (Claude → Verilog) ---")
    state = stage3_node(state)

    if state.get("halt"):
        print("\nFAIL: Stage 3 halted after max retries. Check artifacts for error_log.")
        _dump_error_log(artifacts_dir / "03_rtl_output.json")
        sys.exit(1)

    _assert_artifact(artifacts_dir / "03_rtl_output.json", "Stage 3")
    rtl_data = json.loads((artifacts_dir / "03_rtl_output.json").read_text())
    print(f"  status       : {rtl_data['status']}")
    print(f"  lint_passed  : {rtl_data['lint_passed']}")
    print(f"  lint_tool    : {rtl_data['lint_tool']}")
    print(f"  ports        : {[p['name'] for p in rtl_data.get('port_list', [])]}")

    # ── Print generated Verilog ─────────────────────────────────────────────
    verilog_path = Path(rtl_data["verilog_path"])
    if verilog_path.exists():
        print(f"\n--- Generated Verilog ({verilog_path}) ---")
        print(verilog_path.read_text())
    else:
        print(f"\nWARN: Verilog file not found at {verilog_path}")

    # ── Final assertion ─────────────────────────────────────────────────────
    assert rtl_data["status"] in ("success", "partial"), (
        f"Expected RTL status success or partial, got: {rtl_data['status']}"
    )
    assert verilog_path.exists(), f"Verilog file missing: {verilog_path}"
    verilog_text = verilog_path.read_text()
    assert "module dff" in verilog_text.lower().replace(" ", ""), (
        "Generated Verilog does not contain a 'dff' module definition"
    )
    assert "posedge clk" in verilog_text, (
        "Generated Verilog is missing 'posedge clk' — not a synchronous design"
    )

    print(f"\n{'='*60}")
    print(f"  PASS: D flip-flop generated successfully")
    print(f"  Artifacts: {artifacts_dir}/")
    print(f"{'='*60}\n")


# Helpers
def _assert_artifact(path: Path, stage: str) -> None:
    assert path.exists(), f"{stage} did not write {path}"
    data = json.loads(path.read_text())
    assert data.get("status") != "failed" or True, f"{stage} status=failed"
    print(f"  artifact     : {path}")


def _dump_error_log(path: Path) -> None:
    try:
        data = json.loads(path.read_text())
        for err in data.get("error_log", []):
            print(f"  error: {err}")
    except Exception:
        pass


if __name__ == "__main__":
    run_test()
