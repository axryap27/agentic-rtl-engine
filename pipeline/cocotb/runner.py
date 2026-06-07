"""cocotb runner — Icarus Verilog simulation backend.

Invocation pipeline
-------------------
1. ``iverilog`` compiles the RTL into a VVP binary (build phase).
2. ``vvp`` runs the binary with cocotb's VPI shared library loaded (test phase).
3. Cocotb writes a JUnit-style XML results file; we parse it to determine pass/fail
   without relying on vvp's exit code (which is 0 even when tests fail in cocotb 2.x).

Structured failure trace schema
--------------------------------
On success::

    {"status": "pass"}

On build failure (RTL did not compile — likely Compiler 2 or codegen fault)::

    {
        "status": "fail",
        "phase": "build",
        "error": "<short one-line summary of iverilog stderr>",
        "raw": "<full iverilog stdout + stderr>",
        "failed_vectors": []
    }

On test failure (simulation ran but assertions failed — likely refinement/spec fault)::

    {
        "status": "fail",
        "phase": "test",
        "error": "<short summary: N tests failed>",
        "raw": "<full vvp stdout + stderr>",
        "failed_vectors": [
            {
                "test": "<testcase classname>.<testcase name>",
                "error_type": "<exception class, e.g. AssertionError>",
                "error_msg": "<assertion message>"
            },
            ...
        ]
    }

``phase`` is the primary routing key for the planned end-of-pipeline diagnoser agent:
  - ``phase="build"``  → suspect Compiler 2 / RTL codegen
  - ``phase="test"``   → suspect formal model, refinement chain, or spec vectors

``failed_vectors`` gives the diagnoser structured per-assertion context, avoiding the
need to regex-parse raw cocotb output. ``raw`` is kept for full traceability.

No LLM calls. No imports from openai or anthropic. Surface, don't classify.
"""

import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


