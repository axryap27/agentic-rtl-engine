import subprocess
import sys
from pathlib import Path

# runner.py: simulates the generated RTL against cocotb testbench using iverilog

def run_testbench(testbench_path: Path, rtl_path: Path, module_name: str) -> dict:
    """
    Run a cocotb testbench against an RTL file using Icarus Verilog.

    Returns {"status": "pass"} on success.
    Returns {"status": "fail", "error": "..."} on simulation failure.
    """
    # cocotb's runner API handles build (compiling the RTL) and test (running the sim) separately.
    # We invoke it in a subprocess so failures don't crash the parent pipeline process.
    runner_script = (
        "from cocotb.runner import get_runner; "
        f"r = get_runner('icarus'); "
        # build: compile the RTL into a simulation binary
        f"r.build("
        f"  sources=['{rtl_path}'],"
        f"  hdl_toplevel='{module_name}',"
        f"  build_dir='{testbench_path.parent / 'sim_build'}',"
        f"  always=True"  # always recompile — RTL may have changed since last run
        f"); "
        # test: run the compiled sim with the cocotb testbench
        f"r.test("
        f"  hdl_toplevel='{module_name}',"
        f"  test_module='{testbench_path.stem}',"  # testbench filename without .py
        f"  pythonpath=['{testbench_path.parent}']"  # so cocotb can import the testbench
        f")"
    )

    result = subprocess.run(
        [sys.executable, "-c", runner_script],
        capture_output=True,
        text=True,
        cwd=testbench_path.parent,
    )

    if result.returncode == 0:
        return {"status": "pass"}

    return {
        "status": "fail",
        "error": (result.stdout + result.stderr).strip(),
    }
