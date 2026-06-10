"""
Mass spec-vs-RTL soak — Stage 4 post-pass cross-check.

WHY THIS EXISTS
---------------
The directed bench checks ~20 Agent-1 vectors. The refined spec is an
EXECUTABLE model and (since core/) simulating it is nearly free (~0.8M edges/s
natively), so after the directed bench passes we can afford a much stronger
check: drive THOUSANDS of deterministic random cycles, derive the expected
outputs from the spec, and run the same RTL against them in cocotb. A
divergence here is a genuine spec-vs-RTL bug the directed vectors missed —
Compiler-2 codegen, composition, or simulator-semantics drift — caught at run
time instead of shipping silently.

DESIGN DECISIONS (deliberate)
-----------------------------
* The soak runs ONLY after the directed bench passes — soaking a failing RTL
  is noise on top of a known failure.
* A soak FAILURE does NOT flip 04_evaluation's status: the design met its
  directed acceptance bench, and a spec-vs-RTL divergence is a DETERMINISTIC
  pipeline bug that an Agent-3 revision retry would burn metered credits
  without fixing. It is surfaced exactly like an Agent-1/spec disagreement —
  recorded on the artifact (`soak`), full detail in 04_soak.json, loud banner
  in main.py. (Routing soak failures to the diagnoser is the planned upgrade.)
* The stimulus is DETERMINISTIC per run: seeded by crc32 of the artifact dir
  name, so a soak is exactly replayable from the artifacts alone.
* Stimulus values are IN-WIDTH random ints for every free input. The reset
  port is never driven in-vector (the bench's reset pulse handles reset) and
  no X/string values are used — every vector asserts, densely.
* Fail-soft like vector_check: any infrastructure problem (missing artifacts,
  no replayable chain, runner unavailable) yields status "skipped" with a
  reason — the soak must never break a Stage-4 run. A cocotb FAILURE is a
  RESULT ("failed"), never swallowed.

Config: RTL_SOAK_CYCLES env var (default 2000; "0" disables). The test suite
disables it globally in tests/conftest.py — dedicated soak tests pass
n_cycles explicitly.
"""

from __future__ import annotations

import json
import os
import random
import time
import zlib
from pathlib import Path

from pipeline.schemas.summary_schema import SpecSummary
from pipeline.cocotb.generator import generate_testbench
from pipeline.cocotb.spec_sim import derive_expected
from pipeline.cocotb.vector_check import _reconstruct_refined_spec

DEFAULT_CYCLES = 2000


def soak_cycles() -> int:
    """The configured soak length (RTL_SOAK_CYCLES env, default 2000, 0=off)."""
    raw = os.environ.get("RTL_SOAK_CYCLES", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            return DEFAULT_CYCLES
    return DEFAULT_CYCLES


def generate_soak_stimulus(summary: SpecSummary, n_cycles: int,
                           seed: int) -> list[dict]:
    """Deterministic in-width random stimulus for every free input.

    Excludes clk and the reset port (the generated bench's reset pulse handles
    reset; vectors never re-drive it — the same shape every fixture uses).
    Values stay within each port's declared width: the spec simulator does NOT
    mask inputs while the RTL port would truncate them, so an over-width value
    would be a false divergence, not a finding.
    """
    rng = random.Random(seed)
    reset_port = summary.reset_port or "reset"
    in_ports = [
        p for p in summary.ports
        if p.direction == "input" and p.name not in ("clk", reset_port)
    ]
    return [
        {p.name: rng.randrange(0, 1 << int(p.width or 1)) for p in in_ports}
        for _ in range(n_cycles)
    ]


def run_soak(artifact_dir: Path, verilog_path: Path, module_name: str,
             n_cycles: int | None = None) -> dict:
    """Soak the RTL against the spec on deterministic random stimulus.

    Returns the report dict (always) and best-effort writes it to
    04_soak.json: status "success" | "failed" | "skipped" (+reason). The
    caller decides what to surface; this function never raises.
    """
    t0 = time.perf_counter()
    report: dict = {"status": "skipped", "reason": ""}
    try:
        cycles = soak_cycles() if n_cycles is None else max(0, int(n_cycles))
        if cycles <= 0:
            report["reason"] = "disabled (RTL_SOAK_CYCLES=0)"
            return _finish(artifact_dir, report, t0)

        try:
            from pipeline.cocotb.runner import run_testbench
        except Exception:
            report["reason"] = "cocotb runner unavailable"
            return _finish(artifact_dir, report, t0)

        summary = SpecSummary.model_validate(
            json.loads((artifact_dir / "01_summary.json").read_text())
        )
        refined = _reconstruct_refined_spec(artifact_dir)
        if refined is None:
            report["reason"] = "no replayable refinement chain"
            return _finish(artifact_dir, report, t0)
        output_ports = [p.name for p in summary.ports if p.direction == "output"]
        if not output_ports:
            report["reason"] = "no output ports"
            return _finish(artifact_dir, report, t0)

        # Deterministic, replayable-from-artifacts seed.
        seed = zlib.crc32(artifact_dir.name.encode("utf-8"))
        stimulus = generate_soak_stimulus(summary, cycles, seed)
        expected = derive_expected(
            refined, stimulus, output_ports,
            reset_port=summary.reset_port or "reset",
            reset_active_low=bool(summary.reset_active_low),
        )

        # Degenerate-reference guard (same rule as vector_check): every output
        # must be asserted at least once or the soak would silently prove
        # nothing about it.
        asserted = set().union(*(set(r) for r in expected)) if expected else set()
        if set(output_ports) - asserted:
            report["reason"] = (
                "output(s) never driven by the spec over the soak: "
                f"{sorted(set(output_ports) - asserted)}"
            )
            return _finish(artifact_dir, report, t0)

        soak_data = summary.model_dump()
        soak_data["test_vectors"] = [
            {"inputs": stimulus[i], "expected": expected[i]}
            for i in range(cycles)
        ]
        soak_summary = SpecSummary.model_validate(soak_data)
        tb_path = artifact_dir / "04_soak_testbench.py"
        generate_testbench(soak_summary, tb_path)

        result = run_testbench(tb_path, Path(verilog_path), module_name)
        if result.get("status") == "pass":
            report = {
                "status": "success",
                "cycles": cycles,
                "seed": seed,
                "note": (
                    "RTL matched the spec-derived expecteds on every random "
                    "cycle (X outputs are don't-asserts)."
                ),
            }
        else:
            failed = result.get("failed_vectors", []) or []
            report = {
                "status": "failed",
                "cycles": cycles,
                "seed": seed,
                "phase": result.get("phase", "unknown"),
                "error": result.get("error", "soak simulation failure"),
                "num_failed_vectors": len(failed),
                "first_divergences": failed[:5],
                "raw_tail": (result.get("raw", "") or "")[-1500:],
                "note": (
                    "The RTL diverged from the refined spec on random stimulus "
                    "the directed vectors missed — a genuine spec-vs-RTL bug "
                    "(Compiler-2 codegen, composition, or simulator semantics). "
                    "Deterministic: rerun with the recorded seed to reproduce."
                ),
            }
    except Exception as exc:  # fail-soft: the soak must never break Stage 4
        report = {"status": "skipped", "reason": f"soak error: {exc}"}
    return _finish(artifact_dir, report, t0)


def _finish(artifact_dir: Path, report: dict, t0: float) -> dict:
    report["duration_s"] = round(time.perf_counter() - t0, 3)
    try:
        (artifact_dir / "04_soak.json").write_text(json.dumps(report, indent=2))
    except Exception:
        pass  # report is still returned to the caller
    return report
