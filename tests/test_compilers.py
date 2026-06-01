"""
Tests for Compiler 1 (JSON(TLA) -> TLA+/.cfg) and
Compiler 2 (RTL-style TLA+ -> Verilog-2001).

Run with:  python3.11 -m pytest tests/test_compilers.py -v
Or:        python3.11 tests/test_compilers.py
"""

import subprocess
import sys
import tempfile
import os

# Make pipeline importable from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.compilers.compiler2 import (
    BanlistViolation,
    RTLTLACompiler,
    SAMPLE_TLA,
    _strip_comments,
    compile_tla_to_verilog,
    verify_banlist,
)
from pipeline.compilers.compiler1 import (
    CompilerError,
    compile,
    _make_counter_spec,
)


# ---------------------------------------------------------------------------
# Compiler 1 tests
# ---------------------------------------------------------------------------


def test_compiler1_produces_tla_and_cfg():
    spec = _make_counter_spec()
    tla, cfg = compile(spec)
    assert "MODULE TwoBitCounter" in tla
    assert "VARIABLES" in tla
    assert "Init ==" in tla
    assert "Next ==" in tla
    assert "Spec ==" in tla
    assert "INIT Init" in cfg
    assert "NEXT Next" in cfg
    assert cfg.strip() != ""


def test_compiler1_invariants_in_cfg():
    spec = _make_counter_spec()
    tla, cfg = compile(spec)
    assert "INVARIANT" in cfg


def test_compiler1_type_invariant_when_width_present():
    spec = _make_counter_spec()
    tla, cfg = compile(spec)
    # count is Nat width=2 -> 0..3 range constraint
    assert "TypeInvariant" in tla
    assert "count \\in 0..3" in tla
    # TypeInvariant also appears in .cfg
    assert "INVARIANT TypeInvariant" in cfg


def test_compiler1_action_expressions_translated():
    spec = _make_counter_spec()
    tla, cfg = compile(spec)
    # The invariant "count >= 0 AND count <= 3" should have AND -> /\
    assert "/\\" in tla


def test_compiler1_unchanged_clause():
    spec = _make_counter_spec()
    tla, cfg = compile(spec)
    # Each transition that doesn't update all vars should emit UNCHANGED
    # (in this spec, Tick and Reset both update all vars, so no UNCHANGED needed;
    # just verify the spec is valid TLA+ structure with UNCHANGED not erroneously present)
    # Since both transitions update count AND clk, UNCHANGED list is empty -> not emitted
    assert "UNCHANGED <<>>" not in tla


def test_compiler1_deterministic():
    spec = _make_counter_spec()
    tla1, cfg1 = compile(spec)
    tla2, cfg2 = compile(spec)
    assert tla1 == tla2
    assert cfg1 == cfg2


def test_compiler1_raw_tla_passthrough():
    """If raw_tla is populated, compiler passes it through unchanged."""
    spec = _make_counter_spec()
    raw = "---- MODULE FakeModule ----\nVARIABLES x\nInit == x = 0\nNext == x' = 1\n===="
    spec.raw_tla = raw
    tla, cfg = compile(spec)
    assert tla == raw
    # cfg still generated from structured fields
    assert "INIT Init" in cfg


def test_compiler1_empty_variables_raises():
    spec = _make_counter_spec()
    spec.variables = {}
    try:
        compile(spec)
        assert False, "Should have raised CompilerError"
    except CompilerError as e:
        assert "variables" in str(e).lower()


def test_compiler1_empty_initial_raises():
    spec = _make_counter_spec()
    spec.initial = {}
    try:
        compile(spec)
        assert False, "Should have raised CompilerError"
    except CompilerError as e:
        assert "initial" in str(e).lower()


def test_compiler1_unknown_variable_in_initial_raises():
    spec = _make_counter_spec()
    spec.initial["nonexistent"] = "0"
    try:
        compile(spec)
        assert False, "Should have raised CompilerError"
    except CompilerError as e:
        assert "nonexistent" in str(e)


def test_compiler1_invalid_module_name_raises():
    spec = _make_counter_spec()
    spec.module_name = "123invalid"
    try:
        compile(spec)
        assert False, "Should have raised CompilerError"
    except CompilerError as e:
        assert "module_name" in str(e)


# ---------------------------------------------------------------------------
# Compiler 2: banlist verifier tests
# ---------------------------------------------------------------------------


def test_banlist_clean_verilog_passes():
    """The sample output should pass the banlist verifier."""
    verilog = compile_tla_to_verilog(SAMPLE_TLA, "pipeline_processor")
    # If this raises, the test fails
    verify_banlist(verilog)


def test_banlist_logic_keyword_caught():
    bad = "module foo; logic x; endmodule"
    try:
        verify_banlist(bad)
        assert False, "Should have raised BanlistViolation"
    except BanlistViolation as e:
        assert "logic" in str(e)


def test_banlist_always_ff_caught():
    bad = "module foo(input clk); always_ff @(posedge clk) begin end endmodule"
    try:
        verify_banlist(bad)
        assert False, "Should have raised BanlistViolation"
    except BanlistViolation as e:
        assert "always_ff" in str(e)


