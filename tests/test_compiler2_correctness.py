"""
Compiler-2 correctness tests (deterministic, offline) for the fixes tracked in
docs/test_suite_problems.md:

  G04 — banlist must catch leaked TLA+ keywords (uppercase IF/THEN/ELSE/IN/LET/
        CASE) and a bare FORMAL_ONLY operand, WITHOUT false-positiving on
        legitimate lowercase Verilog (if/else/case/casez/casex), identifiers that
        merely contain those substrings, or a FORMAL_ONLY token inside a comment.
  G05 — Compiler 2 must refuse to emit an illegal multi-driver when the same
        variable is assigned in BOTH a combinational (non-clocked) action and a
        clocked action; it raises MultiDriverError and emits no Verilog.
  G12 — nested IF-THEN-ELSE (the form the bridge emits) renders to a nested
        ternary, not a collapsed single branch.

Plus a tool-guarded lint smoke confirming a representative Compiler-2 output
elaborates clean (no MULTIDRIVEN/BLKANDNBLK/WIDTHTRUNC).

Entry points exercised (discovered in pipeline/compilers/compiler2.py):
    compile_tla_to_verilog(tla_source, module_name) -> str
    RTLTLACompiler(tla_source).compile(module_name) / .translate_expr(expr)
    verify_banlist(verilog) -> None  (raises BanlistViolation)
    BanlistViolation, MultiDriverError

Run with:  python3.11 -m pytest tests/test_compiler2_correctness.py -q
Or:        python3.11 tests/test_compiler2_correctness.py
"""

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

# Make pipeline importable from repo root (mirrors tests/test_compilers.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.compilers.compiler2 import (  # noqa: E402
    BanlistViolation,
    MultiDriverError,
    RTLTLACompiler,
    SAMPLE_TLA,
    compile_tla_to_verilog,
    verify_banlist,
)
from pipeline.refinement.bridge import engine_spec_to_rtl_tla  # noqa: E402


# ===========================================================================
# G04 — banlist catches leaked TLA+ keywords
# ===========================================================================

# Each token is wrapped in a synthesizable-looking continuous assign so the only
# reason the verifier should fire is the leaked uppercase keyword itself.
_LEAKED_KEYWORDS = ["IF", "THEN", "ELSE", "IN", "LET", "CASE"]


@pytest.mark.parametrize("token", _LEAKED_KEYWORDS)
def test_g04_uppercase_tla_keyword_raises_and_names_token(token):
    """A word-boundary uppercase TLA+ keyword in emitted code must raise, and the
    BanlistViolation message must name the offending token."""
    bad = f"module foo;\n    assign x = {token};\nendmodule\n"
    with pytest.raises(BanlistViolation) as exc:
        verify_banlist(bad)
    msg = str(exc.value)
    # The message names the specific leaked keyword (label + the matched token).
    assert token in msg, f"message did not name {token!r}: {msg!r}"


def test_g04_bare_formal_only_operand_raises():
    """A bare FORMAL_ONLY operand surviving comment-stripping is a formal-only
    construct leaked into synthesizable code -> must raise and name it."""
    bad = "module foo;\n    assign x = FORMAL_ONLY;\nendmodule\n"
    with pytest.raises(BanlistViolation) as exc:
        verify_banlist(bad)
    assert "FORMAL_ONLY" in str(exc.value)


# --- CRITICAL no-false-positive cases ------------------------------------

def test_g04_lowercase_verilog_keywords_do_not_raise():
    """Legitimate lowercase Verilog if/else/case/casez/casex are legal RTL and
    MUST NOT trip the (case-sensitive, uppercase-only) TLA+ keyword banlist."""
    clean = """module foo(input clk, input sel, input [1:0] op, output reg q);
    always @(posedge clk) begin
        if (sel)
            q <= 1;
        else
            q <= 0;
        case (op)
            2'b00: q <= 0;
            default: q <= 1;
        endcase
        casez (op)
            2'b1?: q <= 1;
            default: q <= 0;
        endcase
        casex (op)
            2'b1x: q <= 1;
            default: q <= 0;
        endcase
    end
endmodule
"""
    verify_banlist(clean)  # must not raise


