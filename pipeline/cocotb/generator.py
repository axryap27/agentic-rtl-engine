from pathlib import Path  # Path is an object for file paths; Path("a/b.py").write_text(...) writes a file
from pipeline.schemas.summary_schema import SpecSummary

# generator.py: generates cocotb testbench from JSON(S) test vectors.
# NO LLM calls here — SpecSummary.test_vectors provides all structured data needed.

# Fixed boilerplate for every testbench.
# {module_name}, {reset_block}, {test_vectors} are placeholders filled in by generate_testbench().
#
# Cocotb 2.x notes:
#   - Clock uses `unit=` (not `units=`).
#   - After RisingEdge the simulator is still at the rising-edge delta; outputs from
#     `always @(posedge clk)` blocks are not yet visible. A 1 ns Timer lets the
#     sim advance past the delta cycle so registered outputs settle before assertion.
#     This is the standard cocotb 2.x idiom for registered-output DUTs.
_TEMPLATE = """\
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer


@cocotb.test()
async def test_{module_name}(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await RisingEdge(dut.clk)
{reset_block}{test_vectors}"""


def _reset_block(summary: SpecSummary) -> str:
    """Return indented cocotb reset-pulse lines, or empty string if no reset port."""
    # No reset port defined in the spec — skip the reset block entirely.
    if summary.reset_port is None:
        return ""

    # Active-low: assert=0 deassert=1. Active-high: assert=1 deassert=0.
    assert_val = 0 if summary.reset_active_low else 1
    deassert_val = 1 if summary.reset_active_low else 0

    # Pulse reset for one clock cycle, then let one more cycle pass so the
    # DUT fully exits reset before the first test vector drives inputs.
    return (
        f"    dut.{summary.reset_port}.value = {assert_val}\n"
        f"    await RisingEdge(dut.clk)\n"
        f"    await Timer(1, unit=\"ns\")  # settle past delta\n"
        f"    dut.{summary.reset_port}.value = {deassert_val}\n"
        f"    await RisingEdge(dut.clk)\n"
        f"    await Timer(1, unit=\"ns\")  # settle past delta\n"
    )


def _test_vectors_block(summary: SpecSummary) -> str:
    """Return indented cocotb lines that drive inputs and assert expected outputs.

    Each TestVector becomes:
      1. A comment identifying the vector index.
      2. One dut.<port>.value = <val> assignment per input.
      3. await RisingEdge(dut.clk) — clock the DUT.
      4. await Timer(1, unit="ns") — advance past delta so registered outputs settle.
      5. One assert per expected output, with a descriptive failure message.

    The 1 ns Timer is required in cocotb 2.x: RisingEdge wakes the coroutine at the
    rising-edge delta step, before always-@(posedge) blocks update their outputs.
    Without the Timer the assertions run against stale (pre-edge) register values.
    """
    lines = []
    for i, tv in enumerate(summary.test_vectors):
        lines.append(f"    # vector {i}")

        # Drive all inputs onto the DUT ports.
        for port, val in tv.inputs.items():
            lines.append(f"    dut.{port}.value = {val}")

        # Tick the clock so the DUT processes the inputs.
        lines.append("    await RisingEdge(dut.clk)")
        # Advance past delta so always-@(posedge) outputs are visible.
        lines.append('    await Timer(1, unit="ns")  # settle past delta')

        # Check each expected output. The f-string inside the assert is part of
        # the generated code (not evaluated now) — it runs when the testbench executes.
        for port, val in tv.expected.items():
            lines.append(
                f'    assert dut.{port}.value == {val},'
                f' f"vector {i}: expected {port}={val}, got {{dut.{port}.value}}"'
            )

    return "\n".join(lines) + "\n"


def generate_testbench(summary: SpecSummary, output_path: Path) -> Path:
    """Generate a deterministic cocotb testbench and write it to output_path.

    The testbench is fully determined by summary.test_vectors — no LLM involved.
    Returns output_path for convenience.
    """
    content = _TEMPLATE.format(
        module_name=summary.module_name,
        reset_block=_reset_block(summary),
        test_vectors=_test_vectors_block(summary),
    )
    output_path.write_text(content)
    return output_path
