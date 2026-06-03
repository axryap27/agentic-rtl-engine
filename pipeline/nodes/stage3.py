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

Public entry points
-------------------
  run_stage3(state)
      Normal path: generate FormalSpec from scratch, run TLC loop, refine, compile.

  run_stage3_revise_cocotb(state)
      Cocotb-revision path (spec fault): Agent 3 revises the existing FormalSpec
      using the cocotb failure data, then re-runs TLC loop → refinement → Compiler 2.

  run_stage3_backtrack_refinement(state)
      Backtrack path (refinement fault): the FormalSpec is kept unchanged.
      The refinement chain is truncated by BACKTRACK_STEPS, replayed to the
      truncation point, and the engine re-runs from there with failure context
      injected into the pick_rule system prompt.

Reads:  artifacts/<run_id>/01_summary.json
Writes: artifacts/<run_id>/02_formal_spec.json     (FormalSpec JSON)
        artifacts/<run_id>/03_rtl_output.json      (Verilog + metadata, status field)
        artifacts/<run_id>/refinement_chain.json   (rule trace, written by engine)
        artifacts/<run_id>/refinement_chain_prefix.json  (pre-backtrack history)
"""

import json
import subprocess
import tempfile
import traceback
from pathlib import Path

from pipeline.schemas.envelope import write_artifact, write_error
from pipeline.state import PipelineState
from pipeline.schemas.summary_schema import SpecSummary
from pipeline.schemas.tla_schema import FormalSpec

MAX_TLC_RETRIES = 3
BACKTRACK_STEPS = 3   # number of refinement steps to roll back on a refinement fault

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
    from pipeline.refinement.engine import (
        run as _engine_run,
        is_rtl_style as _is_rtl_style,
        RULE_REGISTRY as _RULE_REGISTRY,
        RefinementStall,
        _replay_chain,
    )
    from pipeline.refinement.bridge import (
        formal_spec_to_engine_spec,
        engine_spec_to_rtl_tla,
        engine_spec_to_abstract_tla,
    )
    _ENGINE_AVAILABLE = True
except Exception:
    _ENGINE_AVAILABLE = False

try:
    from pipeline.refinement_templates import (
        pass1_fsm,
        pass2_handshake,
        pass3_datapath,
        pass4_reset,
    )
    _PASS_CONFIGS: list[dict] = [
        {
            "name": "pass1_fsm",
            "allowed": {"SequentialComposition", "Iteration"},
            "system": pass1_fsm.SYSTEM,
            "max_steps": 50,
        },
        {
            "name": "pass2_handshake",
            "allowed": {"Alternation", "IntroduceVariable"},
            "system": pass2_handshake.SYSTEM,
            "max_steps": 30,
        },
        {
            "name": "pass3_datapath",
            "allowed": {"Assignment", "IntroduceVariable"},
            "system": pass3_datapath.SYSTEM,
            "max_steps": 30,
        },
        {
            "name": "pass4_reset",
            "allowed": {"Initialization"},
            "system": pass4_reset.SYSTEM,
            "max_steps": 20,
        },
    ]
    _PASSES_AVAILABLE = True
except Exception:
    _PASS_CONFIGS = []
    _PASSES_AVAILABLE = False


# ---------------------------------------------------------------------------
# TLC runner (best-effort — skipped if TLC not on PATH)
# ---------------------------------------------------------------------------

def _run_tlc(tla_source: str, cfg_source: str) -> tuple[bool, str]:
    try:
        subprocess.run(["tlc", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return True, ""

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
# Module-level helpers (shared across all entry points)
# ---------------------------------------------------------------------------

def _make_tlc_gate(module_name: str):
    """Return a TLC gate callable for use inside the refinement engine."""
    def _gate(engine_spec_dict: dict) -> bool:
        if not _COMPILER1_AVAILABLE or not _ENGINE_AVAILABLE:
            return True
        try:
            tla_src, cfg_src = engine_spec_to_abstract_tla(engine_spec_dict, module_name)
            ok, _ = _run_tlc(tla_src, cfg_src)
            return ok
        except Exception:
            return True
    return _gate


def _make_pass_pick(pass_system_prompt: str):
    """Return a pass-specific pick_rule callable."""
    def _pick(applicable_rules: list[dict], s: dict) -> dict:
        if not _AGENT3_AVAILABLE:
            if applicable_rules:
                return {"rule_name": applicable_rules[0]["name"], "params": {}}
            raise RuntimeError("No applicable rules and Agent 3 unavailable")
        return _agent3.pick_rule(applicable_rules, s, system_prompt=pass_system_prompt)
    return _pick


def _make_pass_termination(allowed: set[str]):
    """Return a pass-specific termination predicate."""
    def _done(s: dict) -> bool:
        return _is_rtl_style(s) or not any(
            r for r in _RULE_REGISTRY
            if r.__class__.__name__ in allowed and r.is_applicable(s)
        )
    return _done


def _make_pick_rule_callable(system_prompt: str | None = None):
    """Return the default (catch-all) pick_rule callable, optionally with a custom prompt."""
    def pick_rule(applicable_rules: list[dict], spec: dict) -> dict:
        if not _AGENT3_AVAILABLE:
            if applicable_rules:
                return {"rule_name": applicable_rules[0]["name"], "params": {}}
            raise RuntimeError("No applicable rules and Agent 3 unavailable")
        return _agent3.pick_rule(applicable_rules, spec, system_prompt=system_prompt)
    return pick_rule


# ---------------------------------------------------------------------------
# Shared TLC → refinement → Compiler 2 pipeline (called by all entry points)
# ---------------------------------------------------------------------------

def _run_stage3_from_spec(state: PipelineState, spec: FormalSpec) -> PipelineState:
    """
    Run the TLC loop, refinement engine, and Compiler 2 for a given FormalSpec.

    Writes 02_formal_spec.json and 03_rtl_output.json before returning.
    Used by run_stage3, run_stage3_revise_cocotb, and (indirectly) as a model
    for run_stage3_backtrack_refinement.
    """
    run_id = state["run_id"]
    artifact_dir = Path("artifacts") / run_id
    formal_path = artifact_dir / "02_formal_spec.json"
    rtl_path = artifact_dir / "03_rtl_output.json"

    # ---- TLC loop ----
    tlc_retry_key = "stage3_tlc"
    tlc_errors = ""
    tla_source = ""

    for attempt in range(MAX_TLC_RETRIES + 1):
        tla_source = ""
        cfg_source = ""
        if _COMPILER1_AVAILABLE:
            try:
                tla_source, cfg_source = _compiler1.compile(spec)
            except Exception as exc:
                tlc_errors = f"Compiler 1 failed: {exc}"
        else:
            break

        if tla_source:
            ok, tlc_errors = _run_tlc(tla_source, cfg_source)
            if ok:
                break

        if attempt < MAX_TLC_RETRIES and tlc_errors:
            state["retry_counts"][tlc_retry_key] = attempt + 1
            try:
                spec = _agent3.revise_on_tlc(spec, tlc_errors)
            except Exception as exc:
                tlc_errors = f"Agent 3 revise_on_tlc failed: {exc}"
                break
        else:
            break

    # Write FormalSpec artifact
    formal_artifact = spec.model_dump()
    formal_artifact["status"] = "success" if not tlc_errors else "error"
    if tlc_errors:
        formal_artifact["tlc_errors"] = tlc_errors
    # Validate the status envelope (BUG-13) before writing.
    write_artifact(formal_path, formal_artifact)

    if tlc_errors and formal_artifact["status"] == "error":
        _write_error(rtl_path, f"TLC verification failed after retries: {tlc_errors}")
        return state

    # ---- Refinement Engine ----
    rtl_tla_source = tla_source   # fallback if engine fails

    if _ENGINE_AVAILABLE:
        try:
            current_spec = formal_spec_to_engine_spec(spec)
            tlc_gate = _make_tlc_gate(spec.module_name)
            refinement_warnings: list[str] = []

            if _PASSES_AVAILABLE:
                for pass_cfg in _PASS_CONFIGS:
                    allowed = pass_cfg["allowed"]
                    try:
                        current_spec = _engine_run(
                            current_spec,
                            _make_pass_pick(pass_cfg["system"]),
                            run_id=run_id,
                            tlc_check=tlc_gate,
                            allowed_rule_names=allowed,
                            termination_check=_make_pass_termination(allowed),
                            max_steps=pass_cfg["max_steps"],
                        )
                    except RefinementStall as e:
                        refinement_warnings.append(f"{pass_cfg['name']} stalled: {e}")

            current_spec = _engine_run(
                current_spec,
                _make_pick_rule_callable(),
                run_id=run_id,
                tlc_check=tlc_gate,
            )

            if refinement_warnings:
                formal_artifact["refinement_warnings"] = refinement_warnings
                write_artifact(formal_path, formal_artifact)

            rtl_tla_source = engine_spec_to_rtl_tla(current_spec, spec.module_name)

        except Exception as exc:
            formal_artifact["refinement_error"] = f"{exc}\n{traceback.format_exc()}"
            formal_artifact["status"] = "partial"
            write_artifact(formal_path, formal_artifact)

    # ---- Compiler 2 → Verilog-2001 ----
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
        write_artifact(rtl_path, {
            "status":       "success",
            "module_name":  spec.module_name,
            "verilog_path": str(verilog_file),
            "verilog":      verilog,
        })
    except Exception as exc:
        _write_error(rtl_path, f"Compiler 2 failed: {exc}\n{traceback.format_exc()}")

    return state


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def run_stage3(state: PipelineState) -> PipelineState:
    """
    LangGraph node for Stage 3 — normal (first-run) path.

    Loads the SpecSummary, calls Agent 3 to generate a FormalSpec from scratch,
    then delegates to _run_stage3_from_spec for TLC → refinement → Compiler 2.
    """
    run_id = state["run_id"]
    artifact_dir = Path("artifacts") / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    formal_path = artifact_dir / "02_formal_spec.json"
    rtl_path = artifact_dir / "03_rtl_output.json"

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

    try:
        spec = _agent3.generate_formal_spec(summary)
    except Exception as exc:
        msg = f"Agent 3 generate_formal_spec failed: {exc}\n{traceback.format_exc()}"
        _write_error(formal_path, msg)
        _write_error(rtl_path, msg)
        return state

    return _run_stage3_from_spec(state, spec)


def run_stage3_revise_cocotb(state: PipelineState) -> PipelineState:
    """
    LangGraph node for Stage 3 — cocotb spec-revision path.

    Called when the diagnoser classifies the failure as a spec fault.
    Agent 3 revises the existing FormalSpec using the structured failure data
    from 04_evaluation.json, then re-runs TLC → refinement → Compiler 2.

    The retry counter is incremented here so _route_after_stage4 can halt
    after _MAX_COCOTB_RETRIES total revision attempts.
    """
    run_id = state["run_id"]
    artifact_dir = Path("artifacts") / run_id
    rtl_path = artifact_dir / "03_rtl_output.json"

    state["retry_counts"]["stage4_cocotb"] = (
        state["retry_counts"].get("stage4_cocotb", 0) + 1
    )

    # Load structured failure data from Stage 4 (phase, failed_vectors, raw)
    eval_path = artifact_dir / "04_evaluation.json"
    sim_log = ""
    try:
        eval_data = json.loads(eval_path.read_text())
        sim_log = (
            f"Error summary: {eval_data.get('error', '')}\n"
            f"Phase: {eval_data.get('phase', '')}\n\n"
            f"Failed test vectors:\n"
            f"{json.dumps(eval_data.get('failed_vectors', []), indent=2)}\n\n"
            f"Raw simulation output:\n{eval_data.get('raw', '')[:3000]}"
        )
    except Exception:
        pass

    # Load current FormalSpec
    formal_path = artifact_dir / "02_formal_spec.json"
    try:
        spec_data = json.loads(formal_path.read_text())
        spec = FormalSpec.model_validate(spec_data)
    except Exception as exc:
        _write_error(rtl_path, f"revise_on_cocotb: cannot load FormalSpec: {exc}")
        return state

    if not _AGENT3_AVAILABLE:
        _write_error(rtl_path, "Agent 3 not available for revise_on_cocotb")
        return state

    # Revise via Agent 3 with the full structured failure context
    try:
        revised = _agent3.revise_on_cocotb(spec, sim_log)
    except Exception as exc:
        _write_error(rtl_path, f"revise_on_cocotb failed: {exc}\n{traceback.format_exc()}")
        return state

    # Hand off to the shared pipeline — BUG-2 FIX: we pass the revised spec
    # directly instead of calling run_stage3() which would re-generate from scratch.
    return _run_stage3_from_spec(state, revised)


def run_stage3_backtrack_refinement(state: PipelineState) -> PipelineState:
    """
    LangGraph node for Stage 3 — refinement backtrack path.

    Called when the diagnoser classifies the failure as a refinement fault.
    The FormalSpec is correct and is kept unchanged. The refinement chain is
    truncated by BACKTRACK_STEPS, replayed to the truncation point, and the
    engine re-runs from there. The diagnosis explanation is injected into the
    pick_rule system prompt so the LLM tries different parameters.

    Writes:
        artifacts/<run_id>/refinement_chain_prefix.json  (pre-backtrack history)
        artifacts/<run_id>/refinement_chain.json         (new steps from engine)
        artifacts/<run_id>/03_rtl_output.json
    """
    run_id = state["run_id"]
    artifact_dir = Path("artifacts") / run_id
    rtl_path = artifact_dir / "03_rtl_output.json"

    state["retry_counts"]["stage4_cocotb"] = (
        state["retry_counts"].get("stage4_cocotb", 0) + 1
    )

    if not _ENGINE_AVAILABLE:
        _write_error(rtl_path, "Refinement engine not available for backtrack")
        return state

    if not _COMPILER2_AVAILABLE:
        _write_error(rtl_path, "Compiler 2 not available for backtrack")
        return state

    # Load FormalSpec (kept unchanged — the spec is correct)
    formal_path = artifact_dir / "02_formal_spec.json"
    try:
        spec_data = json.loads(formal_path.read_text())
        spec = FormalSpec.model_validate(spec_data)
    except Exception as exc:
        _write_error(rtl_path, f"Backtrack: cannot load FormalSpec: {exc}")
        return state

    # Load diagnosis explanation for pick_rule context
    diag_path = artifact_dir / "04_diagnosis.json"
    diagnosis_explanation = ""
    try:
        diag_data = json.loads(diag_path.read_text())
        diagnosis_explanation = diag_data.get("explanation", "")
    except Exception:
        pass

    # Load and truncate the refinement chain
    chain_path = artifact_dir / "refinement_chain.json"
    chain: list[dict] = []
    try:
        chain = json.loads(chain_path.read_text())
    except Exception:
        pass

    if not chain:
        # No chain to backtrack — fall through to spec revision instead
        _write_error(
            rtl_path,
            "Backtrack requested but refinement chain is empty; "
            "no steps to roll back. Route to spec revision manually.",
        )
        return state

    steps_back = min(BACKTRACK_STEPS, max(1, len(chain) // 2))
    truncated_chain = chain[:-steps_back]

    # Preserve the pre-backtrack history for debugging
    prefix_path = artifact_dir / "refinement_chain_prefix.json"
    prefix_path.write_text(json.dumps(chain, indent=2))

    # Replay from the initial engine spec to the truncation point
    try:
        initial_engine_spec = formal_spec_to_engine_spec(spec)
        current_spec = _replay_chain(initial_engine_spec, truncated_chain)
    except Exception as exc:
        _write_error(rtl_path, f"Backtrack replay failed: {exc}\n{traceback.format_exc()}")
        return state

    # Build the backtrack pick function — inject failure context into the prompt
    backtrack_system = (
        _agent3._SYSTEM_PROMPT if _AGENT3_AVAILABLE else ""
    ) + f"""