@pytest.mark.parametrize("ident", ["CASE_count", "IF_flag", "INcoming", "LETter"])
def test_g04_identifiers_containing_keyword_substrings_do_not_raise(ident):
    """Identifiers that merely CONTAIN a banned uppercase substring are not the
    keyword (word boundaries differ) and must not raise."""
    clean = f"module foo(input clk, output reg q);\n    reg {ident};\n    always @(posedge clk) {ident} <= 1;\nendmodule\n"
    verify_banlist(clean)  # must not raise


def test_g04_formal_only_inside_comment_does_not_raise():
    """FORMAL_ONLY inside a /* ... */ comment is exactly what the translator
    emits for dropped constructs; comment-stripping must precede the match so it
    does not false-positive."""
    clean = "module foo;\n    assign x = y /* FORMAL_ONLY */;\nendmodule\n"
    verify_banlist(clean)  # must not raise


def test_g04_keywords_inside_comments_do_not_raise():
    """Uppercase TLA+ keywords appearing in comments (e.g. an explanatory header)
    are stripped before matching and must not raise."""
    clean = """// IF THEN ELSE described here in a line comment
/* LET ... IN and CASE mentioned in a block comment */
module foo(input clk, output reg q);
    always @(posedge clk) q <= 1;
endmodule
"""
    verify_banlist(clean)  # must not raise


# ===========================================================================
# G05 — multi-driver conflict (same variable in comb AND clocked)
# ===========================================================================

def _multidriver_rtl_tla() -> str:
    """Hand-built RTL-style TLA+ that drives `shared` from BOTH CombinationalLogic
    (continuous assign) AND UpdatePipeline (clocked <=) — an illegal double-driver
    Compiler 2 must refuse."""
    return r"""
---- MODULE Conflict ----
EXTENDS Integers

VARIABLES
    clk, reset,
    shared,
    other

CombinationalLogic ==
    /\ shared' = other

UpdatePipeline ==
    /\ clk' = 1 - clk
    /\ IF reset = 1 THEN
          /\ shared' = 0
       ELSE
          /\ shared' = shared + 1

Next == /\ CombinationalLogic /\ UpdatePipeline
====
"""


def _multidriver_engine_spec() -> dict:
    """Engine-spec form of the same conflict: `shared` is assigned by a
    non-clocked (combinational) action and by clocked actions."""
    return {
        "variables": [
            {"name": "shared", "type": "Nat", "width": 1, "abstract": False,
             "reset_value": "0", "clocked": True},
            {"name": "other", "type": "Nat", "width": 1, "abstract": False,
             "reset_value": "0", "clocked": False},
        ],
        "actions": [
            {"name": "Comb", "guard": "TRUE", "clocked": False,
             "updates": [{"variable": "shared", "expression": "other"}]},
            {"name": "Reset", "guard": "reset = 1", "clocked": True,
             "updates": [{"variable": "shared", "expression": "0"}]},
            {"name": "Tick", "guard": "TRUE", "clocked": True,
             "updates": [{"variable": "shared", "expression": "shared + 1"}]},
        ],
        "reset_action": "Reset",
        "init": "shared = 0",
        "invariants": [],
    }


def test_g05_multidriver_raises_via_raw_tla():
    """Same variable driven by comb + clocked block -> MultiDriverError, naming
    the conflicting variable."""
    with pytest.raises(MultiDriverError) as exc:
        compile_tla_to_verilog(_multidriver_rtl_tla(), "conflict")
    assert "shared" in str(exc.value)


def test_g05_multidriver_raises_via_engine_spec_bridge():
    """The same conflict routed through the real engine_spec_to_rtl_tla bridge
    must also raise MultiDriverError."""
    rtl = engine_spec_to_rtl_tla(_multidriver_engine_spec(), "conflict")
    with pytest.raises(MultiDriverError) as exc:
        compile_tla_to_verilog(rtl, "conflict")
    assert "shared" in str(exc.value)


def test_g05_no_double_driver_verilog_emitted():
    """The conflict must be caught BEFORE emit: no Verilog string is returned,
    so a double-driver (assign + procedural <=) on `shared` never escapes the
    compiler. We assert the raise happens and that the compile call yields no
    usable Verilog (the exception is the gate)."""
    emitted = None
    try:
        emitted = compile_tla_to_verilog(_multidriver_rtl_tla(), "conflict")
    except MultiDriverError:
        pass
    assert emitted is None, (
        "double-driver Verilog was emitted instead of raising MultiDriverError:\n"
        f"{emitted}"
    )


