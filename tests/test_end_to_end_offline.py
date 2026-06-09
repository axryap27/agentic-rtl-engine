"""
End-to-end offline pipeline test (G01) on a MEDIUM-complexity design.

This is the suite's highest-risk test: it drives the FULL LangGraph
(`build_graph().invoke`) from a natural-language seed through to a terminal
artifact, on a real medium design (a traffic-light FSM + countdown-timer
datapath, and a multi-op ALU), with EVERY LLM boundary mocked so the run is
deterministic and free. No live LLM, no RUN_LIVE_LLM, no network.

WHAT IS MOCKED (and why each is a boundary, not a behavior)
-----------------------------------------------------------
  pipeline.agents.agent1.run                  -> returns the fixture SpecSummary
  pipeline.agents.agent3.generate_formal_spec -> returns the fixture FormalSpec
  pipeline.agents.agent3.pick_rule            -> APPLICABILITY-DRIVEN picker
  pipeline.agents.agent3.revise_on_tlc        -> returns the same good FormalSpec
  pipeline.agents.agent3.revise_on_cocotb     -> returns the same good FormalSpec
  pipeline.nodes.stage3._run_refinement_critic-> {"verdict":"accept"}
  pipeline.agents.agent_diagnoser.diagnose    -> {"failure_type":"spec",...}

The picker is APPLICABILITY-DRIVEN and IDEMPOTENT (pick the first sequence entry
whose rule is in the current applicable set), NOT a monotonic counter: stage3
runs ~6 engine passes that share one picker, and a counter would be exhausted by
the early passes, stall the catch-all pass with RefinementStall, and silently
divert the run into the G07 'partial' fallback — masking the test.

WHAT IT ASSERTS
---------------
  * The artifact chain through Stage 3 completes: 01_summary, 02_testbench_meta,
    02_formal_spec, 03_rtl_output all have status == 'success'.
  * output.v exists, is banlist-clean (no SystemVerilog / leaked TLA+ keywords),
    and lints clean under iverilog -Wall -t null (guarded by shutil.which).
  * The generated RTL is FUNCTIONALLY correct: prepending the `timescale the
    pipeline forgets to emit, the real cocotb runner PASSes the fixture's
    reset-offset test vectors (guarded by iverilog + cocotb-config presence).

DISCOVERIES (Wave 2) — NOW FIXED in Wave 3 (the tests below are positive)
------------------------------------------------------------------------
Two real medium-tier pipeline bugs surfaced in Wave 2 and were fixed in Wave 3:

  D1  Compiler 2 emitted no `timescale directive, so the graph's Stage 4 cocotb
      run could not represent cocotb's 10 ns clock. FIXED: Compiler 2 emits
      `timescale 1ns/1ps. (A companion runner fix absolutises run_testbench's
      paths so the graph's relative artifact paths no longer break the build.)
      -> test_graph_stage4_cocotb_passes

  D2  Compiler 2 sized every FREE INPUT as 1 bit, so the ALU's 2-bit `op`
      truncated and the wrong op was selected. FIXED: the bridge sizes free
      inputs from the SpecSummary port width / the register they feed.
      -> test_alu_freeinput_width_correct

A further limitation is documented (not relevant to the fixtures, which avoid it):
Compiler 2's IF-THEN-ELSE splitter does not recurse into the THEN branch, so a
conditional nested inside a THEN leaks untranslated IF/THEN/ELSE keywords. The
fixtures express multi-way logic as flat ELSE-IF chains to stay clear of it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pipeline.compilers.compiler2 import verify_banlist, BanlistViolation
from tests.fixtures.medium_designs import MEDIUM_DESIGNS


# ---------------------------------------------------------------------------
# Tool-availability guards (SKIP, never ERROR)
# ---------------------------------------------------------------------------

_HAVE_IVERILOG = shutil.which("iverilog") is not None
_HAVE_COCOTB = shutil.which("cocotb-config") is not None and _HAVE_IVERILOG


def _make_picker(sequence):
    """Build an applicability-driven, idempotent pick_rule from a (name,params) list.

    Picks the FIRST sequence entry whose rule name is in the current applicable
    set. Relies on each rule's is_applicable() going False after it fires, so the
    same picker is safe across the multiple engine passes stage3 runs. Accepts
    the `system_prompt` kwarg that stage3's pass-pick wrappers forward.
    """
    def picker(applicable_rules, spec, *, system_prompt=None):
        names = {r["name"] for r in applicable_rules}
        for rule_name, params in sequence:
            if rule_name in names:
                return {"rule_name": rule_name, "params": params}
        # Fallback: should not be reached for these fixtures once the design is
        # RTL-style; pick the first applicable rule with empty params.
        return {"rule_name": applicable_rules[0]["name"], "params": {}}
    return picker


def _install_offline_mocks(monkeypatch, design: dict) -> None:
    """Patch every LLM boundary so the graph runs deterministically and free."""
    import pipeline.agents.agent1 as agent1
    import pipeline.agents.agent3 as agent3
    import pipeline.agents.agent_diagnoser as agent_diagnoser
    import pipeline.nodes.stage3 as stage3

    summary = design["summary"]()
    formal = design["formal_spec"]()
    picker = _make_picker(design["picker_sequence"]())

    monkeypatch.setattr(agent1, "run", lambda prompt: summary)
    monkeypatch.setattr(agent3, "generate_formal_spec", lambda s: formal)
    monkeypatch.setattr(agent3, "pick_rule", picker)
    # Defensive: TLC is usually absent (loop skipped), but a real key must never
    # be touched if it ever does run.
    monkeypatch.setattr(agent3, "revise_on_tlc", lambda spec, errs: formal)
    monkeypatch.setattr(agent3, "revise_on_cocotb", lambda spec, log: formal)
    monkeypatch.setattr(
        stage3, "_run_refinement_critic",
        lambda abstract, concrete: {"verdict": "accept", "issues": [], "reasoning": "offline"},
    )
    monkeypatch.setattr(
        agent_diagnoser, "diagnose",
        lambda run_id: {"failure_type": "spec", "explanation": "offline-stub"},
    )


def _seed_and_invoke(tmp_path, monkeypatch, design_name: str, prompt: str) -> Path:
    """Seed 00_nl_spec.json in a tmp artifacts dir and invoke the full graph.

    All artifact paths in the pipeline are relative to cwd, so we chdir into a
    private tmp dir — this isolates the run and never touches the repo's
    artifacts/. Returns the artifact directory.
    """
    design = MEDIUM_DESIGNS[design_name]
    _install_offline_mocks(monkeypatch, design)

    monkeypatch.chdir(tmp_path)
    run_id = f"e2e_{design_name}"
    artifact_dir = Path("artifacts") / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "00_nl_spec.json").write_text(json.dumps({"prompt": prompt}))

    # Imported here (after chdir-independent) — build a FRESH graph so we never
    # reuse the module-level singleton across tests.
    from pipeline.graph import build_graph

    graph = build_graph()
    graph.invoke({"run_id": run_id, "retry_counts": {}, "halt": False})
    return artifact_dir


def _status(artifact_dir: Path, filename: str) -> str:
    p = artifact_dir / filename
    if not p.exists():
        return "<missing>"
    return json.loads(p.read_text()).get("status", "<no-status>")


def _run_real_cocotb(artifact_dir: Path, design_name: str, *, inject_timescale: bool):
    """Run the REAL cocotb runner on the pipeline's output.v.

    inject_timescale=True prepends the `timescale directive Compiler 2 omits, to
    isolate functional correctness from the missing-directive bug (D1). The
    testbench was already written by Stage 2 during the graph run.
    """
    from pipeline.cocotb.runner import run_testbench

    verilog_path = artifact_dir / "output.v"
    testbench_path = artifact_dir / "02_testbench.py"
    if inject_timescale:
        patched = artifact_dir / "output_ts.v"
        patched.write_text("`timescale 1ns/1ps\n" + verilog_path.read_text())
        verilog_path = patched
    return run_testbench(testbench_path, verilog_path, design_name)


