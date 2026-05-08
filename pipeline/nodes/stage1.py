import json
from pathlib import Path

from pipeline.schemas import (
    FormalSpec, FormalStateVariable, Invariant, LivenessProperty, NFCConstraints, NLSpec,
)
from pipeline.state import PipelineState

_TLA_CONTENT = """\
---- MODULE Counter ----
EXTENDS Naturals

VARIABLES count

Init == count = 0

Next == count' = (count + 1) % 4

Spec == Init /\\ [][Next]_count /\\ WF_count(Next)

CountBound == count <= 3

Liveness == <>(count = 3)
====
"""

_CFG_CONTENT = """\
INIT Init
NEXT Next
INVARIANT CountBound
PROPERTY Liveness
"""


def stage1_node(state: PipelineState) -> PipelineState:
    run_id = state["run_id"]
    artifacts_dir = Path("artifacts") / run_id

    with open(artifacts_dir / "00_nl_spec.json") as f:
        NLSpec.model_validate(json.load(f))

    tla_dir = artifacts_dir / "tla"
    tla_dir.mkdir(exist_ok=True)

    tla_path = tla_dir / "Counter.tla"
    cfg_path = tla_dir / "Counter.cfg"
    tla_path.write_text(_TLA_CONTENT)
    cfg_path.write_text(_CFG_CONTENT)

    spec = FormalSpec(
        run_id=run_id,
        status="success",
        design_name="counter_2bit",
        tla_module_name="Counter",
        tla_spec_path=str(tla_path),
        tla_cfg_path=str(cfg_path),
        tlc_verified=False,
        tla_syntax_valid=True,
        state_variables=[
            FormalStateVariable(
                name="count",
                type="Nat",
                domain="0..3",
                hardware_mapping="register",
            )
        ],
        invariants=[
            Invariant(
                name="CountBound",
                formula="count <= 3",
                property_class="safety",
            )
        ],
        liveness_properties=[
            LivenessProperty(
                name="Liveness",
                formula="<>(count = 3)",
                property_class="progress",
            )
        ],
        nfc_constraints=NFCConstraints(),
        abstractions_applied=["modular_arithmetic_wrapped_at_4"],
        open_ambiguities=[],
        error_log=[],
        notes="Stub: hardcoded 2-bit counter",
    )

    output_path = artifacts_dir / "01_formal_spec.json"
    output_path.write_text(spec.model_dump_json(indent=2))
    print(f"[Stage 1] {output_path}")
    return state
