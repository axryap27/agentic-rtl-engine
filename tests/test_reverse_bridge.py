"""
Reverse-bridge multi-variable / multi-bit coverage (test-audit gap G11).

Every existing bridge test (`test_dff.py`, the BUG-17 cases in
`test_compilers.py`) feeds `engine_spec_to_rtl_tla` exactly ONE engine variable,
so the reverse bridge's multi-variable code paths are never exercised:

  * the per-variable ``\\* width: N`` comment loop in the VARIABLES block,
  * the comma-emission logic across MORE THAN ONE declared variable
    (trailing/missing-comma correctness only shows up with >= 2 names),
  * per-variable width correctness when TWO variables BOTH have width > 1.

This file closes that gap. It drives a refined FSM+datapath engine-spec with:

  * two variables that BOTH have width > 1  (``state`` width 2, ``data`` width 8),
  * a reset action,
  * >= 2 clocked actions,
  * a guard referencing a FREE input (``en``) that is not a declared variable,

through the real reverse bridge (`engine_spec_to_rtl_tla`), then round-trips it
through Compiler 2 (`compile_tla_to_verilog`). It is fully deterministic and
offline (no LLM, no agents).

Run with:
    python3.11 -m pytest tests/test_reverse_bridge.py -q
Or directly:
    python3.11 tests/test_reverse_bridge.py
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

# Ensure the project root is on sys.path when run directly.
_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from pipeline.compilers.compiler2 import compile_tla_to_verilog
from pipeline.refinement.bridge import engine_spec_to_rtl_tla


# ---------------------------------------------------------------------------
# Fixtures: hand-built refined FSM+datapath engine specs (no LLM)
# ---------------------------------------------------------------------------
# These are *post-refinement* engine specs (the shape engine.run() returns):
# concrete widths, reset_value present, clocked=True. We build them directly so
# the test isolates the reverse bridge (engine spec -> RTL-style TLA+) rather
# than re-testing the refinement engine.


def _fsm_datapath_engine_spec() -> dict:
    """A 2-variable FSM+datapath spec, both vars width > 1, free input `en`.

    `state` (width 2) is a 4-state ring counter; `data` (width 8) is an 8-bit
    accumulator that wraps at 255. The `Accumulate` action is guarded by the
    free input `en` (an external enable, not a declared variable, not clk/reset).
    Both data and state self-update from their own prior value plus constants, so
    no narrow signal feeds a wide register (the width-inference path for that is
    covered by test_narrow_free_input_feeds_wide_register).
    """
    return {
        "variables": [
            {"name": "state", "type": "Nat", "width": 2, "abstract": False,
             "reset_value": "0", "clocked": True},
            {"name": "data", "type": "Nat", "width": 8, "abstract": False,
             "reset_value": "0", "clocked": True},
        ],
        "actions": [
            {"name": "Reset", "guard": "reset = 1", "clocked": True,
             "updates": [
                 {"variable": "state", "expression": "0"},
                 {"variable": "data", "expression": "0"},
             ]},
            # Clocked action #1 — datapath accumulate, guarded by free input `en`.
            {"name": "Accumulate", "guard": "en = 1", "clocked": True,
             "updates": [
                 {"variable": "data",
                  "expression": "IF data = 255 THEN 0 ELSE data + 1"},
             ]},
            # Clocked action #2 — FSM ring advance.
            {"name": "Advance", "guard": "TRUE", "clocked": True,
             "updates": [
                 {"variable": "state",
                  "expression": "IF state = 3 THEN 0 ELSE state + 1"},
             ]},
        ],
        "reset_action": "Reset",
        "init": "state = 0 /\\ data = 0",
        "invariants": [],
    }


# ---------------------------------------------------------------------------
# Lint helper (verilator preferred, iverilog second pass) — tool-guarded.
# ---------------------------------------------------------------------------

def _have_linter() -> bool:
    return shutil.which("verilator") is not None or shutil.which("iverilog") is not None


def _run_linter(verilog_src: str) -> tuple[int, str]:
    """Write verilog_src to a temp file, lint it, return (exit_code, output).

    Mirrors tests/test_compilers.py::_run_linter: verilator first (strict width
    checks), then iverilog as a second pass. Skips a tool that is not installed
    rather than erroring; the caller guards with _have_linter().
    """
    with tempfile.NamedTemporaryFile(suffix=".v", mode="w", delete=False) as f:
        f.write(verilog_src)
        fname = f.name
    try:
        out_parts: list[str] = []
        if shutil.which("verilator") is not None:
            r = subprocess.run(
                ["verilator", "--lint-only", fname],
                capture_output=True, text=True,
            )
            combined = r.stdout + r.stderr
            out_parts.append(f"[verilator] {combined}")
            if r.returncode != 0:
                return r.returncode, "\n".join(out_parts)
        if shutil.which("iverilog") is not None:
            r2 = subprocess.run(
                ["iverilog", "-Wall", "-t", "null", fname],
                capture_output=True, text=True,
            )
            combined2 = r2.stdout + r2.stderr
            out_parts.append(f"[iverilog] {combined2}")
            if r2.returncode != 0:
                return r2.returncode, "\n".join(out_parts)
        return 0, "\n".join(out_parts) or "[no linter] skipped"
    finally:
        os.unlink(fname)


# ---------------------------------------------------------------------------
# 1. Structural assertions on the reverse-bridge TLA+ text (no linter needed)
# ---------------------------------------------------------------------------

def test_multivar_width_comments_per_variable() -> None:
    """Each variable carries its OWN correct `\\* width: N` comment.

    The width-comment loop runs once per variable; with two declared vars of
    DIFFERENT widths (2 and 8) a bug that emitted a single shared width, or
    transposed widths, or dropped the comment for the second var, would surface
    here. clk/reset are always single-bit.
    """
    tla = engine_spec_to_rtl_tla(_fsm_datapath_engine_spec(), "fsm_dp")

    assert re.search(r"\bstate\b\s*,?\s*\\\* width: 2\b", tla), (
        f"`state` missing its `\\* width: 2` comment:\n{tla}"
    )
    assert re.search(r"\bdata\b\s*,?\s*\\\* width: 8\b", tla), (
        f"`data` missing its `\\* width: 8` comment:\n{tla}"
    )
    # clk and reset are implicit single-bit ports.
    assert re.search(r"\bclk\b\s*,?\s*\\\* width: 1\b", tla), tla
    assert re.search(r"\breset\b\s*,?\s*\\\* width: 1\b", tla), tla
    # Per-var widths must not be swapped: `data` is never width 2, `state` never 8.
    assert not re.search(r"\bdata\b\s*,?\s*\\\* width: 2\b", tla), tla
    assert not re.search(r"\bstate\b\s*,?\s*\\\* width: 8\b", tla), tla


def test_free_input_declared_width1_input_port() -> None:
    """The guard-only free input `en` is declared (BUG-18) at width 1.

    `en` appears only in the `Accumulate` guard — it is not a declared variable,
    not clk/reset, not a TLA+ keyword. The reverse bridge must add it to the
    VARIABLES block so Compiler 2's "not driven by either block -> input port"
    classifier exposes it. The bridge does not invent multi-bit widths from a
    bare identifier, so the comment must read width 1.
    """
    tla = engine_spec_to_rtl_tla(_fsm_datapath_engine_spec(), "fsm_dp")
    assert re.search(r"\ben\b\s*,?\s*\\\* width: 1\b", tla), (
        f"free input `en` not declared at width 1:\n{tla}"
    )


def test_reset_emitted_as_well_formed_if_block() -> None:
    """The reset action is emitted as `IF reset = 1 THEN ... ELSE ...`.

    Both reset-branch assignments (state, data) and both else-branch clocked
    updates must land inside the single clocked IF, conjoined with `/\\`.
    """
    tla = engine_spec_to_rtl_tla(_fsm_datapath_engine_spec(), "fsm_dp")

    assert "UpdatePipeline ==" in tla, tla
    assert re.search(r"/\\\s*IF reset = 1 THEN", tla), (
        f"reset not emitted as a clocked `IF reset = 1 THEN` block:\n{tla}"
    )
    # Structure: THEN ... ELSE ... within UpdatePipeline.
    m = re.search(
        r"IF reset = 1 THEN(?P<then>.*?)\bELSE\b(?P<els>.*)", tla, re.DOTALL
    )
    assert m is not None, f"no THEN/ELSE structure in reset block:\n{tla}"
    then_blk, else_blk = m.group("then"), m.group("els")
    # Reset (THEN) branch zeroes both registers.
    assert re.search(r"state'\s*=\s*0", then_blk), then_blk
    assert re.search(r"data'\s*=\s*0", then_blk), then_blk
    # ELSE branch carries the two clocked updates (the non-reset actions).
    assert "data'" in else_blk and "state'" in else_blk, else_blk
    # Each next-state assignment is a `/\\`-conjoined clause.
    assert else_blk.count("/\\") >= 2, (
        f"ELSE branch should conjoin >= 2 clocked updates:\n{else_blk}"
    )


def test_variables_comma_emission_well_formed_multivar() -> None:
    """Comma emission in VARIABLES is well-formed for MULTIPLE variables.

    Every declared name except the LAST gets a trailing comma; the last gets
    none. With one variable (every existing test) this is trivially correct;
    only >= 2 names can reveal a missing-comma (name1 name2) or trailing-comma
    (last,) bug. We parse the VARIABLES block and check each line directly.
    """
    tla = engine_spec_to_rtl_tla(_fsm_datapath_engine_spec(), "fsm_dp")
    lines = tla.splitlines()

    start = next(i for i, l in enumerate(lines) if l.strip() == "VARIABLES")
    # The VARIABLES block runs until the first blank line after it.
    block: list[str] = []
    for l in lines[start + 1:]:
        if l.strip() == "":
            break
        block.append(l)

    assert len(block) >= 4, f"expected >= 4 declared names, got:\n{block}"

    # Strip the width comment to inspect the bare `name[,]` token.
    decls: list[str] = []
    for l in block:
        code = l.split("\\*", 1)[0].rstrip()  # drop trailing width comment
        decls.append(code.strip())

    # Every non-last declaration ends with exactly one comma; the last has none.
    for d in decls[:-1]:
        assert d.endswith(","), f"non-last declaration missing comma: {d!r}\n{block}"
        assert not d.endswith(",,"), f"double comma: {d!r}"
        # name + single trailing comma, nothing else.
        assert re.fullmatch(r"[A-Za-z_]\w*,", d), f"malformed declaration: {d!r}"
    last = decls[-1]
    assert not last.endswith(","), f"trailing comma on last declaration: {last!r}"
    assert re.fullmatch(r"[A-Za-z_]\w*", last), f"malformed last declaration: {last!r}"

    # The two width>1 vars are the first two declared (engine vars come first).
    names = [d.rstrip(",") for d in decls]
    assert "state" in names and "data" in names, names
    assert "clk" in names and "reset" in names and "en" in names, names


# ---------------------------------------------------------------------------
# 2. Round-trip through Compiler 2 + lint (tool-guarded)
# ---------------------------------------------------------------------------

def test_multivar_compiles_to_correct_ranges() -> None:
    """Compiler 2 sizes each multi-bit signal with the right `[N-1:0]` range.

    No linter required — pure string check on the emitted Verilog. This pins the
    per-var width carry across BOTH multi-bit variables simultaneously (the BUG-17
    fix, exercised here for two vars of different widths at once).
    """
    tla = engine_spec_to_rtl_tla(_fsm_datapath_engine_spec(), "fsm_dp")
    verilog = compile_tla_to_verilog(tla, "fsm_dp")

    assert re.search(r"output\s+reg\s+\[1:0\]\s+state\b", verilog), (
        f"`state` (width 2) not sized [1:0]:\n{verilog}"
    )
    assert re.search(r"output\s+reg\s+\[7:0\]\s+data\b", verilog), (
        f"`data` (width 8) not sized [7:0]:\n{verilog}"
    )
    # Free input declared as a scalar input port.
    assert re.search(r"input\s+en\b", verilog), (
        f"free input `en` not declared as a port:\n{verilog}"
    )
    # Ranges must not be swapped.
    assert not re.search(r"\[7:0\]\s+state\b", verilog), verilog
    assert not re.search(r"\[1:0\]\s+data\b", verilog), verilog


def test_multivar_lints_clean_no_width_or_drive_warnings() -> None:
    """Round-trip Verilog lints clean: no WIDTHTRUNC / UNDRIVEN / MULTIDRIVEN."""
    if not _have_linter():
        pytest.skip("neither verilator nor iverilog installed")

    tla = engine_spec_to_rtl_tla(_fsm_datapath_engine_spec(), "fsm_dp")
    verilog = compile_tla_to_verilog(tla, "fsm_dp")
    rc, out = _run_linter(verilog)

    for warn in ("WIDTHTRUNC", "WIDTHEXPAND", "UNDRIVEN", "MULTIDRIVEN"):
        assert warn not in out, (
            f"{warn} present in lint output:\n{out}\n\nVerilog:\n{verilog}"
        )
    assert rc == 0, (
        f"multi-var FSM+datapath Verilog failed to lint (rc={rc}):\n{out}\n\n"
        f"Verilog:\n{verilog}"
    )


# ---------------------------------------------------------------------------
# 3. Multi-var determinism
# ---------------------------------------------------------------------------

def test_multivar_bridge_output_deterministic() -> None:
    """The reverse bridge is a pure function: same spec -> byte-identical TLA+.

    Free-input ordering (sorted by _free_inputs) and the per-var loop must be
    stable across calls, even with multiple variables and multiple free inputs.
    """
    spec1 = _fsm_datapath_engine_spec()
    spec2 = _fsm_datapath_engine_spec()
    tla1 = engine_spec_to_rtl_tla(spec1, "fsm_dp")
    tla2 = engine_spec_to_rtl_tla(spec2, "fsm_dp")
    assert tla1 == tla2, "reverse bridge output is not deterministic"


def test_multivar_compile_deterministic() -> None:
    """Full reverse-bridge + Compiler 2 round-trip is byte-identical twice."""
    spec1 = _fsm_datapath_engine_spec()
    spec2 = _fsm_datapath_engine_spec()
    v1 = compile_tla_to_verilog(engine_spec_to_rtl_tla(spec1, "fsm_dp"), "fsm_dp")
    v2 = compile_tla_to_verilog(engine_spec_to_rtl_tla(spec2, "fsm_dp"), "fsm_dp")
    assert v1 == v2, "reverse-bridge -> Compiler 2 round-trip is not deterministic"


# ---------------------------------------------------------------------------
# D2 (fixed): a free input feeding a WIDE register inherits the register's width.
# ---------------------------------------------------------------------------
# Previously the reverse bridge declared every free input at width 1, so a free
# input assigned directly into a multi-bit register (e.g. `data <= din` where
# data is 8 bits) tripped verilator WIDTHEXPAND (RHS 1 bit, LHS 8 bits) and lint
# FAILED. D2 fix: engine_spec_to_rtl_tla now infers a free input's width from the
# register it feeds directly (_infer_free_input_width), so `din` is sized to 8
# bits and the FSM+datapath that loads an external bus lints clean. This pins the
# multi-var width-correctness fix G11 anticipated.

def _narrow_input_to_wide_reg_engine_spec() -> dict:
    """Same FSM+datapath, but `data` loads the 1-bit free input `din` directly."""
    return {
        "variables": [
            {"name": "state", "type": "Nat", "width": 2, "abstract": False,
             "reset_value": "0", "clocked": True},
            {"name": "data", "type": "Nat", "width": 8, "abstract": False,
             "reset_value": "0", "clocked": True},
        ],
        "actions": [
            {"name": "Reset", "guard": "reset = 1", "clocked": True,
             "updates": [
                 {"variable": "state", "expression": "0"},
                 {"variable": "data", "expression": "0"},
             ]},
            # `din` is a free input used as the RHS of a WIDE (8-bit) register.
            {"name": "Load", "guard": "load = 1", "clocked": True,
             "updates": [{"variable": "data", "expression": "din"}]},
            {"name": "Advance", "guard": "TRUE", "clocked": True,
             "updates": [
                 {"variable": "state",
                  "expression": "IF state = 3 THEN 0 ELSE state + 1"},
             ]},
        ],
        "reset_action": "Reset",
        "init": "state = 0 /\\ data = 0",
        "invariants": [],
    }


def test_narrow_free_input_feeds_wide_register() -> None:
    """A free input loaded into an 8-bit register lints clean (D2 width inference).

    D2 fix: the bridge sizes `din` to the width of the register it feeds (`data`,
    8-bit), so verilator no longer flags WIDTHEXPAND. Previously every free input
    was width 1 and this expanded.
    """
    if not _have_linter():
        pytest.skip("neither verilator nor iverilog installed")
    # Specifically require verilator — it is the tool that performs the width
    # check; iverilog alone does not flag WIDTHEXPAND, so the bug would be hidden.
    if shutil.which("verilator") is None:
        pytest.skip("verilator (the WIDTHEXPAND checker) not installed")

    tla = engine_spec_to_rtl_tla(_narrow_input_to_wide_reg_engine_spec(), "fsm_dp")
    verilog = compile_tla_to_verilog(tla, "fsm_dp")
    rc, out = _run_linter(verilog)
    assert "WIDTHEXPAND" not in out, (
        f"WIDTHEXPAND from narrow free input into wide register:\n{out}\n\n{verilog}"
    )
    assert rc == 0, f"narrow-input/wide-register lint failed:\n{out}\n\n{verilog}"


# ---------------------------------------------------------------------------
# Entry point (dual-mode: pytest + direct execution, per CLAUDE.md)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