# ===========================================================================
# Primary end-to-end test (G01) — traffic-light FSM (self-contained, green path)
# ===========================================================================

def test_end_to_end_offline_traffic_light(tmp_path, monkeypatch):
    """Full graph on the traffic-light medium FSM: chain completes through Stage 3.

    Asserts the artifact chain (01/02/03) is all 'success', output.v exists, is
    banlist-clean, and lints clean. The graph's own Stage 4 cocotb run cannot
    pass (D1, the missing-`timescale bug) so it is not asserted here; functional
    correctness is verified separately in
    test_end_to_end_offline_traffic_light_cocotb.
    """
    artifact_dir = _seed_and_invoke(
        tmp_path, monkeypatch, "traffic_light",
        "Design a traffic-light controller FSM with a countdown timer.",
    )

    assert _status(artifact_dir, "01_summary.json") == "success"
    assert _status(artifact_dir, "02_testbench_meta.json") == "success"
    assert _status(artifact_dir, "02_formal_spec.json") == "success"
    assert _status(artifact_dir, "03_rtl_output.json") == "success", (
        "Stage 3 did not produce success RTL; "
        f"03_rtl_output.json = {(artifact_dir / '03_rtl_output.json').read_text()[:500]}"
    )

    verilog_path = artifact_dir / "output.v"
    assert verilog_path.exists(), "Compiler 2 did not write output.v"
    verilog = verilog_path.read_text()

    # The RTL must be a real medium FSM: a clocked block over two registers.
    assert "always @(posedge clk)" in verilog
    assert "output reg [1:0] state" in verilog
    assert "output reg [1:0] timer" in verilog

    # Banlist gate (no SystemVerilog keywords, no leaked TLA+ keywords).
    try:
        verify_banlist(verilog)
    except BanlistViolation as exc:  # pragma: no cover - failure path
        pytest.fail(f"Generated traffic-light RTL violates the banlist: {exc}")