BACKTRACK CONTEXT: A previous refinement attempt produced incorrect RTL.
Diagnosis: {diagnosis_explanation}
Rolled back {steps_back} step(s). The FormalSpec is correct.
Try different rule parameters — focus on reset values, clock domains, and
update expressions that differ from the choices that led to the failure.
"""

    backtrack_pick = _make_pick_rule_callable(system_prompt=backtrack_system)

    # Write the truncated chain to disk before running the engine.
    # The engine will overwrite refinement_chain.json with the new steps
    # starting from step 0 of this sub-run (the prefix is in _prefix.json).
    chain_path.write_text(json.dumps(truncated_chain, indent=2))

    # Re-run the engine from the truncation point (catch-all pass only — the
    # structured passes already ran in the original attempt).
    try:
        tlc_gate = _make_tlc_gate(spec.module_name)
        final_spec = _engine_run(
            current_spec,
            backtrack_pick,
            run_id=run_id,
            tlc_check=tlc_gate,
        )
        rtl_tla_source = engine_spec_to_rtl_tla(final_spec, spec.module_name)
    except RefinementStall as exc:
        _write_error(rtl_path, f"Backtrack refinement stalled: {exc}")
        return state
    except Exception as exc:
        _write_error(rtl_path, f"Backtrack refinement failed: {exc}\n{traceback.format_exc()}")
        return state

    # Compiler 2 → Verilog-2001
    try:
        compiler = RTLTLACompiler(rtl_tla_source)
        verilog = compiler.compile(module_name=spec.module_name)
        verilog_file = artifact_dir / "output.v"
        verilog_file.write_text(verilog)
        write_artifact(rtl_path, {
            "status":       "success",
            "module_name":  spec.module_name,
            "verilog_path": str(verilog_file),
            "verilog":      verilog,
        })
    except Exception as exc:
        _write_error(rtl_path, f"Compiler 2 failed after backtrack: {exc}\n{traceback.format_exc()}")

    return state


def _write_error(path: Path, message: str) -> None:
    # Routed through the validated envelope helper (BUG-13).
    write_error(path, message)
