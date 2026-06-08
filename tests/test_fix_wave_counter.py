"""Regression tests for the 2-bit-counter fix wave.

The first live `main.py` run on a 2-bit counter emitted broken Verilog whose
`count` was stuck at `00`. Five root causes were identified; these tests pin the
three DETERMINISTIC fixes through the REAL emit path (bridge -> Compiler 2 ->
iverilog/cocotb), so the bugs cannot silently return. The fourth fix (the
Agent-3 prompt) is non-deterministic and is verified separately by a future
live rerun; it is not covered here.

The fixes under test:
  FIX 1  Reset-port name threading: the design's actual reset port name is
         threaded end-to-end (bridge + Compiler 2 + stage3), defaulting to
         "reset". A state variable colliding with the reset port (or "clk") is
         dropped so the reset/clock are PORTS, never `output reg`s.
  FIX 2  Multiple clocked actions writing one variable are composed into a
         single guarded next-state (nested ternary), not colliding nonblocking
         assigns where the last assign wins.
  FIX 3  English boolean word operators (AND/OR/NOT) in guards/updates are
         translated to TLA+ symbolic form before the RTL path, so Compiler 2 and
         the free-input scanner see operators, not words (no phantom ports).

Run:
    python3.11 -m pytest tests/test_fix_wave_counter.py -q

Sim tests (the headline end-to-end) SKIP when cocotb / iverilog / vvp are
absent, mirroring tests/test_cocotb_roundtrip.py.
"""

from __future__ import annotations

import copy
import pathlib
import re
import shutil
import sys
import tempfile

import pytest

# Allow running under pytest / directly without installing the package.
_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from pipeline.compilers.compiler2 import RTLTLACompiler, compile_tla_to_verilog
from pipeline.compilers.compiler2 import verify_banlist, BanlistViolation
from pipeline.refinement.bridge import (
    engine_spec_to_rtl_tla,
    formal_spec_to_engine_spec,
)
from pipeline.refinement.engine import is_rtl_style, run as engine_run
from pipeline.schemas.tla_schema import FormalSpec
# Imported via the module so pytest does not try to collect the pydantic model
# `TestVector` (name starts with "Test") as a test class.
from pipeline.schemas import summary_schema as _ss

SpecSummary = _ss.SpecSummary
Vector = _ss.TestVector


# ---------------------------------------------------------------------------
# Tool-availability guard (mirrors tests/test_cocotb_roundtrip.py).
# ---------------------------------------------------------------------------

def _require_sim_tools() -> None:
    """Skip the calling test unless cocotb + iverilog + vvp are all present."""
    pytest.importorskip("cocotb", reason="cocotb not installed")
    if shutil.which("iverilog") is None:
        pytest.skip("iverilog not installed")
    if shutil.which("vvp") is None:
        pytest.skip("vvp not installed")
    if shutil.which("cocotb-config") is None:
        pytest.skip("cocotb-config not on PATH")


# ===========================================================================
# FIX 1 — reset-port name threading
# ===========================================================================

def _counter_engine_spec_rst() -> dict:
    """A 2-bit up-counter whose reset port is `rst` (not `reset`).

    Three mutually-exclusive clocked actions writing `count`: Reset (rst=1 -> 0),
    Increment (count<3 -> count+1), Wrap (count=3 -> 0). This is the SHAPE a
    corrected Agent 3 would emit for the failing case, with SYMBOLIC guards.
    """
    return {
        "variables": [
            {"name": "count", "type": "Nat", "width": 2, "abstract": False,
             "reset_value": "0", "clocked": True},
        ],
        "actions": [
            {"name": "Reset", "guard": "rst = 1", "clocked": True,
             "updates": [{"variable": "count", "expression": "0"}]},
            {"name": "Increment", "guard": "count < 3", "clocked": True,
             "updates": [{"variable": "count", "expression": "count + 1"}]},
            {"name": "Wrap", "guard": "count = 3", "clocked": True,
             "updates": [{"variable": "count", "expression": "0"}]},
        ],
        "reset_action": "Reset",
        "init": "count = 0",
        "invariants": [],
    }


