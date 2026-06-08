import json
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
    cocotb.start_soon(Clock(dut.{clock_port}, 10, unit="ns").start())
{input_init}    await RisingEdge(dut.{clock_port})
{reset_block}{test_vectors}"""


def _fmt_value(val: object) -> str:
    """Render a test-vector value as a Python literal for the testbench source.

    Test-vector values are `dict[str, Any]`, so a value may be an int, a bool, a
    string (cocotb don't-care `'x'`, a 4-state literal `'1z'`, or a hex string
    `'0xff'`), etc. Interpolating these raw via f-strings is the G14 fragility:
      - `'x'`  -> `dut.a.value = x`   (bare name -> NameError at sim runtime)
      - `'1z'` -> `dut.a.value = 1z`  (invalid token -> SyntaxError)
      - `'0xff'` -> `dut.a.value = 0xff` (str silently coerced to int 255)
      - `True` -> `dut.a.value = True` (never normalized to 1/0)

    Rules:
      - bool BEFORE int (bool is an int subclass): normalize to "1"/"0".
      - int: bare decimal literal.
      - str: `json.dumps` for a DOUBLE-quoted, valid-Python string literal
        (repr would yield single quotes, which the fidelity tests reject).
      - anything else: `repr` as a best-effort fallback.
    """
    if isinstance(val, bool):
        return str(int(val))
    if isinstance(val, int):
        return str(val)
    if isinstance(val, str):
        return json.dumps(val)
    return repr(val)


def _clock_port(summary: SpecSummary) -> str:
    """Derive the DUT clock-port name from the spec, defaulting to "clk".

    The clock is hardcoded as `dut.clk` in the legacy template; a design whose
    clock port is named `clock` (or anything else) would never be clocked. We
    scan input ports for a clock-like name. Preference order:
      1. an exact `clk` or `clock` input port,
      2. an input port whose name contains `clk`/`clock`,
      3. default to `"clk"` (so specs with ports=[] do not regress).
    """
    inputs = [p.name for p in summary.ports if p.direction == "input"]
    for name in inputs:
        if name in ("clk", "clock"):
            return name
    for name in inputs:
        low = name.lower()
        if "clk" in low or "clock" in low:
            return name
    return "clk"


def _input_init_block(summary: SpecSummary) -> str:
    """Drive every test-vector input to 0 at t=0, before the reset pulse.

    Leaving inputs undriven (X) until the first vector lets X propagate into any
    register whose next-state depends on an input at the reset-deassert edge —
    e.g. an enable-gated counter latches X and never recovers, even though the
    enable is correctly driven for every subsequent vector. Initialising inputs
    to a known 0 is standard testbench hygiene and makes such DUTs deterministic
    (memoryless outputs like a DFF or an ALU result are unaffected, since they
    are overwritten from the driven inputs each cycle).

    Input names are derived from the test vectors (always present) rather than
    summary.ports (often empty). clk and the reset port are excluded: the clock
    is free-running and reset is driven by the reset block.

    Only inputs that are *always* driven with a plain int are pre-initialised to
    0. A port driven with a string (cocotb don't-care `'x'`, a 4-state literal,
    or a hex string) or a bool has no meaningful integer zero — forcing `= 0`
    onto it would both be semantically wrong and clobber the literal the spec
    author intended. Such ports are left to their first vector drive.
    """
    int_names: set[str] = set()
    other_names: set[str] = set()
    for tv in summary.test_vectors:
        for name, val in tv.inputs.items():
            # bool is an int subclass; treat it as non-int so it is not 0-inited.
            if isinstance(val, bool) or not isinstance(val, int):
                other_names.add(name)
            else:
                int_names.add(name)
    names = int_names - other_names
    names.discard(_clock_port(summary))
    if summary.reset_port:
        names.discard(summary.reset_port)
    if not names:
        return ""
    return "".join(f"    dut.{n}.value = 0\n" for n in sorted(names))


def _reset_block(summary: SpecSummary) -> str:
    """Return indented cocotb reset-pulse lines, or empty string if no reset port."""
    # No reset port defined in the spec — skip the reset block entirely.
    if summary.reset_port is None:
        return ""

    # Active-low: assert=0 deassert=1. Active-high: assert=1 deassert=0.
    assert_val = 0 if summary.reset_active_low else 1
    deassert_val = 1 if summary.reset_active_low else 0

    clk = _clock_port(summary)

    # Pulse reset for one clock cycle, then let one more cycle pass so the
    # DUT fully exits reset before the first test vector drives inputs.
    return (
        f"    dut.{summary.reset_port}.value = {assert_val}\n"
        f"    await RisingEdge(dut.{clk})\n"
        f"    await Timer(1, unit=\"ns\")  # settle past delta\n"
        f"    dut.{summary.reset_port}.value = {deassert_val}\n"
        f"    await RisingEdge(dut.{clk})\n"
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
    # An empty vector list must NOT yield a vacuous pass: a @cocotb.test() body
    # with no asserts succeeds while verifying nothing. Emit a failing assert so
    # the runner surfaces "no vectors" rather than silently passing. (We do not
    # raise here — callers ast.parse() the returned source; an exception would
    # break generation instead of producing a self-failing testbench.)
    if not summary.test_vectors:
        return '    assert False, "no test vectors: nothing to verify"\n'

    clk = _clock_port(summary)

    lines = []
    for i, tv in enumerate(summary.test_vectors):
        lines.append(f"    # vector {i}")

        # Drive all inputs onto the DUT ports. _fmt_value quotes strings,
        # normalizes bools to 1/0, and leaves ints as bare decimals.
        # FIX RC2 (defensive): the clock port is owned by the free-running Clock
        # plus the single RisingEdge below (one tick per vector). A manual
        # per-vector clk assignment is racy (immediately overridden by the Clock)
        # and, if a spec toggles clk, falsely implies a half-rate advance. Skip
        # it so the 1-tick-per-vector contract is enforced even if Agent 1
        # backslides; reset and all data inputs are still driven.
        for port, val in tv.inputs.items():
            if port == clk:
                continue
            lines.append(f"    dut.{port}.value = {_fmt_value(val)}")

        # Tick the clock so the DUT processes the inputs.
        lines.append(f"    await RisingEdge(dut.{clk})")
        # Advance past delta so always-@(posedge) outputs are visible.
        lines.append('    await Timer(1, unit="ns")  # settle past delta')

        # Check each expected output. The f-string inside the assert is part of
        # the generated code (not evaluated now) — it runs when the testbench executes.
        # The normalized value is used in BOTH the comparison and the message text.
        for port, val in tv.expected.items():
            fval = _fmt_value(val)
            lines.append(
                f'    assert dut.{port}.value == {fval},'
                f' f"vector {i}: expected {port}={fval}, got {{dut.{port}.value}}"'
            )

    return "\n".join(lines) + "\n"


def generate_testbench(summary: SpecSummary, output_path: Path) -> Path:
    """Generate a deterministic cocotb testbench and write it to output_path.

    The testbench is fully determined by summary.test_vectors — no LLM involved.
    Returns output_path for convenience.
    """
    content = _TEMPLATE.format(
        module_name=summary.module_name,
        clock_port=_clock_port(summary),
        input_init=_input_init_block(summary),
        reset_block=_reset_block(summary),
        test_vectors=_test_vectors_block(summary),
    )
    output_path.write_text(content)
    return output_path
