import json
from pathlib import Path

from pipeline.schemas import (
    ConcreteStateVariable, FormalSpec, PlusCalImpl, PPAEstimate, PPAImpact, Process, RuleApplied,
)
from pipeline.state import PipelineState

_PLUSCAL_CONTENT = """\
---- MODULE CounterImpl ----
EXTENDS Naturals

(*--algorithm CounterImpl
variables count = 0;
begin
  Loop:
    while TRUE do
      count := (count + 1) % 4;
    end while;
end algorithm; *)

\\* PlusCal translation placeholder
====
"""


def stage2_node(state: PipelineState) -> PipelineState:
    run_id = state["run_id"]
    artifacts_dir = Path("artifacts") / run_id

    with open(artifacts_dir / "01_formal_spec.json") as f:
        FormalSpec.model_validate(json.load(f))

    pluscal_dir = artifacts_dir / "pluscal"
    pluscal_dir.mkdir(exist_ok=True)

    pluscal_path = pluscal_dir / "CounterImpl.tla"
    pluscal_path.write_text(_PLUSCAL_CONTENT)

    impl = PlusCalImpl(
        run_id=run_id,
        status="success",
        design_name="counter_2bit",
        pluscal_path=str(pluscal_path),
        refinement_depth=1,
        rules_applied=[
            RuleApplied(
                rule_name="register_introduction",
                design_decision="Abstract count variable refined to a 2-bit hardware register",
                proof_status="verified",
                ppa_impact=PPAImpact(
                    power_delta=None,
                    performance_delta=None,
                    area_delta="+2 flip-flops",
                ),
            )
        ],
        refinement_mapping="count_impl = count_spec",
        state_variables=[
            ConcreteStateVariable(
                name="count",
                concrete_type="Reg#(Bit#(2))",
                bsv_mapping="Reg",
                abstract_variable="count",
            )
        ],
        processes=[
            Process(
                name="Increment",
                description="Increments count by 1 each cycle, wrapping at 4",
                bsv_mapping="rule Increment",
            )
        ],
        preserved_invariants=["CountBound"],
        preserved_liveness=["Liveness"],
        backtracks_performed=0,
        ppa_estimate=PPAEstimate(area_gates=4.0),
        open_issues=[],
        error_log=[],
    )

    output_path = artifacts_dir / "02_pluscal_impl.json"
    output_path.write_text(impl.model_dump_json(indent=2))
    print(f"[Stage 2] {output_path}")
    return state