def test_reset_port_named_rst_is_threaded_end_to_end() -> None:
    """FIX 1: reset_port='rst' is emitted as `input rst` and `if (rst)`.

    The cocotb generator drives `dut.rst`; the emitted module must declare an
    `rst` input of the same name (else the reset floats and the design never
    resets — the headline bug). Threading reset_port='rst' through both the
    reverse bridge AND Compiler 2 must produce a clean `input rst` / `if (rst)`,
    with NO bare `reset` port and `rst` NEVER an `output reg`.
    """
    tla = engine_spec_to_rtl_tla(
        _counter_engine_spec_rst(), "counter", reset_port="rst"
    )
    verilog = compile_tla_to_verilog(tla, "counter", reset_port="rst")

    # rst is declared as an input port (allow one or two spaces after `input`).
    assert re.search(r"input\s+rst\b", verilog), (
        f"reset port `rst` not declared as input:\n{verilog}"
    )
    # The clocked reset condition uses rst.
    assert "if (rst)" in verilog, f"`if (rst)` not emitted:\n{verilog}"
    # No bare `reset` port leaked in (the old hardcoded name).
    assert not re.search(r"\breset\b", verilog), (
        f"stale `reset` token present — reset port name not fully threaded:\n{verilog}"
    )
    # rst must NOT be an output reg (it is a port, not a register).
    assert not re.search(r"output\s+reg[^;]*\brst\b", verilog), (
        f"`rst` wrongly emitted as an output reg:\n{verilog}"
    )


def test_variable_named_like_reset_is_dropped_as_register() -> None:
    """FIX 1: a state variable colliding with the reset port becomes the input.

    Agent 3 sometimes models the reset as a state VARIABLE (the failing run
    declared `rst` in variables -> a bogus `output reg rst`). When reset_port
    also == 'rst', the bridge must DROP that variable from the register set so
    `rst` is emitted exactly ONCE — as the reset input — never as a register.
    """
    spec = _counter_engine_spec_rst()
    # Inject `rst` as a (bogus) state variable, exactly as Agent 3 did.
    spec["variables"].insert(0, {
        "name": "rst", "type": "Bit", "width": 1, "abstract": False,
        "reset_value": "0", "clocked": True,
    })
    tla = engine_spec_to_rtl_tla(spec, "counter", reset_port="rst")
    verilog = compile_tla_to_verilog(tla, "counter", reset_port="rst")

    # rst is the input, declared once.
    assert len(re.findall(r"input\s+rst\b", verilog)) == 1, (
        f"`rst` should be declared exactly once as the reset input:\n{verilog}"
    )
    # rst is NOT a register / output reg.
    assert not re.search(r"output\s+reg[^;]*\brst\b", verilog), (
        f"`rst` wrongly emitted as an output reg:\n{verilog}"
    )
    assert not re.search(r"\breg\s+rst\b", verilog), (
        f"`rst` wrongly emitted as an internal reg:\n{verilog}"
    )
    # `rst <=` would mean it is being driven as a register — must not appear.
    assert "rst <=" not in verilog, (
        f"`rst` wrongly driven as a register (`rst <=`):\n{verilog}"
    )


# ===========================================================================
# FIX 2 — multiple clocked actions on one variable compose to ONE driver
# ===========================================================================

def test_multi_action_same_var_composes_to_single_driver() -> None:
    """FIX 2: two clocked actions writing `count` compose to ONE nested ternary.

    Increment (`count < 3` -> count+1) and Wrap (`count = 3` -> 0) both write
    `count`. Emitting each as its own flat conjunct produced TWO `count <=`
    assigns (last wins -> stuck at 0). The bridge must compose them across
    actions into a single guarded next-state, so the ELSE branch has exactly one
    `count <=` and it is a nested ternary.
    """
    tla = engine_spec_to_rtl_tla(
        _counter_engine_spec_rst(), "counter", reset_port="rst"
    )
    verilog = compile_tla_to_verilog(tla, "counter", reset_port="rst")

    # Split into reset (THEN) and non-reset (ELSE) bodies of the clocked block.
    m = re.search(
        r"if \(rst\) begin(?P<then>.*?)end else begin(?P<els>.*?)end",
        verilog, re.DOTALL,
    )
    assert m is not None, f"clocked if/else block not found:\n{verilog}"
    else_body = m.group("els")

    # Exactly one `count <=` in the non-reset branch (no colliding assigns).
    count_assigns = re.findall(r"count\s*<=", else_body)
    assert len(count_assigns) == 1, (
        f"expected exactly ONE `count <=` in the ELSE branch, got "
        f"{len(count_assigns)} (multi-action collision):\n{else_body}"
    )
    # That single assignment is a nested ternary (composed priority logic).
    assert "?" in else_body and ":" in else_body, (
        f"composed `count` next-state is not a ternary:\n{else_body}"
    )
    # Banlist-clean (no leaked IF/THEN/ELSE keywords from a mis-split).
    verify_banlist(verilog)


