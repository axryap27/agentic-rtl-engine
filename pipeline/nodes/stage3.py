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

# Token/cost cap on the LIVE refinement loop — the number of pick_rule (Agent-3,
# LLM) calls each engine pass may make before it stalls. A real design needs only a
# handful of rule applications per pass; the previous caps (50/30/30/20/20 per
# structured pass, plus the engine's 200-step default on the catch-all and backtrack
# passes) let a cycling live picker burn hundreds of Agent-3 calls — and hundreds of
# thousands of input tokens — on a single run. A stalled pass just logs a
# refinement_warning and the run continues, so keeping these small is safe.
_PASS_MAX_STEPS = 8        # per structured pass (was 50/30/30/20/20)
_CATCHALL_MAX_STEPS = 12   # catch-all + backtrack passes (was the engine default, 200)

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
        pass5_mapping,
        pass6_checker,
    )
    _PASS_CONFIGS: list[dict] = [
        {
            "name": "pass1_fsm",
            "allowed": {"SequentialComposition", "Iteration"},
            "system": pass1_fsm.SYSTEM,
            "max_steps": _PASS_MAX_STEPS,
        },
        {
            "name": "pass2_handshake",
            "allowed": {"Alternation", "IntroduceVariable"},
            "system": pass2_handshake.SYSTEM,
            "max_steps": _PASS_MAX_STEPS,
        },
        {
            "name": "pass3_datapath",
            "allowed": {"Assignment", "IntroduceVariable"},
            "system": pass3_datapath.SYSTEM,
            "max_steps": _PASS_MAX_STEPS,
        },
        {
            "name": "pass4_reset",
            "allowed": {"Initialization"},
            "system": pass4_reset.SYSTEM,
            "max_steps": _PASS_MAX_STEPS,
        },
        # Pass 5 — mapping-completeness audit. May only supply a missing
        # mapping symbol (IntroduceVariable); no behavioral rewrite permitted.
        {
            "name": "pass5_mapping",
            "allowed": {"IntroduceVariable"},
            "system": pass5_mapping.SYSTEM,
            "max_steps": _PASS_MAX_STEPS,
        },
        # NOTE: pass6_checker is intentionally NOT an engine pass. It is a pure
        # read-only refinement-correctness critic with no rule to "pick", so it
        # cannot run inside the engine's pick_rule loop. It runs instead as a
        # direct one-shot Agent-3 critic GATE (see _run_refinement_critic below
        # and agent3.critique_refinement) that accepts/rejects the refined spec
        # BEFORE Compiler 2. pass6_checker is still imported above for its SYSTEM
        # prompt, which agent3.critique_refinement sends.
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


def _log_pick_decision(
    run_id: str | None,
    pass_name: str | None,
    applicable_rules: list[dict],
    choice: dict,
) -> None:
    """Append one pick_rule decision to refinement_decisions.jsonl (best-effort).

    The committed refinement_chain.json records only SUCCESSFUL applies; it does
    NOT show what each pass offered, what Agent 3 chose on calls the engine later
    rejected (invalid name, already-excluded, or TLC-gated), or which pass a step
    came from. This incremental, append-as-you-go log captures all of that, so a
    single metered live run yields a COMPLETE refinement trace — a stall is then
    debuggable offline without burning another run. Never raises: a logging
    failure must not break a pipeline run.
    """
    if not run_id:
        return
    try:
        path = Path("artifacts") / run_id / "refinement_decisions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "pass": pass_name,
            "offered": [r.get("name") for r in applicable_rules],
            "chosen": choice.get("rule_name"),
            "params": choice.get("params"),
        }
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def _make_pass_pick(pass_system_prompt: str, *, run_id: str | None = None,
                    pass_name: str | None = None):
    """Return a pass-specific pick_rule callable.

    When run_id is given, every decision is appended to refinement_decisions.jsonl
    (best-effort) so a mid-run stall still leaves a full trace of what this pass
    offered and chose — including picks the engine later rejects.
    """
    def _pick(applicable_rules: list[dict], s: dict) -> dict:
        if not _AGENT3_AVAILABLE:
            if applicable_rules:
                choice = {"rule_name": applicable_rules[0]["name"], "params": {}}
                _log_pick_decision(run_id, pass_name, applicable_rules, choice)
                return choice
            raise RuntimeError("No applicable rules and Agent 3 unavailable")
        choice = _agent3.pick_rule(applicable_rules, s, system_prompt=pass_system_prompt)
        _log_pick_decision(run_id, pass_name, applicable_rules, choice)
        return choice
    return _pick


