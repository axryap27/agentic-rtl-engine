from pathlib import Path  # Path is an object for file paths; Path("a/b.py").write_text(...) writes a file
from pipeline.schemas.summary_schema import SpecSummary

# Fixed boilerplate for every testbench.
# {module_name}, {reset_block}, {test_vectors} are placeholders filled in by generate_testbench().
_TEMPLATE = """\
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge


@cocotb.test()
async def test_{module_name}(dut):
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    await RisingEdge(dut.clk)
{reset_block}{test_vectors}
"""


def _reset_block(summary: SpecSummary) -> str:
    # No reset port defined in the spec — skip the reset block entirely.
    if summary.reset_port is None:
        return ""

    # Active-low: assert=0 deassert=1. Active-high: assert=1 deassert=0.
    assert_val = 0 if summary.reset_active_low else 1
    deassert_val = 1 if summary.reset_active_low else 0

    # Pulse reset for one clock cycle before running test vectors.
    return (
        f"    dut.{summary.reset_port}.value = {assert_val}\n"
        f"    await RisingEdge(dut.clk)\n"
        f"    dut.{summary.reset_port}.value = {deassert_val}\n"
        f"    await RisingEdge(dut.clk)\n"
    )


def _test_vectors_block(summary: SpecSummary) -> str:
    lines = []
    for i, tv in enumerate(summary.test_vectors):
        lines.append(f"    # vector {i}")

        # Drive all inputs onto the DUT ports.
        for port, val in tv.inputs.items():
            lines.append(f"    dut.{port}.value = {val}")

        # Tick the clock so the DUT processes the inputs.
        lines.append("    await RisingEdge(dut.clk)")

        # Check each expected output. The f-string inside the assert is part of
        # the generated code (not evaluated now) — it runs when the testbench executes.
        for port, val in tv.expected.items():
            lines.append(
                f'    assert dut.{port}.value == {val},'
                f' f"vector {i}: expected {port}={val}, got {{dut.{port}.value}}"'
            )

    return "\n".join(lines) + "\n"


def generate_testbench(summary: SpecSummary, output_path: Path) -> Path:
    # Fill the template with the reset block and test vectors, then write to disk.
    content = _TEMPLATE.format(
        module_name=summary.module_name,
        reset_block=_reset_block(summary),
        test_vectors=_test_vectors_block(summary),
    )
    output_path.write_text(content)
    return output_path