# ===========================================================================
# FIX 3 — word-operator robustness (boolean AND/OR/NOT translated)
# ===========================================================================

def _word_op_formal_spec() -> FormalSpec:
    """A counter FormalSpec whose guards use English booleans + symbolic compares.

    Mirrors what Agent 3 emitted (minus the comparison-WORD bug, which the prompt
    fix prevents): boolean `NOT`/`AND` connectives around symbolic comparisons.
    The bridge must translate the booleans to TLA+ symbolic form so the RTL path
    never sees `AND`/`NOT` and the free-input scanner never mints phantom ports.
    """
    return FormalSpec(
        module_name="wc",
        description="counter with English boolean operators in guards",
        variables={"count": {"type": "Nat", "width": 2}},
        initial={"count": "0"},
        transitions=[
            {"label": "Reset", "condition": "rst = 1",
             "updates": {"count": "0"}},
            {"label": "Increment",
             "condition": "(NOT (rst = 1)) AND (count < 3)",
             "updates": {"count": "count + 1"}},
            {"label": "Wrap",
             "condition": "(NOT (rst = 1)) AND (count = 3)",
             "updates": {"count": "0"}},
        ],
        invariants=[],
    )


_WORD_OP_SEQUENCE: list[tuple[str, dict]] = [
    ("Initialization", {"reset_values": {"count": "0"}, "reset_action_name": "Reset"}),
    ("Iteration", {"action_name": "Increment"}),
    ("Iteration", {"action_name": "Wrap"}),
]


def _drive_to_rtl(engine_spec: dict, sequence: list[tuple[str, dict]], run_id: str) -> dict:
    """Drive an engine spec to RTL-style with a deterministic scripted picker."""
    state = {"i": 0}

    def stub_pick(applicable_rules: list[dict], _spec: dict) -> dict:
        names = {r["name"] for r in applicable_rules}
        for j in range(state["i"], len(sequence)):
            rule_name, params = sequence[j]
            if rule_name in names:
                state["i"] = j + 1
                return {"rule_name": rule_name, "params": params}
        return {"rule_name": applicable_rules[0]["name"], "params": {}}

    final = engine_run(
        formal_spec=copy.deepcopy(engine_spec),
        pick_rule=stub_pick,
        run_id=run_id,
    )
    assert is_rtl_style(final), "spec did not reach RTL-style"
    return final


def test_boolean_word_operators_translated_no_leak_and_banlist_clean() -> None:
    """FIX 3: English AND/OR/NOT are translated; no word/`~` tokens leak.

    Through the REAL FormalSpec -> bridge -> Compiler 2 path, a guard using
    `NOT`/`AND` plus symbolic comparisons must emit Verilog with NO leaked
    `AND`/`OR`/`NOT`/`~` tokens, no phantom input ports minted from words, and a
    clean banlist. The engine spec produced by the bridge must already be
    symbolic (so the free-input scanner sees operators, not words).
    """
    engine_spec = formal_spec_to_engine_spec(_word_op_formal_spec())

    # The bridge must have translated booleans to symbolic form in the engine
    # spec already (no AND/OR/NOT words survive in any guard).
    for action in engine_spec["actions"]:
        assert not re.search(r"\b(AND|OR|NOT)\b", action["guard"]), (
            f"boolean word operator survived in guard: {action['guard']!r}"
        )

    final = _drive_to_rtl(engine_spec, _WORD_OP_SEQUENCE, "fix3_wordops")
    tla = engine_spec_to_rtl_tla(final, "wc", reset_port="rst")
    verilog = compile_tla_to_verilog(tla, "wc", reset_port="rst")

    # No leaked word operators or the TLA+ `~` in emitted Verilog.
    assert not re.search(r"\b(AND|OR|NOT)\b", verilog), (
        f"leaked word operator in Verilog:\n{verilog}"
    )
    assert "~" not in verilog, f"leaked TLA+ `~` in Verilog:\n{verilog}"
    # No phantom ports minted from comparison words (the original bug: equals/
    # less/than became input ports). Nothing of the sort here.
    for phantom in ("equals", "less", "than"):
        assert not re.search(rf"input\s+{phantom}\b", verilog), (
            f"phantom input port `{phantom}` minted:\n{verilog}"
        )
    # Banlist-clean.
    verify_banlist(verilog)


