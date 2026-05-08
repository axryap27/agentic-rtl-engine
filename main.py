import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pipeline.graph import build_graph
from pipeline.state import PipelineState


def main():
    run_id = str(uuid.uuid4())
    artifacts_dir = Path("artifacts") / run_id
    artifacts_dir.mkdir(parents=True)
    for subdir in ["tla", "pluscal", "rtl", "benchmarks"]:
        (artifacts_dir / subdir).mkdir()

    nl_spec = {
        "schema_version": "1.0",
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "design_name": "counter_2bit",
        "nl_description": (
            "A 2-bit synchronous counter that increments by 1 on each rising clock edge "
            "and wraps from 3 back to 0. Synchronous active-low reset drives count to 0."
        ),
        "design_class": "fsm",
        "target_benchmarks": ["verilogeval", "rtllm", "cvdp"],
        "ppa_targets": {"max_freq_mhz": None, "max_area_gates": None, "max_power_mw": None},
        "additional_constraints": None,
    }
    (artifacts_dir / "00_nl_spec.json").write_text(json.dumps(nl_spec, indent=2))

    print(f"run_id : {run_id}")
    print(f"artifacts: {artifacts_dir}/")
    print()

    app = build_graph()
    initial_state: PipelineState = {"run_id": run_id, "retry_counts": {}, "halt": False}
    app.invoke(initial_state)

    print()
    print("Pipeline complete.")
    print(f"Results in: {artifacts_dir}/")


if __name__ == "__main__":
    main()
