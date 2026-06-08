"""Deterministic fragility tests for the cocotb testbench generator (G14).

The generator (`pipeline/cocotb/generator.py`) is pure Python string templating
over `SpecSummary.test_vectors` — no LLM, no simulator. These tests are therefore
all string-introspection on the generated `.py` text; none of them launch Icarus.

Audited fragilities (audit finding G14; see docs/status.md):
  1. Test-vector values are interpolated *raw* via f-strings (`generator.py:71,82-84`).
     Since `inputs`/`expected` are `dict[str, Any]`, a string `'x'`, a bool, or a
     hex/4-state literal does NOT survive as the value the spec author intended:
       - `'x'`  -> `dut.a.value = x`   (bare name -> NameError at sim runtime)
       - `'1z'` -> `dut.a.value = 1z`  (invalid token -> SyntaxError, uncollectable)
       - `'0xff'` -> `dut.a.value = 0xff` (quotes silently stripped: str -> int 255)
       - `True` -> `dut.a.value = True` (parses; but the failure-message text and the
                                         driven value diverge from a plain `1`)
  2. Clock/edge are hardcoded to `dut.clk` / `RisingEdge(dut.clk)` (`generator.py:24-25,74`).
     A design whose clock port is NOT literally `clk` is never clocked correctly — the
     generator ignores `summary.ports` entirely.
  3. EMPTY `test_vectors` yields a `@cocotb.test()` body with no drives and no asserts:
     a *vacuously passing* testbench that verifies nothing.

These fragilities were fixed in `generator.py` (FIX WAVE bucket 1): values are
formatted via `_fmt_value` (strings quoted with `json.dumps`, bools normalized to
1/0, ints bare), the clock port is derived from `summary.ports` via `_clock_port`,
and an empty `test_vectors` list emits a failing assert instead of a vacuous pass.
The tests below assert that fixed behavior. Behaviors the generator already got
*right* (active-low vs active-high reset polarity ordering) are asserted as
ordinary passing tests so we notice if they ever regress.

Run with:
    python3.11 -m pytest tests/test_cocotb_generator.py -q
"""

from __future__ import annotations

import ast
import pathlib
import sys
import tempfile

# Ensure the project root is on sys.path when run directly / under pytest.
_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from pipeline.cocotb.generator import generate_testbench
# Import the pydantic models under non-`Test*` names so pytest does not emit a
# PytestCollectionWarning trying to collect `TestVector` (a model, not a suite).
from pipeline.schemas import summary_schema as _ss

SpecSummary = _ss.SpecSummary
Vector = _ss.TestVector  # local alias; never named Test* at module scope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate(summary: SpecSummary) -> str:
    """Run the generator into a temp dir and return the testbench source text."""
    with tempfile.TemporaryDirectory(prefix="cocotb_gen_") as tmp:
        out = pathlib.Path(tmp) / f"test_{summary.module_name}.py"
        generate_testbench(summary, out)
        assert out.exists(), "generator did not write the testbench file"
        return out.read_text()


def _drive_lines(src: str, port: str) -> list[str]:
    """All lines that drive `dut.<port>.value = ...` (input assignments)."""
    needle = f"dut.{port}.value ="
    return [ln for ln in src.splitlines() if needle in ln and "assert" not in ln]


def _assert_lines(src: str, port: str) -> list[str]:
    """All lines that assert `dut.<port>.value == ...` (expected-output checks)."""
    needle = f"assert dut.{port}.value =="
    return [ln for ln in src.splitlines() if needle in ln]


def _runtime_names(src: str) -> set[str]:
    """Free (unbound) names referenced at module scope in `src`.

    A bare identifier on the RHS of a `.value =` assignment (e.g. produced by an
    unquoted string value `'x'`) shows up here. `dut`, builtins, etc. are also
    free, so callers should look for the *specific* offending name.
    """
    tree = ast.parse(src)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            names.add(node.id)
    return names


# ---------------------------------------------------------------------------
# Baseline: a well-formed int-valued spec produces a clean, asserting testbench.
# This is the control case — if it ever breaks, the fragility tests below are moot.
# ---------------------------------------------------------------------------

