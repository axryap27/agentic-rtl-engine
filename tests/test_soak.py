"""
Tests for the mass spec-vs-RTL soak (pipeline/cocotb/soak.py + Stage 4 wiring).

What's pinned:
  * deterministic, in-width, reset-free stimulus generation;
  * fail-soft skips (disabled / no chain) that never break Stage 4;
  * a CORRECT fixture RTL passes a real cocotb soak (and writes 04_soak.json);
  * NON-VACUITY: a corrupted RTL (off-by-one datapath) is CAUGHT by the soak
    with the divergence recorded — the soak is a real net, not a rubber stamp;
  * Stage 4 integration: a soak failure is recorded on 04_evaluation but does
    NOT flip status (deliberate: a spec-vs-RTL divergence is a deterministic
    pipeline bug; a metered Agent-3 revision retry cannot fix it).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pipeline.cocotb.soak import generate_soak_stimulus, run_soak, soak_cycles
from pipeline.schemas.summary_schema import SpecSummary
from tests.fixtures.medium_designs import MEDIUM_DESIGNS
from tests.test_spec_sim import _seed

_HAVE_COCOTB = (
    shutil.which("cocotb-config") is not None
    and shutil.which("iverilog") is not None
)


# ---------------------------------------------------------------------------
# Stimulus generation
# ---------------------------------------------------------------------------

def _summary(name: str) -> SpecSummary:
    return MEDIUM_DESIGNS[name]["summary"]()


def test_cold_import_has_no_cycle():
    """`import pipeline.cocotb.soak` in a FRESH interpreter must not trip the
    spec_sim <-> pipeline.refinement package-init cycle (obligations binds the
    evaluator lazily for exactly this reason)."""
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "-c", "import pipeline.cocotb.soak"],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert r.returncode == 0, r.stderr


def test_stimulus_is_deterministic_per_seed():
    s = _summary("fifo")
    a = generate_soak_stimulus(s, 100, seed=42)
    b = generate_soak_stimulus(s, 100, seed=42)
    c = generate_soak_stimulus(s, 100, seed=43)
    assert a == b
    assert a != c


def test_stimulus_in_width_and_reset_free():
    s = _summary("register_file")  # active-low rst_n + multi-bit inputs
    widths = {p.name: int(p.width or 1) for p in s.ports if p.direction == "input"}
    reset_port = s.reset_port or "reset"
    stim = generate_soak_stimulus(s, 200, seed=7)
    assert len(stim) == 200
    for vec in stim:
        assert reset_port not in vec and "clk" not in vec
        for name, val in vec.items():
            assert 0 <= val < (1 << widths[name]), (name, val)
        # dense: every free input driven every cycle
        assert set(vec) == {n for n in widths if n not in ("clk", reset_port)}


def test_soak_cycles_env(monkeypatch):
    monkeypatch.setenv("RTL_SOAK_CYCLES", "123")
    assert soak_cycles() == 123
    monkeypatch.setenv("RTL_SOAK_CYCLES", "0")
    assert soak_cycles() == 0
    monkeypatch.setenv("RTL_SOAK_CYCLES", "junk")
    assert soak_cycles() == 2000  # malformed -> default


# ---------------------------------------------------------------------------
# Fail-soft skips
# ---------------------------------------------------------------------------

def test_soak_disabled_is_a_skip(tmp_path):
    report = run_soak(tmp_path, tmp_path / "missing.v", "m", n_cycles=0)
    assert report["status"] == "skipped"
    assert "disabled" in report["reason"]
    assert json.loads((tmp_path / "04_soak.json").read_text())["status"] == "skipped"


def test_soak_without_artifacts_is_a_skip_not_a_crash(tmp_path):
    report = run_soak(tmp_path, tmp_path / "missing.v", "m", n_cycles=50)
    assert report["status"] == "skipped"
    assert report["reason"]  # carries the failure detail


def test_soak_without_chain_is_a_skip(tmp_path):
    ad = _seed(tmp_path, "fifo", write_rtl=False)
    (ad / "refinement_chain.json").write_text("[]")  # refinement never ran
    report = run_soak(ad, ad / "output.v", "fifo", n_cycles=50)
    assert report["status"] == "skipped"
    assert "chain" in report["reason"]


# ---------------------------------------------------------------------------
# Real cocotb soak: pass on correct RTL, CATCH a corrupted one (non-vacuity)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAVE_COCOTB, reason="iverilog + cocotb-config required")
def test_soak_passes_correct_rtl(tmp_path):
    ad = _seed(tmp_path, "accumulator", write_rtl=True)
    summary = _summary("accumulator")
    report = run_soak(ad, ad / "output.v", summary.module_name, n_cycles=200)
    assert report["status"] == "success", report
    assert report["cycles"] == 200
    assert "seed" in report
    on_disk = json.loads((ad / "04_soak.json").read_text())
    assert on_disk["status"] == "success"
    assert (ad / "04_soak_testbench.py").exists()


@pytest.mark.skipif(not _HAVE_COCOTB, reason="iverilog + cocotb-config required")
def test_soak_catches_corrupted_rtl(tmp_path):
    """Non-vacuity: an off-by-one in the accumulator datapath — invisible to a
    bench with no vectors but caught within a 200-cycle random soak on the
    first enabled add."""
    ad = _seed(tmp_path, "accumulator", write_rtl=True)
    summary = _summary("accumulator")
    v = (ad / "output.v").read_text()
    assert "acc + din" in v, "codegen changed; update the mutation target"
    (ad / "output.v").write_text(v.replace("acc + din", "acc + din + 1", 1))

    report = run_soak(ad, ad / "output.v", summary.module_name, n_cycles=200)
    assert report["status"] == "failed", report
    assert report["num_failed_vectors"] > 0
    assert report["first_divergences"], "first failing vectors must be recorded"
    assert report["seed"] is not None  # replayable
    assert json.loads((ad / "04_soak.json").read_text())["status"] == "failed"


# ---------------------------------------------------------------------------
# Stage 4 wiring
# ---------------------------------------------------------------------------

def _seed_stage4(tmp_path, name: str, run_id: str) -> Path:
    """Full Stage-4 artifact layout at artifacts/<run_id> (conftest cwd-isolated)."""
    from pipeline.cocotb.generator import generate_testbench

    ad = Path("artifacts") / run_id
    ad.mkdir(parents=True)
    seeded = _seed(tmp_path, name, write_rtl=True)
    for f in seeded.iterdir():
        (ad / f.name).write_text(f.read_text())
    summary = _summary(name)
    tb = ad / "02_testbench.py"
    generate_testbench(summary, tb)
    (ad / "02_testbench_meta.json").write_text(
        json.dumps({"status": "success", "testbench_path": str(tb)})
    )
    (ad / "03_rtl_output.json").write_text(json.dumps({
        "status": "success",
        "verilog_path": str(ad / "output.v"),
        "module_name": summary.module_name,
    }))
    return ad


@pytest.mark.skipif(not _HAVE_COCOTB, reason="iverilog + cocotb-config required")
def test_stage4_records_soak_success(tmp_path, monkeypatch):
    from pipeline.nodes.stage4 import run_stage4

    ad = _seed_stage4(tmp_path, "accumulator", "soak_ok")
    monkeypatch.setenv("RTL_SOAK_CYCLES", "150")
    run_stage4({"run_id": "soak_ok", "retry_counts": {}, "halt": False})

    ev = json.loads((ad / "04_evaluation.json").read_text())
    assert ev["status"] == "success"
    assert ev["soak"]["status"] == "success"
    assert ev["soak"]["cycles"] == 150


@pytest.mark.skipif(not _HAVE_COCOTB, reason="iverilog + cocotb-config required")
def test_stage4_soak_failure_is_loud_but_does_not_flip_status(tmp_path, monkeypatch):
    """The deliberate routing decision: the directed bench PASSED, so a soak
    divergence (a deterministic codegen/composition bug) is surfaced on
    04_evaluation + 04_soak.json instead of flipping status into a metered
    revision retry that cannot fix it."""
    from pipeline.nodes.stage4 import run_stage4

    ad = _seed_stage4(tmp_path, "accumulator", "soak_bad")
    # Corrupt a BUSY-path behavior the 5 directed vectors never exercise: the
    # directed bench still passes, only the soak can catch it. The accumulator
    # directed vectors never reach acc >= 128, so gate the adder bug there.
    v = (ad / "output.v").read_text()
    assert "acc + din" in v
    (ad / "output.v").write_text(
        v.replace("acc + din", "(acc < 128) ? (acc + din) : (acc + din + 1)", 1)
    )
    monkeypatch.setenv("RTL_SOAK_CYCLES", "300")
    run_stage4({"run_id": "soak_bad", "retry_counts": {}, "halt": False})

    ev = json.loads((ad / "04_evaluation.json").read_text())
    assert ev["status"] == "success", "directed bench passed; status must hold"
    assert ev["soak"]["status"] == "failed"
    assert ev["soak"]["num_failed_vectors"] > 0
    assert ev["soak"]["first_divergences"]
    full = json.loads((ad / "04_soak.json").read_text())
    assert full["status"] == "failed" and full["raw_tail"]


def test_stage4_soak_disabled_keeps_envelope_clean(tmp_path, monkeypatch):
    """With the soak disabled (the suite default), 04_evaluation carries no
    soak block and Stage 4 behaves exactly as before."""
    if not _HAVE_COCOTB:
        pytest.skip("iverilog + cocotb-config required")
    from pipeline.nodes.stage4 import run_stage4

    ad = _seed_stage4(tmp_path, "accumulator", "soak_off")
    # conftest already sets RTL_SOAK_CYCLES=0
    run_stage4({"run_id": "soak_off", "retry_counts": {}, "halt": False})
    ev = json.loads((ad / "04_evaluation.json").read_text())
    assert ev["status"] == "success"
    assert "soak" not in ev
    assert json.loads((ad / "04_soak.json").read_text())["status"] == "skipped"
