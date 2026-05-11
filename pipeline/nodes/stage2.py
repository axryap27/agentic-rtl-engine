"""Stage 2 — Refinement node.

Reads ``01_formal_spec.json``, calls Claude Sonnet 4.6 to produce a
PlusCal algorithm that refines the abstract TLA+ spec down to concrete,
hardware-annotated pseudocode, writes the ``.tla`` file to disk, then
writes ``02_pluscal_impl.json``.

Retry policy
------------
``state["retry_counts"]["stage2"]`` is incremented on each failure.  The
graph's conditional edge will re-enter this node up to MAX_RETRIES times.
On the final retry the node writes ``status: "failed"`` and sets
``state["halt"] = True`` so the graph routes to END.
"""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

from pipeline.llm import call_claude
from pipeline.schemas import (
    ConcreteStateVariable,
    FormalSpec,
    PlusCalImpl,
    PPAEstimate,
    PPAImpact,
    Process,
    RuleApplied,
)
from pipeline.state import PipelineState

MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# System prompt (large, reused across retries → gets prompt-cached)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = textwrap.dedent("""\
    You are an expert formal hardware refinement engineer specialising in
    PlusCal and Bluespec System Verilog (BSV).  Your job is to take an
    abstract TLA+ hardware specification and refine it into a concrete
    PlusCal algorithm with BSV annotations that can be compiled to
    synthesizable RTL.

    ## Output format

    Your response MUST be a single JSON object with the following keys.
    Do not include any text outside the JSON.

    {
      "pluscal_module_name": "<CamelCase module name, typically <DesignName>Impl>",
      "pluscal_content": "<complete .tla file as one string; use \\n for newlines>",
      "state_variables": [
        {
          "name": "<variable name, matching the abstract TLA+ variable>",
          "concrete_type": "<BSV type, e.g. Reg#(Bit#(N))>",
          "bsv_mapping": "<Reg | Wire | FIFO | rule <name>>",
          "abstract_variable": "<name of the TLA+ variable this refines>"
        }
      ],
      "processes": [
        {
          "name": "<process name>",
          "description": "<one-sentence description of what this process does>",
          "bsv_mapping": "<rule <name> | method <name>>"
        }
      ],
      "rules_applied": [
        {
          "rule_name": "<e.g. register_introduction | fsm_encoding | pipeline_stage | memory_mapping>",
          "design_decision": "<what decision was made and why>",
          "proof_status": "<verified | pending_tlc | failed>",
          "ppa_impact": {
            "power_delta": "<e.g. +5mW, or null>",
            "performance_delta": "<e.g. +10MHz, or null>",
            "area_delta": "<e.g. +2 flip-flops, or null>"
          }
        }
      ],
      "refinement_mapping": "<string describing how concrete state maps to abstract, e.g. 'count_impl = count_spec'>",
      "preserved_invariants": ["<invariant names from the TLA+ spec that still hold>"],
      "preserved_liveness": ["<liveness property names that still hold>"],
      "ppa_estimate": {
        "power_mw": <float or null>,
        "performance_mhz": <float or null>,
        "area_gates": <float or null>
      },
      "open_issues": ["<unresolved concerns or ambiguities carried forward>"],
      "notes": "<optional string or null>"
    }

    ## PlusCal file structure rules

    1. pluscal_content must be a syntactically valid TLA+ file:
         ---- MODULE <pluscal_module_name> ----
         EXTENDS Naturals (add Sequences, FiniteSets as needed)

         (*--algorithm <AlgorithmName>
         variables <var1> = <init1>;
         begin
           <Label>:
             while TRUE do
               ...
             end while;
         end algorithm; *)

         \\* TLA+ translation placeholder
         ====
    2. Module name in ---- MODULE <name> ---- must exactly match pluscal_module_name.
    3. Every TLA+ state variable must map to exactly one PlusCal variable.
    4. Every abstract Next-state transition must appear as a labelled
       statement block inside the algorithm body.
    5. Label every statement block — PlusCal requires labels on top-level
       while/if constructs.
    6. Use while TRUE do ... end while for free-running hardware processes.
    7. Use await <condition> to model synchronous enable conditions (guards).
    8. Do not include the actual TLA+ translation of the PlusCal — a
       placeholder comment is sufficient.

    ## BSV mapping conventions

    - Abstract variable → physical register:  bsv_mapping = "Reg",  concrete_type = "Reg#(Bit#(N))"
    - Combinational signal:                   bsv_mapping = "Wire", concrete_type = "Wire#(Bit#(N))"
    - Clock-domain FIFO:                      bsv_mapping = "FIFO", concrete_type = "FIFO#(Bit#(N))"
    - FSM state register:                     bsv_mapping = "Reg",  concrete_type = "Reg#(State)"
    - Name each process rule after the corresponding TLA+ action:
      bsv_mapping = "rule <ActionName>"

    ## Refinement rule vocabulary

    - register_introduction : abstract variable → physical flip-flop register
    - fsm_encoding           : abstract state space → binary or one-hot FSM register
    - pipeline_stage         : atomic TLA+ step → pipelined multi-cycle operation
    - memory_mapping         : abstract array → SRAM or register file
    All decisions must preserve every invariant and liveness property
    listed in the spec unless you note the exception in open_issues.
