"""
Spec-derived golden-vector cross-check (Stage 4 pre-flight).

Agent 1 hand-computes the cocotb golden vectors from the NL prompt; on deep
sequential designs its arithmetic is fragile (the live FIFO run failed a CORRECT
RTL because one of 19 golden vectors miscounted occupancy — a "false red"). This
module removes that failure class: it reconstructs the REFINED engine spec (the
executable model Agent 3 authored), simulates it on Agent 1's INPUT stimulus
(`pipeline.cocotb.spec_sim`) to derive arithmetically-correct expected outputs,
regenerates the testbench against those, and records any disagreement with Agent
1's expecteds in `02_vector_check.json`.

Design (chosen by the project owner):
  * cocotb runs against the SPEC-DERIVED expecteds — so a correct RTL is never
    failed by a wrong Agent-1 vector (no false red), and the run cross-validates
    Compiler 2 against an INDEPENDENT interpreter of the same refined spec.
  * Every Agent-1 vs spec-sim disagreement is SURFACED in 02_vector_check.json,
    so a genuine spec/intent bug (or an Agent-1 error) is recorded for review
    rather than silently masked. Full agreement is the strong case (RTL confirmed
    against two independent sources).

Fail-soft: ANY error here (spec not reconstructable, an expression the simulator
can't evaluate, a missing artifact) returns None and the caller falls back to
Agent 1's original testbench — this pre-flight must never break a Stage-4 run.
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.schemas.summary_schema import SpecSummary
from pipeline.schemas.tla_schema import FormalSpec
from pipeline.refinement.bridge import formal_spec_to_engine_spec
from pipeline.refinement.engine import _replay_chain
from pipeline.cocotb.generator import generate_testbench
from pipeline.cocotb.spec_sim import derive_expected


def _reconstruct_refined_spec(artifact_dir: Path) -> dict | None:
    """Rebuild the refined engine spec from disk via the engine's replay invariant.

    The committed refinement_chain.json replayed from the FormalSpec's engine spec
    reproduces exactly the spec Compiler 2 used. Returns None if the refinement did
    not run (empty/missing chain) or anything is unreadable.
    """
    spec_data = json.loads((artifact_dir / "02_formal_spec.json").read_text())
    spec = FormalSpec.model_validate(spec_data)
    chain_path = artifact_dir / "refinement_chain.json"
    chain = json.loads(chain_path.read_text()) if chain_path.exists() else []
    if not chain:
        return None  # no refinement ran (e.g. the G07 partial fallback) -> skip
    return _replay_chain(formal_spec_to_engine_spec(spec), chain)


def _compare(agent1: list[dict], spec: list[dict], output_ports: list[str]) -> list[dict]:
    """Per-port disagreements between Agent-1 and spec-derived expecteds.

    Compares only the declared output ports. A port present on one side and absent
    on the other (e.g. spec yields X -> omitted, or Agent 1 asserts an output the
    spec never drives) is itself a disagreement.
    """
    out: list[dict] = []
    for i, (a, s) in enumerate(zip(agent1, spec)):
        for port in output_ports:
            av, sv = a.get(port), s.get(port)
            present_a, present_s = port in a, port in s
            if av != sv or present_a != present_s:
                out.append({
                    "vector": i,
                    "port": port,
                    "agent1": av if present_a else "<unasserted>",
                    "spec": sv if present_s else "<X/undriven>",
                })
    return out


def apply_spec_derived_vectors(artifact_dir: Path) -> dict | None:
    """Build a spec-corrected testbench + a vector-check report (best-effort).

    Returns {"testbench_path": Path, "report": dict, "agreed": bool} on success,
    or None on any failure (caller runs Agent 1's original testbench instead).
    Writes 02_testbench_specvec.py (the corrected bench) and 02_vector_check.json.
    """
    try:
        summary = SpecSummary.model_validate(
            json.loads((artifact_dir / "01_summary.json").read_text())
        )
        refined = _reconstruct_refined_spec(artifact_dir)
        if refined is None:
            return None

        stimulus = [tv.inputs for tv in summary.test_vectors]
        output_ports = [p.name for p in summary.ports if p.direction == "output"]
        if not stimulus or not output_ports:
            return None

        spec_expected = derive_expected(
            refined, stimulus, output_ports,
            reset_port=summary.reset_port or "reset",
            reset_active_low=bool(summary.reset_active_low),
        )

        # Degenerate-reference guard. If the interpreter could not produce a
        # concrete value for some declared output across ANY vector — an Agent-3
        # modelling gap (a port the spec never drives) or an expression form the
        # interpreter cannot evaluate (everything degrades to X) — that port would
        # get ZERO cocotb assertions and any RTL would pass it silently. Refuse the
        # spec-derived reference and fall back to Agent 1's testbench unless EVERY
        # output port is asserted by it at least once.
        asserted = set().union(*(set(r.keys()) for r in spec_expected)) if spec_expected else set()
        if set(output_ports) - asserted:
            return None

        agent1_expected = [dict(tv.expected) for tv in summary.test_vectors]
        disagreements = _compare(agent1_expected, spec_expected, output_ports)

        # Corrected summary: same inputs, spec-derived expecteds. Round-trip
        # through model_validate so test_vectors are TestVector objects (a plain
        # model_copy would leave them as dicts, which the generator can't read).
        corrected_data = summary.model_dump()
        corrected_data["test_vectors"] = [
            {"inputs": tv.inputs, "expected": spec_expected[i]}
            for i, tv in enumerate(summary.test_vectors)
        ]
        corrected = SpecSummary.model_validate(corrected_data)

        tb_path = artifact_dir / "02_testbench_specvec.py"
        generate_testbench(corrected, tb_path)

        report = {
            "status": "success",
            "agreed": not disagreements,
            "num_vectors": len(stimulus),
            "num_disagreements": len(disagreements),
            "disagreements": disagreements,
            "note": (
                "cocotb runs against SPEC-DERIVED expecteds (an independent "
                "interpreter of the refined spec), so a correct RTL is never failed "
                "by a wrong Agent-1 golden vector. Each disagreement below is an "
                "Agent-1 vector that differs from the spec — either an Agent-1 "
                "arithmetic error (false red avoided) or a spec/intent bug — and is "
                "surfaced for review, not silently masked."
            ),
        }
        (artifact_dir / "02_vector_check.json").write_text(json.dumps(report, indent=2))
        return {"testbench_path": tb_path, "report": report, "agreed": not disagreements}
    except Exception:
        # Pre-flight must never break Stage 4: fall back to Agent 1's testbench.
        return None