def _cocotb_lib_dir() -> str:
    """Return the directory containing cocotb's VPI shared libraries."""
    result = subprocess.run(
        ["cocotb-config", "--lib-dir"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _cocotb_vpi_lib() -> str:
    """Return the cocotb VPI library name for Icarus (without leading 'lib' or extension)."""
    result = subprocess.run(
        ["cocotb-config", "--lib-name", "vpi", "icarus"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _parse_results_xml(xml_path: Path) -> list[dict]:
    """Parse a cocotb JUnit XML results file and return a list of failure dicts.

    Each failure dict has keys: ``test``, ``error_type``, ``error_msg``.
    Returns an empty list if the file is absent or all tests passed.
    """
    if not xml_path.exists():
        return []

    failures = []
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        # JUnit structure: <testsuites><testsuite><testcase [<failure/>]>
        for testcase in root.iter("testcase"):
            failure_el = testcase.find("failure")
            if failure_el is not None:
                classname = testcase.get("classname", "")
                name = testcase.get("name", "")
                failures.append({
                    "test": f"{classname}.{name}",
                    "error_type": failure_el.get("error_type", ""),
                    "error_msg": failure_el.get("error_msg", ""),
                })
    except ET.ParseError:
        pass
    return failures


def run_testbench(testbench_path: Path, rtl_path: Path, module_name: str) -> dict:
    """Run a cocotb testbench against RTL using Icarus Verilog.

    Args:
        testbench_path: Path to the generated cocotb ``.py`` file.
        rtl_path:       Path to the Verilog-2001 RTL module.
        module_name:    Top-level Verilog module name (must match ``testbench_path.stem``
                        prefix convention ``test_<module_name>``).

    Returns:
        ``{"status": "pass"}`` on success, or a structured failure dict (see module
        docstring for the full schema). The ``phase`` key distinguishes build failures
        from test assertion failures so downstream consumers can route the fault.
    """
    # Resolve to absolute paths up front. Phase 2 runs vvp with cwd set to the
    # testbench's directory so cocotb can import the testbench module; if the
    # caller passed RELATIVE paths (the graph does — artifacts/<run_id>/...), a
    # relative vvp_bin would then be re-resolved against that cwd and double up
    # (artifacts/<run_id>/artifacts/<run_id>/...), so vvp reports "Unable to open
    # input file". Absolutising here makes the runner caller-cwd-independent.
    testbench_path = Path(testbench_path).resolve()
    rtl_path = Path(rtl_path).resolve()

    sim_build = testbench_path.parent / "sim_build"
    sim_build.mkdir(parents=True, exist_ok=True)
    vvp_bin = sim_build / f"{module_name}.vvp"
    results_xml = sim_build / "results.xml"
    # Remove stale results file so we never mistake a prior run's outcome.
    results_xml.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Phase 1: BUILD — compile RTL with iverilog
    # The RTL must have a `timescale directive for cocotb's 10 ns clock to work.
    # iverilog emits a non-zero exit code on syntax/elaboration errors.
    # ------------------------------------------------------------------
    iverilog_cmd = [
        "iverilog",
        "-g2001",                         # Verilog-2001 only (project constraint)
        "-o", str(vvp_bin),
        str(rtl_path),
    ]
    build_result = subprocess.run(
        iverilog_cmd,
        capture_output=True,
        text=True,
    )
    if build_result.returncode != 0:
        raw = (build_result.stdout + build_result.stderr).strip()
        # First non-empty line is usually the most informative error.
        first_line = next((ln for ln in raw.splitlines() if ln.strip()), raw)
        return {
            "status": "fail",
            "phase": "build",
            "error": f"iverilog compile error: {first_line}",
            "raw": raw,
            "failed_vectors": [],
        }

    # ------------------------------------------------------------------
    # Phase 2: TEST — run vvp with cocotb VPI library
    # cocotb 2.x requires:
    #   COCOTB_TOPLEVEL        — HDL top-level module name
    #   COCOTB_TEST_MODULES    — Python module name (testbench file stem)
    #   PYGPI_PYTHON_BIN       — absolute path to Python interpreter
    #   COCOTB_RESULTS_FILE    — where cocotb writes JUnit XML (we parse this)
    # vvp exits 0 even when tests fail, so we rely on the XML for pass/fail.
    # ------------------------------------------------------------------
    lib_dir = _cocotb_lib_dir()
    vpi_lib = _cocotb_vpi_lib()

    env_overrides = {
        "COCOTB_TOPLEVEL": module_name,
        "COCOTB_TEST_MODULES": testbench_path.stem,
        "PYGPI_PYTHON_BIN": sys.executable,
        "COCOTB_RESULTS_FILE": str(results_xml),
        # Suppress cocotb's coloured output so raw is readable in logs.
        "COCOTB_ANSI_OUTPUT": "0",
        # Guarantee the generated testbench can import pipeline.* even in a
        # fresh environment where the caller never exported PYTHONPATH. cwd is
        # the artifacts dir, so the repo root must be on the path explicitly.
        # parents[2] of pipeline/cocotb/runner.py is the repo root.
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
    }

    # Inherit the parent environment so Python path etc. are available.
    import os
    env = {**os.environ, **env_overrides}

    vvp_cmd = [
        "vvp",
        "-M", lib_dir,
        "-m", vpi_lib,
        str(vvp_bin),
    ]
    test_result = subprocess.run(
        vvp_cmd,
        capture_output=True,
        text=True,
        cwd=str(testbench_path.parent),   # so cocotb can import the testbench module
        env=env,
    )

    raw = (test_result.stdout + test_result.stderr).strip()

    # Parse XML for structured failure data.
    failed_vectors = _parse_results_xml(results_xml)

    if failed_vectors:
        n = len(failed_vectors)
        return {
            "status": "fail",
            "phase": "test",
            "error": f"{n} test(s) failed in {testbench_path.stem}",
            "raw": raw,
            "failed_vectors": failed_vectors,
        }

    # If the XML file doesn't exist at all, vvp may have crashed before cocotb
    # initialized (e.g. VPI load error). Treat as a build-adjacent failure.
    if not results_xml.exists():
        return {
            "status": "fail",
            "phase": "build",
            "error": "cocotb VPI did not initialize; vvp may have crashed",
            "raw": raw,
            "failed_vectors": [],
        }

    # XML exists and has no failures → all tests passed.
    return {"status": "pass"}