def test_compiler2_translates_leaked_word_and_tilde_defensively() -> None:
    """FIX 3 (Compiler 2): a hand-written guard with `~`/word ops still translates.

    Defensive: if symbolic `~` or a word operator reaches Compiler 2 (e.g. TLA+
    fed directly, bypassing the bridge), `_translate_basic` must render Verilog
    logical operators, not leave the raw token.
    """
    c = RTLTLACompiler("", reset_port="reset")
    out = c.translate_expr("~ (count = 3)")
    assert "~" not in out, f"`~` not translated to `!`: {out!r}"
    assert "!" in out, f"expected logical NOT in: {out!r}"
    out2 = c.translate_expr("a = 1 AND b = 2")
    assert "AND" not in out2 and "&&" in out2, f"`AND` not translated: {out2!r}"


# ===========================================================================
# HEADLINE — full offline counter end-to-end (deterministic stand-in for the
# forbidden live `main.py` rerun). Proves count 0->1->2->3->0 with dut.rst
# driving reset, through bridge -> engine -> Compiler 2 -> iverilog -> cocotb.
# ===========================================================================

def test_headline_offline_counter_counts_and_resets() -> None:
    """HEADLINE: the corrected counter actually counts 0->1->2->3->0 in sim.

    Builds the counter the way a CORRECTED Agent 3 should (reset_port='rst',
    symbolic guards, 3 mutually-exclusive clocked actions), runs the FULL real
    path bridge -> Compiler 2, and simulates with the deterministic cocotb
    generator + runner. The generator drives `dut.rst` for the reset pulse, then
    holds rst=0 while the counter advances. We assert the count sequence
    1,2,3,0,1 (the +1 offset is the post-reset settle edge, mirroring
    tests/test_cocotb_roundtrip.py::test_generated_counter_behaves_increments),
    then a final rst=1 vector resets count to 0.

    This is the deterministic replacement for the forbidden live rerun: if the
    reset port floated, or `count` collided to a stuck 0, or the guards leaked,
    this sim would FAIL — exactly the original bug.

    Sequence rationale (no enable gate, so EVERY non-reset edge advances):
      reset block: edge 1 (rst=1) -> count=0; edge 2 (rst=0 settle) -> count=1.
      vector 0 (rst=0) -> count=2, v1 -> 3, v2 -> wrap 0, v3 -> 1, v4 -> 2, then
      a final rst=1 vector -> reset to 0. So the asserted sequence is 2,3,0,1,2,0
      — a full +1-mod-4 increment chain (with the wrap at 3->0) plus a working
      synchronous reset.
    """
    _require_sim_tools()
    from pipeline.cocotb.generator import generate_testbench
    from pipeline.cocotb.runner import run_testbench

    tla = engine_spec_to_rtl_tla(
        _counter_engine_spec_rst(), "counter", reset_port="rst"
    )
    verilog = compile_tla_to_verilog(tla, "counter", reset_port="rst")

    # Sanity (pre-sim): single driver, rst is the input, no stuck-at-0 collision.
    assert "if (rst)" in verilog and re.search(r"input\s+rst\b", verilog), verilog

    # Drive with rst=0 to count up (and wrap), then a final rst=1 to reset.
    # The counter already advanced once on the reset-deassert settle edge, so the
    # first sampled value is 2; each enabled edge then adds 1 mod 4.
    up_seq = [2, 3, 0, 1, 2]
    # Guard: the up sequence really is a +1-mod-4 increment chain (covers 3->0 wrap).
    assert all((up_seq[i + 1] - up_seq[i]) % 4 == 1 for i in range(len(up_seq) - 1))
    assert 0 in up_seq, "sequence must exercise the 3->0 wrap"
    vectors = [Vector(inputs={"rst": 0}, expected={"count": v}) for v in up_seq]
    vectors.append(Vector(inputs={"rst": 1}, expected={"count": 0}))  # reset works

    summary = SpecSummary(
        module_name="counter",
        description="2-bit up-counter generated by Compiler 2 (reset port = rst)",
        ports=[],
        test_vectors=vectors,
        reset_port="rst",          # generator drives dut.rst
        reset_active_low=False,
    )

    with tempfile.TemporaryDirectory(prefix="fix_wave_cnt_") as tmp:
        d = pathlib.Path(tmp)
        rtl = d / "counter.v"
        rtl.write_text("`timescale 1ns/1ps\n" + verilog + "\n")
        tb = d / "test_counter.py"
        generate_testbench(summary, tb)
        result = run_testbench(tb, rtl, "counter")
        assert result == {"status": "pass"}, (
            f"headline offline counter did not count 0->1->2->3->0 / reset via "
            f"dut.rst: {result}"
        )