def test_g05_multidriver_conflict_list_is_sorted_deterministic():
    """When multiple variables conflict, the error lists them sorted (so the
    message is deterministic regardless of VARIABLES order)."""
    tla = r"""
---- MODULE Conflict2 ----
EXTENDS Integers

VARIABLES
    clk, reset,
    zeta, alpha,
    other

CombinationalLogic ==
    /\ zeta' = other
    /\ alpha' = other

UpdatePipeline ==
    /\ clk' = 1 - clk
    /\ IF reset = 1 THEN
          /\ zeta' = 0
          /\ alpha' = 0
       ELSE
          /\ zeta' = zeta + 1
          /\ alpha' = alpha + 1
====
"""
    with pytest.raises(MultiDriverError) as exc:
        compile_tla_to_verilog(tla, "conflict2")
    msg = str(exc.value)
    # Sorted order: alpha before zeta, regardless of declaration order.
    assert msg.index("alpha") < msg.index("zeta"), (
        f"conflict list not sorted deterministically: {msg!r}"
    )


# ===========================================================================
# G12 — nested IF-THEN-ELSE -> nested (fully-parenthesized) ternary
# ===========================================================================

def test_g12_nested_if_translates_to_nested_ternary_expr():
    """RTLTLACompiler.translate_expr renders the bridge's nested-IF form into a
    fully-parenthesized nested ternary. Assert on structure / token order, not an
    unparenthesized exact string (Compiler 2 parenthesizes every operand)."""
    out = RTLTLACompiler("").translate_expr(
        "IF g1 THEN a ELSE IF g2 THEN b ELSE c"
    )
    # Two ternaries, nested: g1 selects a, else (g2 selects b, else c).
    assert out.count("?") == 2, f"expected two ternaries: {out!r}"
    assert out.count(":") == 2, f"expected two ternary colons: {out!r}"
    # No TLA+ keyword survives the translation.
    for kw in ("IF", "THEN", "ELSE"):
        assert kw not in out, f"leaked {kw!r} in nested ternary: {out!r}"
    # Operand order is preserved: g1 ? a ... g2 ? b ... c.
    positions = [out.index(t) for t in ("g1", "a", "g2", "b", "c")]
    assert positions == sorted(positions), (
        f"nested-ternary operand order not preserved: {out!r}"
    )
    # The else-branch of the first ternary is itself a ternary (nesting), so the
    # second '?' falls between the first ':' and the final operand 'c'.
    first_colon = out.index(":")
    assert out.index("g2") > first_colon, (
        f"second ternary not nested in the else-branch: {out!r}"
    )


def test_g12_nested_if_renders_in_emitted_verilog():
    """End-to-end through compile_tla_to_verilog: a clocked register whose
    non-reset next-state is a nested IF must emit a nested ternary in the
    clocked block (both branches survive — not collapsed to first-wins)."""
    tla = r"""
---- MODULE Nested ----
EXTENDS Integers

VARIABLES
    clk, reset,
    g1, g2, a, b, cc,
    r_out

UpdatePipeline ==
    /\ clk' = 1 - clk
    /\ IF reset = 1 THEN
          /\ r_out' = 0
       ELSE
          /\ r_out' = IF g1 THEN a ELSE IF g2 THEN b ELSE cc
====
"""
    verilog = compile_tla_to_verilog(tla, "nested")
    # Find the non-reset clocked assignment to r_out.
    assign_lines = [
        ln.strip() for ln in verilog.splitlines()
        if "r_out <=" in ln and "0;" not in ln
    ]
    assert assign_lines, f"no non-reset r_out assignment found:\n{verilog}"
    line = assign_lines[0]
    assert line.count("?") == 2 and line.count(":") == 2, (
        f"nested ternary collapsed (branch lost):\n{line}"
    )
    positions = [line.index(t) for t in ("g1", "a", "g2", "b", "cc")]
    assert positions == sorted(positions), (
        f"nested-ternary operand order not preserved:\n{line}"
    )


# ===========================================================================
# Lint smoke (tool-guarded): representative Compiler-2 output elaborates clean
# ===========================================================================

_HAVE_VERILATOR = shutil.which("verilator") is not None
_HAVE_IVERILOG = shutil.which("iverilog") is not None