def test_int_valued_vectors_parse_and_assert() -> None:
    """Plain int inputs/expecteds: valid Python, with a real driven + asserted value."""
    summary = SpecSummary(
        module_name="ctrl",
        description="control case",
        ports=[],
        test_vectors=[Vector(inputs={"en": 1}, expected={"q": 2})],
    )
    src = _generate(summary)
    ast.parse(src)  # must be syntactically valid Python
    # The generator initialises every input to 0 before the reset pulse (TB
    # hygiene, so undriven X cannot poison a registered DUT), then drives the
    # vector value. So `en` is driven 0 (init) then 1 (vector 0), in that order.
    assert _drive_lines(src, "en") == ["    dut.en.value = 0", "    dut.en.value = 1"]
    assert _assert_lines(src, "q"), "expected output `q` was not asserted"
    # The control case must NOT introduce a stray free name (the bug signature below).
    assert "x" not in _runtime_names(src)


# ---------------------------------------------------------------------------
# Fragility 1a — string value 'x' (cocotb don't-care) becomes a bare NameError.
# ---------------------------------------------------------------------------

def test_string_value_x_is_quoted_not_bare_name() -> None:
    summary = SpecSummary(
        module_name="m",
        description="don't-care input",
        ports=[],
        test_vectors=[Vector(inputs={"a": "x"}, expected={"q": 1})],
    )
    src = _generate(summary)
    # CORRECT behavior would be a quoted literal. Today we get a bare `x` name.
    assert "x" not in _runtime_names(src), (
        "string value 'x' leaked as a bare identifier (NameError at runtime); "
        f"drive line was: {_drive_lines(src, 'a')}"
    )


# ---------------------------------------------------------------------------
# Fragility 1b — a 4-state literal string '1z' produces an outright SyntaxError,
# so the generated testbench is not even collectable.
# ---------------------------------------------------------------------------

def test_fourstate_string_value_keeps_testbench_parseable() -> None:
    summary = SpecSummary(
        module_name="m",
        description="4-state literal",
        ports=[],
        test_vectors=[Vector(inputs={"a": "1z"}, expected={"q": 1})],
    )
    src = _generate(summary)
    ast.parse(src)  # quoted by _fmt_value, so the testbench stays parseable


# ---------------------------------------------------------------------------
# Fragility 1c — a hex string '0xff' silently loses its quotes (str -> int 255).
# It parses, but the round-trip fidelity (a string stays a string) is broken.
# ---------------------------------------------------------------------------

def test_hex_string_value_preserved_as_string() -> None:
    summary = SpecSummary(
        module_name="m",
        description="hex string",
        ports=[],
        test_vectors=[Vector(inputs={"a": "0xff"}, expected={"q": 1})],
    )
    src = _generate(summary)
    drive = _drive_lines(src, "a")
    assert drive == ['    dut.a.value = "0xff"'], (
        f"hex string not preserved as a quoted literal; got: {drive}"
    )


# ---------------------------------------------------------------------------
# Fragility 1d — a bool value. This one *parses* and even drives a cocotb-legal
# value, but the type silently diverges from an int and the failure-message text
# would read `expected q=False` instead of `q=0`. _fmt_value now normalizes the
# bool to 1/0 in both the drive and the message, which this test pins.
# ---------------------------------------------------------------------------

def test_bool_value_normalized_to_int() -> None:
    summary = SpecSummary(
        module_name="m",
        description="bool input/expected",
        ports=[],
        test_vectors=[Vector(inputs={"a": True}, expected={"q": False})],
    )
    src = _generate(summary)
    ast.parse(src)  # this part is fine
    # We WANT bools normalized to 1/0 for hardware. They are not.
    assert _drive_lines(src, "a") == ["    dut.a.value = 1"], (
        f"bool not normalized to int; got: {_drive_lines(src, 'a')}"
    )
    assert _assert_lines(src, "q") == [
        '    assert dut.q.value == 0, f"vector 0: expected q=0, got {dut.q.value}"'
    ], f"bool expected not normalized; got: {_assert_lines(src, 'q')}"