def _make_pass_termination(allowed: set[str]):
    """Return a pass-specific termination predicate."""
    def _done(s: dict) -> bool:
        return _is_rtl_style(s) or not any(
            r for r in _RULE_REGISTRY
            if r.__class__.__name__ in allowed and r.is_applicable(s)
        )
    return _done


def _run_refinement_critic(
    abstract_engine_spec: dict, refined_engine_spec: dict
) -> dict | None:
    """
    Run the pass6_checker refinement-correctness critic as a one-shot Agent-3 GATE.

    This is the SINGLE, MOCKABLE boundary for the critic. Wave 2 tests stub THIS
    function (`pipeline.nodes.stage3._run_refinement_critic`) — or the underlying
    `pipeline.agents.agent3.critique_refinement` — to return accept/reject WITHOUT
    a live LLM call. It makes NO live call unless actually invoked at the gate.

    Returns:
        The verdict dict {"verdict","issues","reasoning"} from the critic, or
        None to mean "no verdict — proceed to compile" (used when Agent 3 is
        unavailable, e.g. no ANTHROPIC_API_KEY, or the critic call itself errors).
        The critic is an additive safety net: its UNAVAILABILITY must not halt an
        otherwise-valid run, but an explicit 'reject' DOES halt (see the gate).
    """
    if not _AGENT3_AVAILABLE:
        return None
    try:
        return _agent3.critique_refinement(
            abstract_engine_spec,
            refined_engine_spec,
            abstraction_mapping=refined_engine_spec.get("abstraction_mapping", {}),
        )
    except Exception:
        # Critic transport/parse failure — skip the gate rather than halt a run
        # that otherwise refined cleanly. The verdict normaliser already fails
        # closed on a PARSEABLE-but-bad response; this branch is only for the
        # critic being unreachable. Proceeding here matches the no-key path.
        return None


def _make_pick_rule_callable(system_prompt: str | None = None, *,
                             run_id: str | None = None, pass_name: str = "catchall"):
    """Return the default (catch-all) pick_rule callable, optionally with a custom prompt."""
    def pick_rule(applicable_rules: list[dict], spec: dict) -> dict:
        if not _AGENT3_AVAILABLE:
            if applicable_rules:
                choice = {"rule_name": applicable_rules[0]["name"], "params": {}}
                _log_pick_decision(run_id, pass_name, applicable_rules, choice)
                return choice
            raise RuntimeError("No applicable rules and Agent 3 unavailable")
        choice = _agent3.pick_rule(applicable_rules, spec, system_prompt=system_prompt)
        _log_pick_decision(run_id, pass_name, applicable_rules, choice)
        return choice
    return pick_rule


# ---------------------------------------------------------------------------
# Shared TLC → refinement → Compiler 2 pipeline (called by all entry points)
# ---------------------------------------------------------------------------

def _input_port_widths(artifact_dir: Path) -> dict[str, int]:
    """Best-effort {name: width} for the design's INPUT ports from 01_summary.json.

    Used to size free inputs in the reverse bridge (D2): a free input declared
    multi-bit in the Stage-1 SpecSummary (e.g. a 2-bit ALU `op`) must not be
    truncated to 1 bit. Returns {} on any error so the bridge falls back to
    register-feed inference / width-1, never crashing the stage.
    """
    try:
        data = json.loads((artifact_dir / "01_summary.json").read_text())
        summary = SpecSummary.model_validate(data)
        return {p.name: p.width for p in summary.ports if p.direction == "input"}
    except Exception:
        return {}