def test_banlist_always_comb_caught():
    bad = "module foo; always_comb begin end endmodule"
    try:
        verify_banlist(bad)
        assert False, "Should have raised BanlistViolation"
    except BanlistViolation as e:
        assert "always_comb" in str(e)


def test_banlist_initial_caught():
    bad = "module foo; initial begin end endmodule"
    try:
        verify_banlist(bad)
        assert False, "Should have raised BanlistViolation"
    except BanlistViolation as e:
        assert "initial" in str(e)


def test_banlist_hash_delay_caught():
    bad = "module foo; always @(*) begin #10 x = 1; end endmodule"
    try:
        verify_banlist(bad)
        assert False, "Should have raised BanlistViolation"
    except BanlistViolation as e:
        assert "#" in str(e)


def test_banlist_interface_caught():
    bad = "interface my_if; endinterface"
    try:
        verify_banlist(bad)
        assert False, "Should have raised BanlistViolation"
    except BanlistViolation as e:
        assert "interface" in str(e)


def test_banlist_typedef_caught():
    # Use typedef without logic so we specifically exercise the typedef banlist entry
    bad = "typedef [7:0] byte_t;"
    try:
        verify_banlist(bad)
        assert False, "Should have raised BanlistViolation"
    except BanlistViolation as e:
        assert "typedef" in str(e)


def test_banlist_comment_with_banned_word_no_false_positive():
    """Banned keywords in comments must NOT trigger the verifier."""
    verilog_with_comments = """
// logic in a line comment -- should not trigger
/* always_ff: used in block comment */
module foo(input clk, output reg q);
    // always_comb would be wrong here
    always @(posedge clk) begin
        q <= 1;
    end
endmodule
"""
    verify_banlist(verilog_with_comments)  # must not raise


def test_strip_comments_preserves_newlines():
    src = "module foo; // logic\n/* always_ff */\nassign x = 1;\n"
    stripped = _strip_comments(src)
    assert src.count("\n") == stripped.count("\n")
    assert "logic" not in stripped
    assert "always_ff" not in stripped
    assert "assign" in stripped


def test_banlist_no_false_positive_on_docstring_header():
    """The compiler2 module-level docstring mentions banned keywords in explanatory
    text.  Importing it and running the verifier on real output must not raise."""
    verilog = compile_tla_to_verilog(SAMPLE_TLA, "pipeline_processor")
    # verify_banlist runs on *emitted* code, not on module source, so this is
    # covered by test_banlist_clean_verilog_passes.  This test makes the intent
    # explicit.
    verify_banlist(verilog)


# ---------------------------------------------------------------------------
# Compiler 2: emitter correctness tests
# ---------------------------------------------------------------------------


def test_compiler2_produces_verilog_2001_always_posedge():
    verilog = compile_tla_to_verilog(SAMPLE_TLA, "pipeline_processor")
    assert "always @(posedge clk)" in verilog


def test_compiler2_no_always_star_for_comb():
    """Sample spec has CombinationalLogic -> assign, no always @(*) expected."""
    verilog = compile_tla_to_verilog(SAMPLE_TLA, "pipeline_processor")
    assert "always @(*)" not in verilog
    assert "assign" in verilog


def test_compiler2_internal_regs_declared():
    verilog = compile_tla_to_verilog(SAMPLE_TLA, "pipeline_processor")
    assert "reg  r_stg1_valid" in verilog
    assert "reg  r_stg2_acc" in verilog


def test_compiler2_hw_vars_dropped():
    verilog = compile_tla_to_verilog(SAMPLE_TLA, "pipeline_processor")
    assert "hw_in_history" not in verilog
    assert "hw_out_history" not in verilog


def test_compiler2_output_ports_correct_kind():
    verilog = compile_tla_to_verilog(SAMPLE_TLA, "pipeline_processor")
    # CombinationalLogic drives in_ready, out_valid, out_data -> output (wire)
    assert "output in_ready" in verilog
    assert "output out_valid" in verilog
    assert "output out_data" in verilog
    # They must NOT be declared as output reg (wire, driven by assign)
    assert "output reg in_ready" not in verilog


def test_compiler2_input_ports_correct():
    verilog = compile_tla_to_verilog(SAMPLE_TLA, "pipeline_processor")
    assert "input  clk" in verilog
    assert "input  reset" in verilog
    assert "input  in_valid" in verilog


def test_compiler2_if_then_else_translated_to_ternary():
    verilog = compile_tla_to_verilog(SAMPLE_TLA, "pipeline_processor")
    assert "?" in verilog
    assert ":" in verilog


def test_compiler2_reset_branch_emitted():
    verilog = compile_tla_to_verilog(SAMPLE_TLA, "pipeline_processor")
    assert "if (reset) begin" in verilog
    assert "end else begin" in verilog


def test_compiler2_deterministic():
    v1 = compile_tla_to_verilog(SAMPLE_TLA, "pipeline_processor")
    v2 = compile_tla_to_verilog(SAMPLE_TLA, "pipeline_processor")
    assert v1 == v2