@pytest.mark.skipif(not _HAVE_IVERILOG, reason="iverilog not installed")
def test_end_to_end_offline_traffic_light_lints_clean(tmp_path, monkeypatch):
    """The graph-generated traffic-light Verilog lints clean under iverilog."""
    artifact_dir = _seed_and_invoke(
        tmp_path, monkeypatch, "traffic_light",
        "Design a traffic-light controller FSM with a countdown timer.",
    )
    verilog_path = artifact_dir / "output.v"
    assert verilog_path.exists()

    import subprocess
    result = subprocess.run(
        ["iverilog", "-Wall", "-t", "null", str(verilog_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        "Generated traffic-light RTL failed iverilog lint:\n"
        f"{result.stdout}\n{result.stderr}\n\n{verilog_path.read_text()}"
    )


@pytest.mark.skipif(not _HAVE_COCOTB, reason="iverilog + cocotb-config required")
def test_end_to_end_offline_traffic_light_cocotb(tmp_path, monkeypatch):
    """Functional verification: the graph's traffic-light RTL PASSes cocotb.

    Runs the REAL cocotb runner (the same one Stage 4 uses) on the pipeline's
    output.v and the Stage-2-generated testbench. D1 fix: Compiler 2 now emits
    `timescale 1ns/1ps so the simulator represents the 10 ns clock; the
    inject_timescale flag is now redundant (a second directive is harmless).
    Everything else — the RTL logic, the test vectors, the runner — is the
    pipeline's own output. A PASS here proves the medium FSM's next-state and
    timer datapath are functionally correct, not merely lint-clean.
    """
    artifact_dir = _seed_and_invoke(
        tmp_path, monkeypatch, "traffic_light",
        "Design a traffic-light controller FSM with a countdown timer.",
    )
    result = _run_real_cocotb(artifact_dir, "traffic_light", inject_timescale=True)
    assert result.get("status") == "pass", (
        "Traffic-light RTL failed cocotb (with `timescale injected):\n"
        f"phase={result.get('phase')} error={result.get('error')}\n"
        f"{result.get('raw', '')[-2000:]}"
    )


# ===========================================================================
# Second medium design — multi-op ALU (exercises the chain + a free-input bug)
# ===========================================================================

def test_end_to_end_offline_alu_chain_completes(tmp_path, monkeypatch):
    """Full graph on the multi-op ALU: chain completes through Stage 3, lint-clean.

    The ALU is a 4-way datapath mux + flag. Even though its generated RTL is
    functionally WRONG (D2: the 2-bit `op` is truncated to 1 bit), Compiler 2
    still emits lint-clean Verilog and the chain reaches 03_rtl_output success —
    which is exactly the silent-failure shape the goal must eventually catch.
    """
    artifact_dir = _seed_and_invoke(
        tmp_path, monkeypatch, "alu",
        "Design a multi-operation ALU (add/sub/and/or) with a zero flag.",
    )

    assert _status(artifact_dir, "01_summary.json") == "success"
    assert _status(artifact_dir, "02_testbench_meta.json") == "success"
    assert _status(artifact_dir, "02_formal_spec.json") == "success"
    assert _status(artifact_dir, "03_rtl_output.json") == "success"

    verilog = (artifact_dir / "output.v").read_text()
    assert "output reg [3:0] result" in verilog
    assert "output reg zero" in verilog
    try:
        verify_banlist(verilog)
    except BanlistViolation as exc:  # pragma: no cover
        pytest.fail(f"Generated ALU RTL violates the banlist: {exc}")


@pytest.mark.skipif(not _HAVE_IVERILOG, reason="iverilog not installed")
def test_end_to_end_offline_alu_lints_clean(tmp_path, monkeypatch):
    """The graph-generated ALU Verilog lints clean (despite being functionally wrong)."""
    artifact_dir = _seed_and_invoke(
        tmp_path, monkeypatch, "alu",
        "Design a multi-operation ALU (add/sub/and/or) with a zero flag.",
    )
    import subprocess
    result = subprocess.run(
        ["iverilog", "-Wall", "-t", "null", str(artifact_dir / "output.v")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"Generated ALU RTL failed iverilog lint:\n{result.stdout}\n{result.stderr}"
    )


# ===========================================================================
# DISCOVERIES — real pipeline bugs, captured as xfail (NOT patched in Wave 2)
# ===========================================================================

@pytest.mark.skipif(not _HAVE_COCOTB, reason="iverilog + cocotb-config required")
def test_graph_stage4_cocotb_passes(tmp_path, monkeypatch):
    """The graph's OWN Stage 4 (04_evaluation.json) reaches cocotb PASS.

    This is the literal G01 acceptance criterion ('Stage 4 runs to a cocotb
    PASS') — the full NL→RTL→cocotb-verified headline goal on a medium FSM.
    Unblocked by D1 (Compiler 2 emits `timescale) plus the runner path fix
    (run_testbench now absolutises its inputs, so the graph's relative
    artifact paths no longer double up and the .vvp builds).
    """
    artifact_dir = _seed_and_invoke(
        tmp_path, monkeypatch, "traffic_light",
        "Design a traffic-light controller FSM with a countdown timer.",
    )
    assert _status(artifact_dir, "04_evaluation.json") == "success", (
        "Stage 4 cocotb did not pass: "
        f"{(artifact_dir / '04_evaluation.json').read_text()[:600]}"
    )


@pytest.mark.skipif(not _HAVE_COCOTB, reason="iverilog + cocotb-config required")
def test_alu_freeinput_width_correct(tmp_path, monkeypatch):
    """The ALU RTL computes all four ops correctly under cocotb.

    D2 fix: the bridge sizes the 2-bit `op` free input from the Stage-1
    SpecSummary port width (instead of truncating it to 1 bit), so ops 2/3
    (AND/OR) are selected correctly and every cocotb vector passes.
    """
    artifact_dir = _seed_and_invoke(
        tmp_path, monkeypatch, "alu",
        "Design a multi-operation ALU (add/sub/and/or) with a zero flag.",
    )
    result = _run_real_cocotb(artifact_dir, "alu", inject_timescale=True)
    assert result.get("status") == "pass", (
        "ALU RTL failed cocotb (free-input width truncation): "
        f"phase={result.get('phase')} error={result.get('error')}\n"
        f"{result.get('raw', '')[-1500:]}"
    )


# ===========================================================================
# Third medium design — 8-bit accumulator (self-referential datapath + RC1)
# ===========================================================================
#
# The accumulator is the first fixture whose next-state references BOTH its own
# register and a free data input (`acc <= acc + din`, gated by `en`), and the
# first to use an ACTIVE-LOW reset (`rst_n`). A green cocotb run here proves two
# things end to end through the REAL engine + Compiler 2: (1) a self-referential,
# enable-gated, wrapping datapath register is functionally correct, and (2) RC1's
# active-low reset polarity (`if (!rst_n)`) is honoured in generated RTL — the
# branch the live FSM run never exercised.

_ACC_PROMPT = "Design an 8-bit accumulator with an enable and a synchronous active-low reset."


def test_end_to_end_offline_accumulator_chain_completes(tmp_path, monkeypatch):
    """Full graph on the accumulator: chain completes through Stage 3, active-low.

    Asserts the 01/02/03 chain is all 'success', the RTL is a clocked 8-bit
    register block, and — the RC1 signature — the reset is emitted active-low
    (`if (!rst_n)`), not the hardcoded active-high form the bridge/Compiler 2
    used before RC1.
    """
    artifact_dir = _seed_and_invoke(
        tmp_path, monkeypatch, "accumulator", _ACC_PROMPT,
    )

    assert _status(artifact_dir, "01_summary.json") == "success"
    assert _status(artifact_dir, "02_testbench_meta.json") == "success"
    assert _status(artifact_dir, "02_formal_spec.json") == "success"
    assert _status(artifact_dir, "03_rtl_output.json") == "success", (
        "Stage 3 did not produce success RTL; "
        f"03_rtl_output.json = {(artifact_dir / '03_rtl_output.json').read_text()[:500]}"
    )

    verilog = (artifact_dir / "output.v").read_text()
    assert "always @(posedge clk)" in verilog
    assert "output reg [7:0] acc" in verilog
    # RC1: active-low reset polarity must reach codegen.
    assert "!rst_n" in verilog, (
        "RC1 regression: active-low reset not emitted as `if (!rst_n)`:\n" + verilog
    )
    try:
        verify_banlist(verilog)
    except BanlistViolation as exc:  # pragma: no cover - failure path
        pytest.fail(f"Generated accumulator RTL violates the banlist: {exc}")


@pytest.mark.skipif(not _HAVE_IVERILOG, reason="iverilog not installed")
def test_end_to_end_offline_accumulator_lints_clean(tmp_path, monkeypatch):
    """The graph-generated accumulator Verilog lints clean under iverilog."""
    artifact_dir = _seed_and_invoke(
        tmp_path, monkeypatch, "accumulator", _ACC_PROMPT,
    )
    import subprocess
    result = subprocess.run(
        ["iverilog", "-Wall", "-t", "null", str(artifact_dir / "output.v")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        "Generated accumulator RTL failed iverilog lint:\n"
        f"{result.stdout}\n{result.stderr}\n\n{(artifact_dir / 'output.v').read_text()}"
    )


@pytest.mark.skipif(not _HAVE_COCOTB, reason="iverilog + cocotb-config required")
def test_end_to_end_offline_accumulator_cocotb(tmp_path, monkeypatch):
    """Functional verification: the graph's accumulator RTL PASSes cocotb.

    The headline proof for this design class. The fixture's vectors exercise
    accumulate, hold (en=0), and 8-bit wraparound, each expected value depending
    on the previous (self-referential state). A PASS proves the bridge sized the
    8-bit `din` free input correctly (D2), the enable-gated self-update is right,
    and the active-low reset (RC1) cleared acc to 0 before the first vector.
    """
    artifact_dir = _seed_and_invoke(
        tmp_path, monkeypatch, "accumulator", _ACC_PROMPT,
    )
    result = _run_real_cocotb(artifact_dir, "accumulator", inject_timescale=True)
    assert result.get("status") == "pass", (
        "Accumulator RTL failed cocotb (self-referential accumulate / active-low reset):\n"
        f"phase={result.get('phase')} error={result.get('error')}\n"
        f"{result.get('raw', '')[-2000:]}"
    )
