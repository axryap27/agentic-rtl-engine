"""
Regression for the first live `main.py` run (2-bit counter, run 64b59441443e).

That run produced a `partial` artifact (degenerate empty module) because the
LIVE refinement loop hit two distinct faults that the deterministic stub picker
had never exercised:

  Bug A — Iteration was non-idempotent: it wrapped an action's guard in another
          paren layer on EVERY apply, so a picker that re-picked the same action
          kept changing the spec and the pass cycled to max_steps.

  Bug B — one bad pick_rule response killed the whole run. On the handshake pass,
          Agent 3 (correctly) judged "a counter has no handshake interface" and
          returned a verbose non-pick "blocked" report; that overflowed the
          512-token cap, truncated to invalid JSON, and pick_rule RAISED — which
          propagated out of the engine and aborted refinement -> `partial`.

The fixes (all pure / offline-verifiable):
  * A1  Iteration.apply is idempotent (no-op on an already-clocked action).
  * A2  the engine treats a no-op application (post_hash == pre_hash) as a
        non-advancing pick: exclude + strike, never commit-and-spin.
  * B-fix-1  a pick_rule EXCEPTION is caught in the engine and handled exactly
        like an invalid pick (strike -> backtrack), so a single bad Agent-3
        response degrades to a skipped pass instead of a dead pipeline.

The captured Agent-3 spec itself was CLEAN (symbolic comparisons, clk/rst not
modelled as state variables) — the hardened prompt worked. So these tests pin
the *engine/rule* robustness that lets a competent-but-imperfect live picker
drive that clean spec all the way to a synthesizable counter.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pipeline.refinement.engine import (
    run as engine_run,
    is_rtl_style,
    RefinementStall,
    _replay_chain,
)
from pipeline.refinement.bridge import formal_spec_to_engine_spec
from pipeline.refinement.rules.iteration import Iteration
from pipeline.schemas.tla_schema import FormalSpec


# --- The exact artifacts captured from live run 64b59441443e -----------------

CAPTURED_SPEC = {
    "module_name": "sync_up_counter_2bit",
    "description": "A synchronous 2-bit binary up-counter.",
    "variables": {"count": {"type": "Nat", "width": 2}},
    "initial": {"count": "0"},
    "transitions": [
        {"label": "Reset", "condition": "rst = 1", "updates": {"count": "0"}},
        {"label": "Wrap", "condition": "rst = 0 AND count = 3",
         "updates": {"count": "0"}},
        {"label": "Increment", "condition": "rst = 0 AND count < 3",
         "updates": {"count": "count + 1"}},
    ],
    "invariants": ["count >= 0 AND count <= 3"],
    "raw_tla": None,
}

CAPTURED_SUMMARY = {
    "module_name": "sync_up_counter_2bit",
    "description": "A synchronous 2-bit binary up-counter.",
    "ports": [
        {"name": "clk", "direction": "input", "width": 1},
        {"name": "rst", "direction": "input", "width": 1},
        {"name": "count", "direction": "output", "width": 2},
    ],
    "test_vectors": [
        {"inputs": {"clk": 1, "rst": 1}, "expected": {"count": 0}},
        {"inputs": {"clk": 1, "rst": 0}, "expected": {"count": 1}},
        {"inputs": {"clk": 1, "rst": 0}, "expected": {"count": 2}},
        {"inputs": {"clk": 1, "rst": 0}, "expected": {"count": 3}},
        {"inputs": {"clk": 1, "rst": 0}, "expected": {"count": 0}},
        {"inputs": {"clk": 1, "rst": 1}, "expected": {"count": 0}},
    ],
    "reset_port": "rst",
    "reset_active_low": False,
    "status": "success",
}


def _engine_spec():
    return formal_spec_to_engine_spec(FormalSpec.model_validate(CAPTURED_SPEC))


# ---------------------------------------------------------------------------
# A1 — Iteration idempotency
# ---------------------------------------------------------------------------

def test_iteration_is_idempotent():
    """Applying Iteration twice to the same action == applying it once.

    Before the fix, the second apply added a paren layer to the guard, so the
    spec kept changing and the engine cycled.
    """
    spec = _engine_spec()
    rule = Iteration()
    once = rule.apply(spec, {"action_name": "Increment"})
    twice = rule.apply(once, {"action_name": "Increment"})
    assert twice == once, "Iteration.apply must be idempotent on a clocked action"
    # And the action really did become clocked on the first apply.
    inc = next(a for a in once["actions"] if a["name"] == "Increment")
    assert inc["clocked"] is True


# ---------------------------------------------------------------------------
# B-fix-1 — a throwing picker must NOT escape the engine as a raw crash
# ---------------------------------------------------------------------------

def test_engine_survives_a_throwing_pick_rule():
    """A pick_rule that always raises (the handshake 'blocked' report shape)
    must surface as a controlled RefinementStall, never the picker's own
    exception. The structured-pass wrapper in stage3 catches RefinementStall
    and continues to the next pass — so one bad response can't kill the run.
    """
    # Isolation: clear any chain left by a prior (possibly interrupted) run.
    # The engine concatenates a same-run_id committed prefix onto the on-disk
    # chain (G13), so a stale file at this fixed path would skew the assertion.
    shutil.rmtree(Path("artifacts/test_repro_throwing_picker"), ignore_errors=True)

    def always_throws(applicable_rules, spec):
        raise ValueError("simulated: Agent 3 returned a non-pick 'blocked' report")

    with pytest.raises(RefinementStall):
        engine_run(
            formal_spec=_engine_spec(),
            pick_rule=always_throws,
            run_id="test_repro_throwing_picker",
            max_steps=8,
        )


# ---------------------------------------------------------------------------
# A2 — a no-op picker must strike/stall fast, not spin to max_steps
# ---------------------------------------------------------------------------

def test_engine_does_not_spin_on_a_no_op_picker():
    """A picker that keeps choosing Iteration on the SAME (already-clocked after
    one apply) action used to spin to max_steps. With A1 (idempotent apply) +
    A2 (no-op = strike), the engine excludes the no-op and backtracks; the chain
    never reaches the cap with identical steps.
    """
    # Isolation: clear any chain left by a prior (possibly interrupted) run so the
    # committed-prefix concatenation (G13) can't inflate the chain-length assertion
    # below with stale steps. Without this, a leftover 4-step file makes this test
    # spuriously fail on the next full-suite run.
    shutil.rmtree(Path("artifacts/test_repro_noop_picker"), ignore_errors=True)

    def always_iterate_increment(applicable_rules, spec):
        return {"rule_name": "Iteration", "params": {"action_name": "Increment"}}

    with pytest.raises(RefinementStall) as exc:
        engine_run(
            formal_spec=_engine_spec(),
            pick_rule=always_iterate_increment,
            run_id="test_repro_noop_picker",
            max_steps=50,
        )
    # The committed chain must be tiny (one real Iteration, then strikes), NOT a
    # cycle of dozens of identical commits.
    chain_path = Path("artifacts/test_repro_noop_picker/refinement_chain.json")
    if chain_path.exists():
        chain = json.loads(chain_path.read_text())
        assert len(chain) <= 3, (
            f"no-op picker should not accumulate a long chain; got {len(chain)}"
        )


# ---------------------------------------------------------------------------
# Integration — the full stage3 path converges on the captured counter
# ---------------------------------------------------------------------------

def _competent_picker(applicable_rules, spec, *, system_prompt=None):
    """An applicability-driven picker that mimics a competent LIVE Agent 3.

    It makes the two constructive picks a counter needs (establish reset, then
    clock each counting action) and otherwise DECLINES by raising — historically
    the way live Agent 3 declined the handshake/mapping passes.

    NOTE: the structured passes are now gated off (stage3._RUN_STRUCTURED_PASSES),
    so under the sole catch-all driver this picker never hits the decline branch
    (the catch-all only ever offers constructive rules for the counter). The
    decline path's engine-robustness coverage now lives in
    test_engine_survives_a_throwing_pick_rule (direct engine test). This picker is
    kept applicability-driven (not a monotonic counter) because the catch-all may
    still call it more than the minimal number of times.
    """
    names = {r["name"] for r in applicable_rules}
    reset_action = spec.get("reset_action")

    if "Initialization" in names:
        needs_reset = reset_action is None or any(
            v.get("reset_value") is None for v in spec.get("variables", [])
        )
        if needs_reset:
            return {
                "rule_name": "Initialization",
                "params": {
                    "reset_values": {v["name"]: "0" for v in spec.get("variables", [])},
                    "reset_action_name": "Reset",
                },
            }

    if "Iteration" in names:
        for a in spec.get("actions", []):
            if a["name"] != (reset_action or "Reset") and not a.get("clocked", False):
                return {"rule_name": "Iteration", "params": {"action_name": a["name"]}}

    # Nothing constructive for this pass/design (handshake/mapping on a counter):
    # mimic Agent 3 declining with a non-pick 'blocked' report.
    raise ValueError("simulated Agent-3 decline: no constructive rule for this pass")


def test_full_stage3_converges_on_captured_counter(tmp_path, monkeypatch):
    """NL→spec is fixed to the captured CLEAN spec; the competent picker drives
    the real stage3 (catch-all sole driver). With the engine/rule fixes the run
    must reach `success` with a correct 2-bit counter — the exact scenario that
    produced a `partial` empty module before the fixes.
    """
    from pipeline.nodes import stage3
    from pipeline.agents import agent3

    run_id = "repro_counter"
    art = tmp_path / "artifacts" / run_id
    art.mkdir(parents=True, exist_ok=True)
    (art / "01_summary.json").write_text(json.dumps(CAPTURED_SUMMARY))
    monkeypatch.chdir(tmp_path)

    spec = FormalSpec.model_validate(CAPTURED_SPEC)
    monkeypatch.setattr(agent3, "generate_formal_spec", lambda summary: spec)
    monkeypatch.setattr(agent3, "pick_rule", _competent_picker)
    # Critic gate accepts (it is the single mockable boundary — see stage3 doc).
    monkeypatch.setattr(
        stage3, "_run_refinement_critic",
        lambda abstract, refined: {"verdict": "accept", "issues": [], "reasoning": ""},
    )

    state = {"run_id": run_id, "retry_counts": {}, "halt": False, "last_diagnosis": None}
    stage3.run_stage3(state)

    rtl = json.loads((art / "03_rtl_output.json").read_text())
    assert rtl["status"] == "success", (
        f"stage3 must converge to success, got {rtl.get('status')}: "
        f"{rtl.get('error', '')[:300]}"
    )

    verilog = (art / "output.v").read_text()
    # Correct counter shape, Verilog-2001 only.
    assert "output reg [1:0] count" in verilog, verilog
    assert "input  rst" in verilog or "input rst" in verilog, verilog
    assert "always @(posedge clk)" in verilog, verilog
    for banned in ("logic ", "always_ff", "always_comb"):
        assert banned not in verilog, f"SystemVerilog token {banned!r} leaked"


def test_full_stage3_counter_passes_cocotb(tmp_path, monkeypatch):
    """End-to-end closure: the Verilog stage3 produces from the captured spec
    passes the deterministically-generated cocotb testbench (0->1->2->3->0 with
    synchronous reset via dut.rst). Skipped when the sim toolchain is absent.
    """
    pytest.importorskip("cocotb", reason="cocotb not installed")
    if shutil.which("iverilog") is None or shutil.which("vvp") is None:
        pytest.skip("iverilog/vvp not installed")

    from pipeline.nodes import stage3
    from pipeline.agents import agent3
    from pipeline.cocotb.generator import generate_testbench
    from pipeline.cocotb.runner import run_testbench
    from pipeline.schemas.summary_schema import SpecSummary

    run_id = "repro_counter_cocotb"
    art = tmp_path / "artifacts" / run_id
    art.mkdir(parents=True, exist_ok=True)
    (art / "01_summary.json").write_text(json.dumps(CAPTURED_SUMMARY))
    monkeypatch.chdir(tmp_path)

    spec = FormalSpec.model_validate(CAPTURED_SPEC)
    monkeypatch.setattr(agent3, "generate_formal_spec", lambda summary: spec)
    monkeypatch.setattr(agent3, "pick_rule", _competent_picker)
    monkeypatch.setattr(
        stage3, "_run_refinement_critic",
        lambda abstract, refined: {"verdict": "accept", "issues": [], "reasoning": ""},
    )

    state = {"run_id": run_id, "retry_counts": {}, "halt": False, "last_diagnosis": None}
    stage3.run_stage3(state)
    rtl = json.loads((art / "03_rtl_output.json").read_text())
    assert rtl["status"] == "success", rtl.get("error", "")[:300]

    # Generate the testbench deterministically from the summary and simulate.
    summary = SpecSummary.model_validate(CAPTURED_SUMMARY)
    tb_path = art / "02_testbench.py"
    generate_testbench(summary, tb_path)

    result = run_testbench(tb_path, art / "output.v", spec.module_name)
    assert result["status"] == "pass", json.dumps(result, indent=2)[:1500]
