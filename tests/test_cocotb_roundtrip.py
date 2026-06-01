"""Round-trip verification for the deterministic cocotb generator + runner.

Tests:
  1. Generator produces valid Python for a 2-bit counter SpecSummary.
  2. Runner returns {"status": "pass"} against a known-good counter.v.
  3. Runner returns a structured fail (phase="test") for a mutant counter.v.
  4. Runner returns a structured fail (phase="build") for invalid Verilog.

Run with:
    python3.11 tests/test_cocotb_roundtrip.py
"""

import ast
import sys
import tempfile
from pathlib import Path

# Allow running from the project root without installing the package.
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.cocotb.generator import generate_testbench
from pipeline.cocotb.runner import run_testbench
from pipeline.schemas.summary_schema import SpecSummary, TestVector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_counter_summary() -> SpecSummary:
    """2-bit free-running counter with active-high synchronous reset.

    Reset sequence: assert rst for one cycle (q=0), deassert.
    Vectors probe the first three post-reset counts: 1, 2, 3.

    Note: the generator inserts a post-reset cycle so the DUT is at q=1
    by the time the first test vector drives inputs.  Each vector:
      - drives inputs (en=1),
      - awaits one RisingEdge + 1 ns Timer,
      - asserts the expected output.
    """
    return SpecSummary(
        module_name="counter",
        description="2-bit synchronous counter with active-high reset and enable",
        ports=[],                         # not used by generator
        test_vectors=[
            # After reset the DUT is at q=0.  Each vector clocks once.
            TestVector(inputs={"en": 1}, expected={"q": 1}),   # 0->1
            TestVector(inputs={"en": 1}, expected={"q": 2}),   # 1->2
            TestVector(inputs={"en": 1}, expected={"q": 3}),   # 2->3
        ],
        reset_port="rst",
        reset_active_low=False,           # active-high reset
    )


# ---------------------------------------------------------------------------
# Known-good RTL
# ---------------------------------------------------------------------------

_GOOD_COUNTER_V = """\
`timescale 1ns/1ps
module counter (
    input  wire clk,
    input  wire rst,
    input  wire en,
    output reg  [1:0] q
);
    always @(posedge clk) begin
        if (rst)
            q <= 2'b00;
        else if (en)
            q <= q + 1;
    end
endmodule
"""

# Mutant: counter *decrements* instead of incrementing — vectors will fail.
_BAD_COUNTER_V = """\
`timescale 1ns/1ps
module counter (
    input  wire clk,
    input  wire rst,
    input  wire en,
    output reg  [1:0] q
);
    always @(posedge clk) begin
        if (rst)
            q <= 2'b00;
        else if (en)
            q <= q - 1;   // BUG: should be +1
    end
endmodule
"""

# Invalid Verilog — will not compile.
_INVALID_COUNTER_V = """\
`timescale 1ns/1ps
module counter (
    input  wire clk
    // missing semicolons and port list
    this is not verilog at all
);
endmodule
"""


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def _run_tests() -> None:
    summary = _make_counter_summary()

    with tempfile.TemporaryDirectory(prefix="cocotb_roundtrip_") as tmpdir:
        tmp = Path(tmpdir)

        # ----------------------------------------------------------------
        # 1. Generator produces syntactically valid Python.
        # ----------------------------------------------------------------
        tb_path = tmp / "test_counter.py"
        generate_testbench(summary, tb_path)

        assert tb_path.exists(), "Generator did not write testbench file"
        src = tb_path.read_text()
        try:
            ast.parse(src)
        except SyntaxError as e:
            print("FAIL  test 1 — generated testbench has syntax error")
            print(src)
            raise

        print("PASS  test 1 — generated testbench parses as valid Python")
        print("--- generated testbench ---")
        print(src)
        print("---------------------------")

        # ----------------------------------------------------------------
        # 2. Runner passes on known-good RTL.
        # ----------------------------------------------------------------
        good_v = tmp / "counter_good.v"
        good_v.write_text(_GOOD_COUNTER_V)

        # Each test runs in its own subdirectory so sim_build artifacts don't collide.
        pass_dir = tmp / "pass_run"
        pass_dir.mkdir()
        tb_pass = pass_dir / "test_counter.py"
        generate_testbench(summary, tb_pass)

        result_pass = run_testbench(tb_pass, good_v, "counter")
        print(f"\nPASS-run result: {result_pass}")

        assert result_pass == {"status": "pass"}, (
            f"Expected pass on good RTL, got: {result_pass}"
        )
        print("PASS  test 2 — runner returns pass on good RTL")

        # ----------------------------------------------------------------
        # 3. Runner returns structured fail (phase=test) on mutant RTL.
        # ----------------------------------------------------------------
        bad_v = tmp / "counter_bad.v"
        bad_v.write_text(_BAD_COUNTER_V)

        fail_dir = tmp / "fail_run"
        fail_dir.mkdir()
        tb_fail = fail_dir / "test_counter.py"
        generate_testbench(summary, tb_fail)

        result_fail = run_testbench(tb_fail, bad_v, "counter")
        print(f"\nFAIL-run result: {result_fail}")

        assert result_fail["status"] == "fail", (
            f"Expected fail on mutant RTL, got status={result_fail['status']}"
        )
        assert result_fail["phase"] == "test", (
            f"Expected phase=test for assertion failure, got phase={result_fail.get('phase')}"
        )
        assert len(result_fail["failed_vectors"]) > 0, (
            "Expected at least one entry in failed_vectors"
        )
        fv = result_fail["failed_vectors"][0]
        assert "error_type" in fv and "error_msg" in fv and "test" in fv, (
            f"failed_vectors entry missing required keys: {fv}"
        )
        print("PASS  test 3 — runner returns structured phase=test fail on mutant RTL")

        # ----------------------------------------------------------------
        # 4. Runner returns structured fail (phase=build) on invalid Verilog.
        # ----------------------------------------------------------------
        invalid_v = tmp / "counter_invalid.v"
        invalid_v.write_text(_INVALID_COUNTER_V)

        build_fail_dir = tmp / "buildfail_run"
        build_fail_dir.mkdir()
        tb_build = build_fail_dir / "test_counter.py"
        generate_testbench(summary, tb_build)

        result_build = run_testbench(tb_build, invalid_v, "counter")
        print(f"\nBUILD-fail result: {result_build}")

        assert result_build["status"] == "fail", (
            f"Expected fail on invalid Verilog, got status={result_build['status']}"
        )
        assert result_build["phase"] == "build", (
            f"Expected phase=build for compile error, got phase={result_build.get('phase')}"
        )
        assert "error" in result_build and result_build["error"], (
            "Expected non-empty error field on build failure"
        )
        assert result_build["failed_vectors"] == [], (
            "Expected empty failed_vectors on build failure"
        )
        print("PASS  test 4 — runner returns structured phase=build fail on invalid Verilog")

    print("\nAll 4 round-trip tests passed.")


if __name__ == "__main__":
    _run_tests()