def test_compiler2_module_name_respected():
    verilog = compile_tla_to_verilog(SAMPLE_TLA, "my_custom_core")
    assert "module my_custom_core" in verilog


def test_compiler2_no_logic_keyword_in_output():
    verilog = compile_tla_to_verilog(SAMPLE_TLA, "pipeline_processor")
    import re
    stripped = verilog
    # Remove comments before checking
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
    stripped = re.sub(r"//[^\n]*", "", stripped)
    assert not re.search(r"\blogic\b", stripped), "Found 'logic' in emitted code"


# ---------------------------------------------------------------------------
# Compiler 2: lint tests (iverilog / verilator)
# ---------------------------------------------------------------------------


def _run_linter(verilog_src: str) -> tuple[int, str]:
    """Write verilog_src to a temp file, lint it, return (exit_code, output)."""
    with tempfile.NamedTemporaryFile(suffix=".v", mode="w", delete=False) as f:
        f.write(verilog_src)
        fname = f.name
    try:
        # Try verilator first
        r = subprocess.run(
            ["verilator", "--lint-only", fname],
            capture_output=True, text=True
        )
        tool = "verilator"
        combined = r.stdout + r.stderr
        if r.returncode != 0:
            return r.returncode, f"[{tool}] {combined}"
        # Also try iverilog as a second pass
        r2 = subprocess.run(
            ["iverilog", "-Wall", "-t", "null", fname],
            capture_output=True, text=True
        )
        tool2 = "iverilog"
        combined2 = r2.stdout + r2.stderr
        if r2.returncode != 0:
            return r2.returncode, f"[{tool2}] {combined2}"
        return 0, f"[{tool}+{tool2}] CLEAN"
    finally:
        os.unlink(fname)


def test_compiler2_sample_lint_clean():
    verilog = compile_tla_to_verilog(SAMPLE_TLA, "pipeline_processor")
    rc, out = _run_linter(verilog)
    assert rc == 0, f"Lint failed:\n{out}"


def test_compiler2_counter_tla_lint_clean():
    """Generate Verilog from a minimal 2-bit counter RTL TLA+ spec and lint it.

    Uses IF-THEN-ELSE for the counter increment to avoid modulo-width issues
    (verilator warns on width truncation when bare 'reg' has no declared width
    and a 32-bit integer expression is assigned to it -- a known limitation of
    the no-bit-width emitter documented in docs/compiler1.md).
    """
    counter_tla = r"""
---- MODULE TwoBitCounter ----
EXTENDS Integers

VARIABLES
    clk, reset,
    r_count,
    out_valid

CombinationalLogic ==
    /\ out_valid' = r_count

UpdatePipeline ==
    /\ clk' = 1 - clk
    /\ IF reset = 1 THEN
          /\ r_count' = 0
       ELSE
          /\ r_count' = IF r_count = 1 THEN 0 ELSE 1

Next == /\ CombinationalLogic /\ UpdatePipeline
Spec == Init /\ [][Next]_vars
====
"""
    verilog = compile_tla_to_verilog(counter_tla, "two_bit_counter")
    rc, out = _run_linter(verilog)
    assert rc == 0, f"Counter lint failed:\n{out}\n\nVerilog:\n{verilog}"


# ---------------------------------------------------------------------------
# CLI self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_compiler1_produces_tla_and_cfg,
        test_compiler1_invariants_in_cfg,
        test_compiler1_type_invariant_when_width_present,
        test_compiler1_action_expressions_translated,
        test_compiler1_unchanged_clause,
        test_compiler1_deterministic,
        test_compiler1_raw_tla_passthrough,
        test_compiler1_empty_variables_raises,
        test_compiler1_empty_initial_raises,
        test_compiler1_unknown_variable_in_initial_raises,
        test_compiler1_invalid_module_name_raises,
        test_banlist_clean_verilog_passes,
        test_banlist_logic_keyword_caught,
        test_banlist_always_ff_caught,
        test_banlist_always_comb_caught,
        test_banlist_initial_caught,
        test_banlist_hash_delay_caught,
        test_banlist_interface_caught,
        test_banlist_typedef_caught,
        test_banlist_comment_with_banned_word_no_false_positive,
        test_strip_comments_preserves_newlines,
        test_banlist_no_false_positive_on_docstring_header,
        test_compiler2_produces_verilog_2001_always_posedge,
        test_compiler2_no_always_star_for_comb,
        test_compiler2_internal_regs_declared,
        test_compiler2_hw_vars_dropped,
        test_compiler2_output_ports_correct_kind,
        test_compiler2_input_ports_correct,
        test_compiler2_if_then_else_translated_to_ternary,
        test_compiler2_reset_branch_emitted,
        test_compiler2_deterministic,
        test_compiler2_module_name_respected,
        test_compiler2_no_logic_keyword_in_output,
        test_compiler2_sample_lint_clean,
        test_compiler2_counter_tla_lint_clean,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