""")


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_user_prompt(spec: FormalSpec, error_log: list[str] | None = None) -> str:
    tla_content = ""
    try:
        tla_content = Path(spec.tla_spec_path).read_text()
    except Exception:
        tla_content = "(TLA+ source file not readable)"

    sv_lines = [
        f"  - name={sv.name}, type={sv.type}, domain={sv.domain}, "
        f"hardware_mapping={sv.hardware_mapping}"
        for sv in spec.state_variables
    ]

    inv_lines = [
        f"  - {inv.name}: {inv.formula} [{inv.property_class}]"
        for inv in spec.invariants
    ]

    lv_lines = [
        f"  - {lp.name}: {lp.formula} [{lp.property_class}]"
        for lp in spec.liveness_properties
    ]

    lines = [
        f"Design name: {spec.design_name}",
        f"TLA+ module: {spec.tla_module_name}",
        "",
        "State variables:",
        *(sv_lines or ["  (none)"]),
        "",
        "Invariants to preserve:",
        *(inv_lines or ["  (none)"]),
        "",
        "Liveness properties to preserve:",
        *(lv_lines or ["  (none)"]),
    ]

    nfc = spec.nfc_constraints
    if nfc.timing or nfc.area or nfc.power:
        lines += ["", "NFC constraints:"]
        for tc in nfc.timing:
            lines.append(f"  - timing: {tc.name} {tc.value}{tc.unit} ({tc.type})")
        for ac in nfc.area:
            lines.append(f"  - area: {ac.name} {ac.budget}{ac.unit}")
        for pc in nfc.power:
            lines.append(f"  - power: {pc.name} {pc.budget}{pc.unit} ({pc.mode})")

    if spec.abstractions_applied:
        lines += ["", "Abstractions applied in TLA+ spec:"]
        lines += [f"  - {a}" for a in spec.abstractions_applied]

    if spec.open_ambiguities:
        lines += ["", "Open ambiguities from formalization:"]
        lines += [f"  - {a}" for a in spec.open_ambiguities]

    lines += [
        "",
        "TLA+ source:",
        "```",
        tla_content,
        "```",
    ]

    if error_log:
        lines += [
            "",
            "PREVIOUS ATTEMPT FAILED.  Fix the following errors and try again:",
        ]
        lines += [f"  - {e}" for e in error_log]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------

def _parse_response(raw: str) -> dict:
    raw = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", raw)
    if fence_match:
        raw = fence_match.group(1).strip()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Node entry point
# ---------------------------------------------------------------------------

def stage2_node(state: PipelineState) -> PipelineState:
    run_id = state["run_id"]
    retry_counts = dict(state.get("retry_counts", {}))
    attempt = retry_counts.get("stage2", 0)

    artifacts_dir = Path("artifacts") / run_id

    # ── Read and validate input ──────────────────────────────────────────────
    with open(artifacts_dir / "01_formal_spec.json") as f:
        raw_spec = json.load(f)
    spec = FormalSpec.model_validate(raw_spec)

    # ── Paths ────────────────────────────────────────────────────────────────
    pluscal_dir = artifacts_dir / "pluscal"
    pluscal_dir.mkdir(exist_ok=True)
    output_path = artifacts_dir / "02_pluscal_impl.json"

    # If a previous failed run wrote an error log, inject it into the retry.
    prior_error_log: list[str] = []
    if attempt > 0 and output_path.exists():
        try:
            prev = json.loads(output_path.read_text())
            prior_error_log = prev.get("error_log", [])
        except Exception:
            pass

    # ── Call Claude ──────────────────────────────────────────────────────────
    print(f"[Stage 2] Calling Claude (attempt {attempt + 1}/{MAX_RETRIES}) ...")
    try:
        raw_response = call_claude(
            system=_SYSTEM_PROMPT,
            user=_build_user_prompt(spec, prior_error_log if attempt > 0 else None),
            max_tokens=4096,
            temperature=0.0,
        )
        parsed = _parse_response(raw_response)
    except Exception as exc:
        return _write_failure(
            state, output_path, run_id, spec,
            retry_counts, attempt,
            error=f"LLM call or JSON parse failed: {exc}",
        )

    # ── Extract required fields ──────────────────────────────────────────────
    try:
        pluscal_module_name: str = parsed["pluscal_module_name"]
        pluscal_content: str = parsed["pluscal_content"]
    except KeyError as exc:
        return _write_failure(
            state, output_path, run_id, spec,
            retry_counts, attempt,
            error=f"Missing required key in LLM response: {exc}",
        )

    # ── Write PlusCal file ───────────────────────────────────────────────────
    pluscal_path = pluscal_dir / f"{pluscal_module_name}.tla"
    pluscal_path.write_text(pluscal_content)
    print(f"[Stage 2] Wrote {pluscal_path}")

    # ── Validate PlusCal structure ───────────────────────────────────────────
    syntax_errors = _validate_pluscal_syntax(pluscal_content, pluscal_module_name)
    if syntax_errors:
        return _write_failure(
            state, output_path, run_id, spec,
            retry_counts, attempt,
            error="; ".join(syntax_errors),
            pluscal_path=str(pluscal_path),
        )

    # ── Build pydantic objects from parsed data ──────────────────────────────
    state_variables = [
        ConcreteStateVariable(
            name=sv["name"],
            concrete_type=sv.get("concrete_type", "Reg#(Bit#(32))"),
            bsv_mapping=sv.get("bsv_mapping", "Reg"),
            abstract_variable=sv.get("abstract_variable", sv["name"]),
        )
        for sv in parsed.get("state_variables", [])
    ]

    processes = [
        Process(
            name=p["name"],
            description=p.get("description", ""),
            bsv_mapping=p.get("bsv_mapping", f"rule {p['name']}"),
        )
        for p in parsed.get("processes", [])
    ]

    rules_applied = [
        RuleApplied(
            rule_name=r["rule_name"],
            design_decision=r.get("design_decision", ""),
            proof_status=r.get("proof_status", "pending_tlc"),
            ppa_impact=PPAImpact(
                power_delta=r.get("ppa_impact", {}).get("power_delta"),
                performance_delta=r.get("ppa_impact", {}).get("performance_delta"),
                area_delta=r.get("ppa_impact", {}).get("area_delta"),
            ),
        )
        for r in parsed.get("rules_applied", [])
    ]

    raw_ppa = parsed.get("ppa_estimate") or {}
    ppa_estimate = PPAEstimate(
        power_mw=raw_ppa.get("power_mw"),
        performance_mhz=raw_ppa.get("performance_mhz"),
        area_gates=raw_ppa.get("area_gates"),
    )

    # Fall back to all invariant/liveness names from Stage 1 if Claude omits them.
    preserved_invariants = parsed.get(
        "preserved_invariants", [inv.name for inv in spec.invariants]
    )
    preserved_liveness = parsed.get(
        "preserved_liveness", [lp.name for lp in spec.liveness_properties]
    )

    impl = PlusCalImpl(
        run_id=run_id,
        status="success",
        design_name=spec.design_name,
        pluscal_path=str(pluscal_path),
        refinement_depth=attempt + 1,
        rules_applied=rules_applied,
        refinement_mapping=parsed.get("refinement_mapping", ""),
        state_variables=state_variables,
        processes=processes,
        preserved_invariants=preserved_invariants,
        preserved_liveness=preserved_liveness,
        backtracks_performed=attempt,
        ppa_estimate=ppa_estimate,
        open_issues=parsed.get("open_issues", []),
        error_log=[],
    )

    output_path.write_text(impl.model_dump_json(indent=2))
    print(f"[Stage 2] Wrote {output_path}")

    retry_counts["stage2"] = 0
    return {**state, "retry_counts": retry_counts, "halt": False}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_pluscal_syntax(content: str, module_name: str) -> list[str]:
    """Lightweight structural checks that catch the most common LLM mistakes."""
    errors: list[str] = []
    if f"MODULE {module_name}" not in content:
        errors.append(
            f"PlusCal module header '---- MODULE {module_name} ----' not found"
        )
    if "(*--algorithm" not in content:
        errors.append("PlusCal algorithm block '(*--algorithm' not found")
    if "end algorithm; *)" not in content:
        errors.append("PlusCal algorithm closing 'end algorithm; *)' not found")
    if "begin" not in content:
        errors.append("PlusCal 'begin' keyword missing from algorithm body")
    if "====" not in content:
        errors.append("TLA+ module closing ==== missing")
    return errors


def _write_failure(
    state: PipelineState,
    output_path: Path,
    run_id: str,
    spec: FormalSpec,
    retry_counts: dict,
    attempt: int,
    error: str,
    pluscal_path: str = "",
) -> PipelineState:
    """Write a failed PlusCalImpl artifact and increment the retry counter."""
    retry_counts["stage2"] = attempt + 1
    should_halt = retry_counts["stage2"] >= MAX_RETRIES

    print(
        f"[Stage 2] FAILURE (attempt {attempt + 1}): {error}  "
        f"{'Halting.' if should_halt else 'Will retry.'}"
    )

    impl = PlusCalImpl(
        run_id=run_id,
        status="failed",
        design_name=spec.design_name,
        pluscal_path=pluscal_path,
        refinement_depth=0,
        rules_applied=[],
        refinement_mapping="",
        state_variables=[],
        processes=[],
        preserved_invariants=[],
        preserved_liveness=[],
        backtracks_performed=attempt,
        ppa_estimate=PPAEstimate(),
        open_issues=[],
        error_log=[error],
    )
    output_path.write_text(impl.model_dump_json(indent=2))

    return {**state, "retry_counts": retry_counts, "halt": should_halt}