def _reset_port(artifact_dir: Path) -> str:
    """Best-effort reset-port name for this design from 01_summary.json (FIX 1).

    The cocotb generator drives ``dut.<SpecSummary.reset_port>``; the emitted
    Verilog must declare a reset input of the SAME name or the reset floats and
    the design never resets (the 2-bit counter bug: generator drove `dut.rst`
    while Compiler 2 emitted a `reset` port). We thread this name into BOTH the
    reverse bridge and Compiler 2.

    Defaults to "reset" when the summary is missing, unreadable, not a success,
    or has no reset_port — preserving the prior hardcoded behaviour for designs
    that name their reset "reset". Mirrors `_input_port_widths`' fail-soft style.
    """
    try:
        data = json.loads((artifact_dir / "01_summary.json").read_text())
        if data.get("status") != "success":
            return "reset"
        summary = SpecSummary.model_validate(data)
        return summary.reset_port or "reset"
    except Exception:
        return "reset"


def _run_stage3_from_spec(
    state: PipelineState,
    spec: FormalSpec,
    port_widths: dict[str, int] | None = None,
    reset_port: str = "reset",
) -> PipelineState:
    """
    Run the TLC loop, refinement engine, and Compiler 2 for a given FormalSpec.

    Writes 02_formal_spec.json and 03_rtl_output.json before returning.
    Used by run_stage3, run_stage3_revise_cocotb, and (indirectly) as a model
    for run_stage3_backtrack_refinement.

    port_widths: {name: width} hints (from the SpecSummary input ports) used to
    size free inputs in the reverse bridge (D2). None → bridge falls back to
    register-feed inference / width-1.
    reset_port: the design's reset input name (FIX 1), threaded into BOTH the
    reverse bridge and Compiler 2 so the emitted reset port matches the cocotb
    generator's `dut.<reset_port>`. Defaults to "reset".
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
    # refinement_ok stays False until the engine actually produces RTL-style
    # TLA+. If we fall back to the UNREFINED tla_source (engine unavailable or
    # threw), the emitted Verilog is from the abstract spec, not the refined
    # one — that must be reported as 'partial', never 'success' (G07).
    refinement_ok = False
    abstract_engine_spec: dict | None = None   # pre-refinement, for the critic gate
    refined_engine_spec: dict | None = None    # post-refinement, for the critic gate

    if _ENGINE_AVAILABLE:
        try:
            current_spec = formal_spec_to_engine_spec(spec)
            abstract_engine_spec = formal_spec_to_engine_spec(spec)  # frozen copy
            tlc_gate = _make_tlc_gate(spec.module_name)
            refinement_warnings: list[str] = []

            if _PASSES_AVAILABLE:
                for pass_cfg in _PASS_CONFIGS:
                    allowed = pass_cfg["allowed"]
                    try:
                        current_spec = _engine_run(
                            current_spec,
                            _make_pass_pick(
                                pass_cfg["system"],
                                run_id=run_id, pass_name=pass_cfg["name"],
                            ),
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
                _make_pick_rule_callable(run_id=run_id, pass_name="catchall"),
                run_id=run_id,
                tlc_check=tlc_gate,
                max_steps=_CATCHALL_MAX_STEPS,
            )

            if refinement_warnings:
                formal_artifact["refinement_warnings"] = refinement_warnings
                write_artifact(formal_path, formal_artifact)

            rtl_tla_source = engine_spec_to_rtl_tla(
                current_spec, spec.module_name,
                port_widths=port_widths, reset_port=reset_port,
            )
            refined_engine_spec = current_spec
            refinement_ok = True

        except Exception as exc:
            # Refinement threw. rtl_tla_source stays the UNREFINED fallback and
            # refinement_ok stays False, so the RTL artifact below is written
            # 'partial' (G07) — the Verilog will be from the abstract spec.
            formal_artifact["refinement_error"] = f"{exc}\n{traceback.format_exc()}"
            formal_artifact["status"] = "partial"
            write_artifact(formal_path, formal_artifact)

    # ---- Refinement-correctness critic GATE (pass6_checker) ----
    # Runs ONLY when refinement actually produced a refined spec. A read-only
    # one-shot Agent-3 critic accepts/rejects the refinement BEFORE Compiler 2.
    # On 'reject' we must NOT compile a bad refinement: write a non-success RTL
    # artifact so _route_after_stage3 halts, with the critic's issues in `error`.
    if refinement_ok and refined_engine_spec is not None and abstract_engine_spec is not None:
        verdict = _run_refinement_critic(abstract_engine_spec, refined_engine_spec)
        if verdict is not None and verdict.get("verdict") != "accept":
            issues = verdict.get("issues", [])
            reasoning = verdict.get("reasoning", "")
            critic_msg = (
                "Refinement-correctness critic REJECTED the refined spec; "
                "compilation halted to avoid emitting unverified RTL. "
                f"Issues: {issues}. Reasoning: {reasoning}"
            )
            # Record the verdict on the formal-spec artifact for debugging.
            formal_artifact["status"] = "partial"
            formal_artifact["critic_verdict"] = verdict
            write_artifact(formal_path, formal_artifact)
            # Write the routed artifact with a non-success status so the router
            # halts (G07: 'partial' → halt). The honest error is on disk.
            write_artifact(rtl_path, {
                "status":      "partial",
                "module_name": spec.module_name,
                "error":       critic_msg,
            })
            return state

    # ---- Compiler 2 → Verilog-2001 ----
    if not _COMPILER2_AVAILABLE:
        _write_error(rtl_path, "pipeline.compilers.compiler2.RTLTLACompiler not available")
        return state

    if not rtl_tla_source:
        _write_error(rtl_path, "No RTL-style TLA+ source available for Compiler 2")
        return state

    try:
        compiler = RTLTLACompiler(rtl_tla_source, reset_port=reset_port)
        verilog = compiler.compile(module_name=spec.module_name)
        verilog_file = artifact_dir / "output.v"
        verilog_file.write_text(verilog)
        # G07: only 'success' when refinement actually ran. If we fell back to
        # the unrefined tla_source, the RTL is from the abstract spec → 'partial'.
        rtl_artifact = {
            "status":       "success" if refinement_ok else "partial",
            "module_name":  spec.module_name,
            "verilog_path": str(verilog_file),
            "verilog":      verilog,
        }
        if not refinement_ok:
            rtl_artifact["error"] = (
                "RTL compiled from the UNREFINED/abstract TLA+ spec because the "
                "refinement engine was unavailable or failed; this output is not "
                "from a completed refinement and must not be treated as verified."
            )
        write_artifact(rtl_path, rtl_artifact)
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

    # D2: pass the declared input-port widths so multi-bit free inputs aren't
    # truncated to 1 bit by the reverse bridge.
    port_widths = {p.name: p.width for p in summary.ports if p.direction == "input"}
    # FIX 1: thread the design's actual reset-port name so the emitted reset
    # port matches the cocotb generator's `dut.<reset_port>`.
    reset_port = summary.reset_port or "reset"
    return _run_stage3_from_spec(
        state, spec, port_widths=port_widths, reset_port=reset_port,
    )


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
    return _run_stage3_from_spec(
        state, revised,
        port_widths=_input_port_widths(artifact_dir),
        reset_port=_reset_port(artifact_dir),
    )


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

    backtrack_pick = _make_pick_rule_callable(
        system_prompt=backtrack_system, run_id=run_id, pass_name="backtrack",
    )

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
            max_steps=_CATCHALL_MAX_STEPS,
        )
        rtl_tla_source = engine_spec_to_rtl_tla(
            final_spec, spec.module_name,
            port_widths=_input_port_widths(artifact_dir),
            reset_port=_reset_port(artifact_dir),
        )
    except RefinementStall as exc:
        _write_error(rtl_path, f"Backtrack refinement stalled: {exc}")
        return state
    except Exception as exc:
        _write_error(rtl_path, f"Backtrack refinement failed: {exc}\n{traceback.format_exc()}")
        return state

    # Compiler 2 → Verilog-2001
    try:
        compiler = RTLTLACompiler(rtl_tla_source, reset_port=_reset_port(artifact_dir))
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
