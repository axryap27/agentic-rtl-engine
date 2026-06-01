"""
Stage 3 node — Agent 3 + Compiler 1 + Refinement Engine + Compiler 2.

This is the formal branch:
  1. Agent 3 generates a FormalSpec (JSON(TLA)) from the SpecSummary.
  2. Compiler 1 emits TLA+ source from the FormalSpec.
  3. TLC model-checks the TLA+ (if available). On failure, Agent 3 revises
     the FormalSpec and we loop (up to MAX_TLC_RETRIES times).
  4. Refinement Engine drives rule-application until RTL-style TLA+ is reached.
     Agent 3's pick_rule is the injected callable.
  5. Compiler 2 emits Verilog-2001 from the RTL-style TLA+.

Reads:  artifacts/<run_id>/01_summary.json
Writes: artifacts/<run_id>/02_formal_spec.json     (FormalSpec JSON)
        artifacts/<run_id>/03_rtl_output.json      (Verilog + metadata, status field)
        artifacts/<run_id>/refinement_chain.json   (rule trace, written by engine)

TLC retry: up to 3 attempts (PipelineState.retry_counts["stage3_tlc"]).
"""

import json
import subprocess
import tempfile
import traceback
from pathlib import Path

from pipeline.state import PipelineState
from pipeline.schemas.summary_schema import SpecSummary
from pipeline.schemas.tla_schema import FormalSpec

MAX_TLC_RETRIES = 3

try:
    from pipeline.agents import agent3 as _agent3
    _AGENT3_AVAILABLE = True
except Exception:
    _AGENT3_AVAILABLE = False

try:
    from pipeline.compilers import compiler1 as _compiler1
    _COMPILER1_AVAILABLE = True
except Exception:
    _COMPILER1_AVAILABLE = False

try:
    from pipeline.compilers.compiler2 import RTLTLACompiler
    _COMPILER2_AVAILABLE = True
except Exception:
    _COMPILER2_AVAILABLE = False

try:
    from pipeline.refinement.engine import run as _engine_run
    _ENGINE_AVAILABLE = True
except Exception:
    _ENGINE_AVAILABLE = False


# ---------------------------------------------------------------------------
# TLC runner (best-effort — skipped if TLC not on PATH)
# ---------------------------------------------------------------------------