def _lint(verilog_src: str) -> tuple[int, str]:
    """Lint verilog_src with whichever tool is available; return (rc, output)."""
    with tempfile.NamedTemporaryFile(suffix=".v", mode="w", delete=False) as f:
        f.write(verilog_src)
        fname = f.name
    try:
        if _HAVE_VERILATOR:
            r = subprocess.run(
                ["verilator", "--lint-only", fname],
                capture_output=True, text=True,
            )
            return r.returncode, "[verilator] " + r.stdout + r.stderr
        r = subprocess.run(
            ["iverilog", "-Wall", "-t", "null", fname],
            capture_output=True, text=True,
        )
        return r.returncode, "[iverilog] " + r.stdout + r.stderr
    finally:
        os.unlink(fname)


@pytest.mark.skipif(
    not (_HAVE_VERILATOR or _HAVE_IVERILOG),
    reason="neither verilator nor iverilog on PATH",
)
def test_compiler2_sample_elaborates_clean_no_multidriven():
    """A representative Compiler-2 output (the SAMPLE_TLA pipeline) elaborates
    clean: rc==0 and no MULTIDRIVEN/BLKANDNBLK/WIDTHTRUNC in the linter output."""
    verilog = compile_tla_to_verilog(SAMPLE_TLA, "pipeline_processor")
    rc, out = _lint(verilog)
    for warn in ("MULTIDRIVEN", "BLKANDNBLK", "WIDTHTRUNC"):
        assert warn not in out, f"{warn} present:\n{out}\n\nVerilog:\n{verilog}"
    assert rc == 0, f"sample lint failed:\n{out}\n\nVerilog:\n{verilog}"


@pytest.mark.skipif(
    not (_HAVE_VERILATOR or _HAVE_IVERILOG),
    reason="neither verilator nor iverilog on PATH",
)
def test_compiler2_width2_counter_elaborates_clean():
    """A width-2 counter routed through the real bridge elaborates clean with no
    width/driver warnings — exercises a sized output reg and the nested-ternary
    wrap expression together."""
    engine_spec = {
        "variables": [
            {"name": "count", "type": "Nat", "width": 2, "abstract": False,
             "reset_value": "0", "clocked": True},
        ],
        "actions": [
            {"name": "Reset", "guard": "reset = 1", "clocked": True,
             "updates": [{"variable": "count", "expression": "0"}]},
            {"name": "Tick", "guard": "TRUE", "clocked": True,
             "updates": [{"variable": "count",
                          "expression": "IF count = 3 THEN 0 ELSE count + 1"}]},
        ],
        "reset_action": "Reset",
        "init": "count = 0",
        "invariants": [],
    }
    rtl = engine_spec_to_rtl_tla(engine_spec, "counter")
    verilog = compile_tla_to_verilog(rtl, "counter")
    rc, out = _lint(verilog)
    for warn in ("MULTIDRIVEN", "BLKANDNBLK", "WIDTHTRUNC"):
        assert warn not in out, f"{warn} present:\n{out}\n\nVerilog:\n{verilog}"
    assert rc == 0, f"width-2 counter lint failed:\n{out}\n\nVerilog:\n{verilog}"


# ===========================================================================
# CLI self-test (mirrors tests/test_compilers.py __main__ harness)
# ===========================================================================

if __name__ == "__main__":
    import inspect

    fns = [
        obj for name, obj in sorted(globals().items())
        if name.startswith("test_") and inspect.isfunction(obj)
    ]
    passed = failed = 0
    for fn in fns:
        sig = inspect.signature(fn)
        # Expand simple @parametrize cases for the CLI harness.
        marks = getattr(fn, "pytestmark", [])
        param_sets = []
        for m in marks:
            if m.name == "parametrize":
                argname, values = m.args[0], m.args[1]
                param_sets = [(argname, v) for v in values]
        skip = any(m.name == "skipif" and m.args and m.args[0] for m in marks)
        try:
            if skip:
                print(f"  SKIP  {fn.__name__}")
                continue
            if param_sets:
                for argname, v in param_sets:
                    fn(**{argname: v})
            elif len(sig.parameters) == 0:
                fn()
            else:
                # Unparametrized fn with params we can't supply — skip safely.
                print(f"  SKIP  {fn.__name__} (needs args)")
                continue
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {fn.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