# ---------------------------------------------------------------------------
# Fragility 2 — non-`clk` clock port. The generator hardcodes dut.clk and
# never consults summary.ports, so a design clocked on `clock` is never driven.
# ---------------------------------------------------------------------------

def test_non_clk_clock_port_is_used() -> None:
    summary = SpecSummary(
        module_name="m",
        description="clock port named 'clock', not 'clk'",
        ports=[
            {"name": "clock", "direction": "input", "width": 1},
            {"name": "q", "direction": "output", "width": 1},
        ],
        test_vectors=[Vector(inputs={}, expected={"q": 1})],
    )
    src = _generate(summary)
    # The generator should clock the design's actual clock port.
    assert "dut.clock" in src, (
        "generator hardcoded dut.clk and ignored the 'clock' port; "
        "design would never be clocked"
    )
    assert "dut.clk" not in src, (
        "generator references a nonexistent dut.clk port"
    )


# ---------------------------------------------------------------------------
# Fragility 3 — empty test_vectors must NOT yield a vacuously-passing testbench.
# ---------------------------------------------------------------------------

def test_empty_vectors_does_not_produce_vacuous_pass() -> None:
    summary = SpecSummary(
        module_name="m",
        description="no vectors at all",
        ports=[],
        test_vectors=[],
    )
    src = _generate(summary)
    ast.parse(src)  # it is valid Python...
    # ...but a testbench with a @cocotb.test() and zero assertions is a vacuous
    # pass. Require that an empty-vector spec does not silently produce one.
    has_test_decl = "@cocotb.test()" in src
    has_no_asserts = "assert" not in src
    assert not (has_test_decl and has_no_asserts), (
        "empty test_vectors produced a @cocotb.test() with no assertions: "
        "this testbench passes while verifying nothing"
    )


# ---------------------------------------------------------------------------
# Behaviors the generator gets RIGHT — assert as ordinary passing tests so a
# regression in reset polarity is caught. (Quality gate: reset polarity matches
# summary.reset_active_low.)
# ---------------------------------------------------------------------------

def test_active_low_reset_polarity_and_ordering() -> None:
    """Active-low reset: assert with 0 first, then deassert with 1."""
    summary = SpecSummary(
        module_name="m",
        description="active-low reset",
        ports=[],
        test_vectors=[Vector(inputs={"a": 1}, expected={"q": 1})],
        reset_port="rst_n",
        reset_active_low=True,
    )
    src = _generate(summary)
    ast.parse(src)
    lines = src.splitlines()
    assert_idx = next(i for i, l in enumerate(lines) if "dut.rst_n.value = 0" in l)
    deassert_idx = next(i for i, l in enumerate(lines) if "dut.rst_n.value = 1" in l)
    assert assert_idx < deassert_idx, (
        "active-low reset must assert (=0) before deasserting (=1)"
    )


def test_active_high_reset_polarity_and_ordering() -> None:
    """Active-high reset: assert with 1 first, then deassert with 0."""
    summary = SpecSummary(
        module_name="m",
        description="active-high reset",
        ports=[],
        test_vectors=[Vector(inputs={"a": 1}, expected={"q": 1})],
        reset_port="rst",
        reset_active_low=False,
    )
    src = _generate(summary)
    ast.parse(src)
    lines = src.splitlines()
    assert_idx = next(i for i, l in enumerate(lines) if "dut.rst.value = 1" in l)
    deassert_idx = next(i for i, l in enumerate(lines) if "dut.rst.value = 0" in l)
    assert assert_idx < deassert_idx, (
        "active-high reset must assert (=1) before deasserting (=0)"
    )


def test_no_reset_port_emits_no_reset_block() -> None:
    """When reset_port is None the generator emits no reset drive at all."""
    summary = SpecSummary(
        module_name="m",
        description="no reset",
        ports=[],
        test_vectors=[Vector(inputs={"a": 1}, expected={"q": 1})],
        reset_port=None,
    )
    src = _generate(summary)
    ast.parse(src)
    # No reset port name to drive; only the clock + vector lines should appear.
    assert ".value = " in src  # the vector drive exists
    # A heuristic guard: nothing named rst* is driven.
    assert "rst" not in src