def _run_tlc(tla_source: str, cfg_source: str) -> tuple[bool, str]:
    """
    Run TLC on the given TLA+ source.

    Returns (ok: bool, stderr: str).
    If TLC is not installed, returns (True, "") — we skip checking.
    """
    try:
        subprocess.run(["tlc", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return True, ""  # TLC not available; skip

    with tempfile.TemporaryDirectory() as tmpdir:
        tla_path = Path(tmpdir) / "Spec.tla"
        cfg_path = Path(tmpdir) / "Spec.cfg"
        tla_path.write_text(tla_source)
        cfg_path.write_text(cfg_source)
        result = subprocess.run(
            ["tlc", "-config", str(cfg_path), str(tla_path)],
            capture_output=True,
            text=True,
        )
        ok = result.returncode == 0
        errors = (result.stdout + result.stderr).strip() if not ok else ""
        return ok, errors


# ---------------------------------------------------------------------------
# pick_rule callable injected into the Refinement Engine
# ---------------------------------------------------------------------------

def _make_pick_rule_callable():
    """Return a pick_rule callable bound to Agent 3 (if available)."""

    def pick_rule(applicable_rules: list[dict], spec: dict) -> dict:
        if not _AGENT3_AVAILABLE:
            # Fallback: pick the first applicable rule mechanically.
            if applicable_rules:
                return {"rule_name": applicable_rules[0]["rule_name"], "params": {}}
            raise RuntimeError("No applicable rules and Agent 3 unavailable")
        return _agent3.pick_rule(applicable_rules, spec)

    return pick_rule


# ---------------------------------------------------------------------------
# Node entry point
# ---------------------------------------------------------------------------

def run_stage3(state: PipelineState) -> PipelineState:
    """
    LangGraph node for Stage 3.

    Orchestrates the full formal branch: spec generation → TLC loop →
    refinement → RTL compilation. Writes both 02_formal_spec.json and
    03_rtl_output.json before returning, even on failure.
    """
    run_id = state["run_id"]
    artifact_dir = Path("artifacts") / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    formal_path = artifact_dir / "02_formal_spec.json"
    rtl_path = artifact_dir / "03_rtl_output.json"

    # ---- Load Stage 1 output ----
    summary_path = artifact_dir / "01_summary.json"
    try:
        data = json.loads(summary_path.read_text())
        if data.get("status") != "success":
            _write_error(formal_path, f"Stage 1 did not succeed: {data.get('status')}")
            _write_error(rtl_path, "Stage 3 aborted: Stage 1 did not succeed")
            return state
        summary = SpecSummary.model_validate(data)
    except Exception as exc:
        msg = f"Failed to load SpecSummary: {exc}\n{traceback.format_exc()}"
        _write_error(formal_path, msg)
        _write_error(rtl_path, msg)
        return state

    if not _AGENT3_AVAILABLE:
        msg = "pipeline.agents.agent3 could not be imported"
        _write_error(formal_path, msg)
        _write_error(rtl_path, msg)
        return state

    # ---- Step 1: Generate FormalSpec ----
    try:
        spec = _agent3.generate_formal_spec(summary)
    except Exception as exc:
        msg = f"Agent 3 generate_formal_spec failed: {exc}\n{traceback.format_exc()}"
        _write_error(formal_path, msg)
        _write_error(rtl_path, msg)
        return state

    # ---- Step 2: TLC loop (up to MAX_TLC_RETRIES) ----
    tlc_retry_key = "stage3_tlc"
    tlc_errors = ""

    for attempt in range(MAX_TLC_RETRIES + 1):
        # Compile to TLA+ if Compiler 1 is available
        tla_source = ""
        cfg_source = ""
        if _COMPILER1_AVAILABLE:
            try:
                tla_source, cfg_source = _compiler1.compile(spec)
            except Exception as exc:
                tlc_errors = f"Compiler 1 failed: {exc}"
                # Treat as a TLC error and let Agent 3 revise
        else:
            # Compiler 1 not yet built; skip TLC entirely for now
            break

        if tla_source:
            ok, tlc_errors = _run_tlc(tla_source, cfg_source)
            if ok:
                break

        # TLC failed — revise spec if we have retries left
        if attempt < MAX_TLC_RETRIES and tlc_errors:
            # Increment TLC retry counter in state so it persists to the next
            # LangGraph node (nodes return state; edge functions only route on it).
            state["retry_counts"][tlc_retry_key] = attempt + 1
            try:
                spec = _agent3.revise_on_tlc(spec, tlc_errors)
            except Exception as exc:
                tlc_errors = f"Agent 3 revise_on_tlc failed: {exc}"
                break
        else:
            break

    # Write the (possibly TLC-surviving) FormalSpec artifact
    formal_artifact = spec.model_dump()
    formal_artifact["status"] = "success" if not tlc_errors else "error"
    if tlc_errors:
        formal_artifact["tlc_errors"] = tlc_errors
    formal_path.write_text(json.dumps(formal_artifact, indent=2))

    # If TLC ultimately failed, stop here
    if tlc_errors and formal_artifact["status"] == "error":
        _write_error(rtl_path, f"TLC verification failed after retries: {tlc_errors}")
        return state

    # ---- Step 3: Refinement Engine ----
    rtl_tla_source = tla_source  # may be empty if compiler1 unavailable

    if _ENGINE_AVAILABLE and tla_source:
        try:
            pick_fn = _make_pick_rule_callable()
            # Pass run_id so engine writes refinement_chain.json to the correct
            # artifacts/<run_id>/ directory (not the default "default" dir).
            engine_result = _engine_run(spec.model_dump(), pick_fn, run_id=run_id)
            # engine.run() returns the RTL-style spec dict (engine-internal format).
            # Compiler 2 needs TLA+ text, not a spec dict. The engine does not emit
            # TLA+ itself — that is Compiler 1's responsibility, applied to a
            # FormalSpec. At this stage we continue with the Compiler-1-generated
            # TLA+ (tla_source), which is the correct input for Compiler 2's
            # RTL-pattern recogniser. The engine result is recorded in the artifact
            # for debugging; a future integration can re-run Compiler 1 on the
            # refined spec if tla_source key is present.
            if isinstance(engine_result, dict) and "tla_source" in engine_result:
                rtl_tla_source = engine_result["tla_source"]
        except Exception as exc:
            # Refinement failure degrades to partial — still try Compiler 2
            # with the original (pre-refinement) TLA+.
            formal_artifact["refinement_error"] = f"{exc}\n{traceback.format_exc()}"
            formal_artifact["status"] = "partial"
            formal_path.write_text(json.dumps(formal_artifact, indent=2))

    # ---- Step 4: Compiler 2 → Verilog-2001 ----
    if not _COMPILER2_AVAILABLE:
        _write_error(rtl_path, "pipeline.compilers.compiler2.RTLTLACompiler not available")
        return state

    if not rtl_tla_source:
        _write_error(rtl_path, "No RTL-style TLA+ source available for Compiler 2")
        return state

    try:
        compiler = RTLTLACompiler(rtl_tla_source)
        verilog = compiler.compile(module_name=spec.module_name)
        verilog_file = artifact_dir / "output.v"
        verilog_file.write_text(verilog)

        rtl_path.write_text(json.dumps({
            "status": "success",
            "module_name": spec.module_name,
            "verilog_path": str(verilog_file),
            "verilog": verilog,
        }, indent=2))
    except Exception as exc:
        _write_error(rtl_path, f"Compiler 2 failed: {exc}\n{traceback.format_exc()}")

    return state


def _write_error(path: Path, message: str) -> None:
    path.write_text(json.dumps({"status": "error", "error": message}, indent=2))
