"""Stage 1 — Formalization node.

Reads ``00_nl_spec.json``, calls Claude Sonnet 4.6 to produce a TLA+
specification (.tla + .cfg), writes those files to disk, then writes
``01_formal_spec.json``.

Retry policy
------------
``state["retry_counts"]["stage1"]`` is incremented on each failure.  The
graph's conditional edge will re-enter this node up to MAX_RETRIES times.
On the final retry the node writes ``status: "failed"`` and sets
``state["halt"] = True`` so the graph can route to END rather than
continuing downstream.
"""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

from pipeline.llm import call_claude
from pipeline.schemas import (
    FormalSpec,
    FormalStateVariable,
    Invariant,
    LivenessProperty,
    NFCConstraints,
    NLSpec,
    TimingConstraint,
    AreaConstraint,
    PowerConstraint,
)
from pipeline.state import PipelineState

MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# System prompt (large, reused across retries → gets prompt-cached)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = textwrap.dedent("""\
    You are an expert formal hardware specification engineer specialising in
    TLA+ and TLC model checking.  Your job is to translate a natural-language
    hardware design description into a syntactically valid TLA+ module and
    matching TLC configuration file.

    ## Output format

    Your response MUST be a single JSON object with the following keys.
    Do not include any text outside the JSON.

    {
      "tla_module_name": "<CamelCase module name, no spaces>",
      "tla_content": "<full .tla file content as a single string, newlines as \\n>",
      "cfg_content": "<full .cfg file content as a single string, newlines as \\n>",
      "state_variables": [
        {
          "name": "<TLA+ variable name>",
          "type": "<TLA+ type or description>",
          "domain": "<value domain, e.g. 0..N-1 or BOOLEAN>",
          "hardware_mapping": "<register | FSM_state | counter | flag | memory | etc.>"
        }
      ],
      "invariants": [
        {
          "name": "<invariant name, matching what is in the .cfg INVARIANT line>",
          "formula": "<TLA+ formula>",
          "property_class": "<safety | mutual_exclusion | no_deadlock | data_integrity | other>"
        }
      ],
      "liveness_properties": [
        {
          "name": "<property name, matching .cfg PROPERTY line>",
          "formula": "<TLA+ temporal formula>",
          "property_class": "<progress | response | fairness>"
        }
      ],
      "timing_constraints": [
        {
          "name": "<constraint name>",
          "type": "<setup_time | hold_time | clock_period | latency>",
          "value": "<numeric string>",
          "unit": "<ns | ps | MHz>",
          "source_requirement": "<quoted phrase from the NL spec>"
        }
      ],
      "area_constraints": [
        {
          "name": "<constraint name>",
          "budget": "<numeric string>",
          "unit": "<gates | LUTs | um2>",
          "source_requirement": "<quoted phrase from the NL spec>"
        }
      ],
      "power_constraints": [
        {
          "name": "<constraint name>",
          "budget": "<numeric string>",
          "unit": "<mW | uW>",
          "mode": "<active | idle | peak>",
          "source_requirement": "<quoted phrase from the NL spec>"
        }
      ],
      "abstractions_applied": ["<list of abstraction choices made>"],
      "open_ambiguities": ["<list of unclear points in the NL spec>"],
      "notes": "<optional notes, or null>"
    }

    ## TLA+ style rules

    1. Use EXTENDS Naturals (and Sequences / FiniteSets as needed).
    2. Variables must be declared with VARIABLES at the top of the module.
    3. Init and Next predicates must be defined.
    4. The Spec temporal formula must be defined: Spec == Init /\\ [][Next]_vars /\\ WF_vars(Next).
    5. Every invariant referenced in the .cfg INVARIANT section must be
       defined as a state predicate in the .tla file.
    6. Every property referenced in the .cfg PROPERTY section must be
       defined as a temporal formula in the .tla file.
    7. Do NOT use ASSUME or CONSTANT for values that are structurally fixed
       by the NL spec; define them as literal values instead.
    8. Module name in ---- MODULE <name> ---- must exactly match
       tla_module_name.

    ## .cfg style rules

    1. Lines: INIT <name>, NEXT <name>, INVARIANT <name>, PROPERTY <name>.
    2. List one item per line; repeat the keyword for multiple items.
    3. Do not include comments.

    If the NL spec mentions explicit PPA targets (frequency, area, power),
    populate the constraint lists.  If none are mentioned, return empty lists.
""")

# Prompt builder
def _build_user_prompt(nl_spec: NLSpec, error_log: list[str] | None = None) -> str:
    lines = [
        f"Design name: {nl_spec.design_name}",
        f"Design class: {nl_spec.design_class}",
        "",
        "Natural language description:",
        nl_spec.nl_description,
    ]
    if nl_spec.additional_constraints:
        lines += ["", "Additional constraints:", nl_spec.additional_constraints]

    ppa = nl_spec.ppa_targets
    ppa_lines = []
    if ppa.max_freq_mhz is not None:
        ppa_lines.append(f"  max_freq_mhz: {ppa.max_freq_mhz}")
    if ppa.max_area_gates is not None:
        ppa_lines.append(f"  max_area_gates: {ppa.max_area_gates}")
    if ppa.max_power_mw is not None:
        ppa_lines.append(f"  max_power_mw: {ppa.max_power_mw}")
    if ppa_lines:
        lines += ["", "PPA targets:"] + ppa_lines

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
    """Extract the JSON object from Claude's response.

    Claude may wrap it in a code fence; we strip that if present.
    """
    raw = raw.strip()
    # Strip markdown code fences if present
    fence_match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", raw)
    if fence_match:
        raw = fence_match.group(1).strip()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Node entry point
# ---------------------------------------------------------------------------

def stage1_node(state: PipelineState) -> PipelineState:
    run_id = state["run_id"]
    retry_counts = dict(state.get("retry_counts", {}))
    attempt = retry_counts.get("stage1", 0)

    artifacts_dir = Path("artifacts") / run_id

    # ── Read and validate input ──────────────────────────────────────────────
    with open(artifacts_dir / "00_nl_spec.json") as f:
        raw_spec = json.load(f)
    nl_spec = NLSpec.model_validate(raw_spec)

    # ── Paths ────────────────────────────────────────────────────────────────
    tla_dir = artifacts_dir / "tla"
    tla_dir.mkdir(exist_ok=True)

    output_path = artifacts_dir / "01_formal_spec.json"

    # If a previous failed run wrote an error log, inject it into the retry.
    prior_error_log: list[str] = []
    if attempt > 0 and output_path.exists():
        try:
            prev = json.loads(output_path.read_text())
            prior_error_log = prev.get("error_log", [])
        except Exception:
            pass

    # ── Call Claude ──────────────────────────────────────────────────────────
    print(f"[Stage 1] Calling Claude (attempt {attempt + 1}/{MAX_RETRIES}) ...")
    try:
        raw_response = call_claude(
            system=_SYSTEM_PROMPT,
            user=_build_user_prompt(nl_spec, prior_error_log if attempt > 0 else None),
            max_tokens=4096,
            temperature=0.0,
        )
        parsed = _parse_response(raw_response)
    except Exception as exc:
        return _write_failure(
            state, output_path, run_id, nl_spec,
            retry_counts, attempt,
            error=f"LLM call or JSON parse failed: {exc}",
        )

    # ── Extract fields from parsed JSON ──────────────────────────────────────
    try:
        tla_module_name: str = parsed["tla_module_name"]
        tla_content: str = parsed["tla_content"]
        cfg_content: str = parsed["cfg_content"]
    except KeyError as exc:
        return _write_failure(
            state, output_path, run_id, nl_spec,
            retry_counts, attempt,
            error=f"Missing required key in LLM response: {exc}",
        )

    # ── Write TLA+ files ─────────────────────────────────────────────────────
    tla_path = tla_dir / f"{tla_module_name}.tla"
    cfg_path = tla_dir / f"{tla_module_name}.cfg"
    tla_path.write_text(tla_content)
    cfg_path.write_text(cfg_content)
    print(f"[Stage 1] Wrote {tla_path} and {cfg_path}")

    # ── Basic syntax validation ───────────────────────────────────────────────
    syntax_errors = _validate_tla_syntax(tla_content, tla_module_name, cfg_content)
    tla_syntax_valid = len(syntax_errors) == 0

    if not tla_syntax_valid:
        return _write_failure(
            state, output_path, run_id, nl_spec,
            retry_counts, attempt,
            error="; ".join(syntax_errors),
            tla_module_name=tla_module_name,
            tla_path=str(tla_path),
            cfg_path=str(cfg_path),
        )

    # ── Build pydantic objects from parsed data ───────────────────────────────
    state_variables = [
        FormalStateVariable(
            name=sv["name"],
            type=sv.get("type", "unknown"),
            domain=sv.get("domain", "unknown"),
            hardware_mapping=sv.get("hardware_mapping", "register"),
        )
        for sv in parsed.get("state_variables", [])
    ]

    invariants = [
        Invariant(
            name=inv["name"],
            formula=inv["formula"],
            property_class=inv.get("property_class", "safety"),
        )
        for inv in parsed.get("invariants", [])
    ]

    liveness_properties = [
        LivenessProperty(
            name=lp["name"],
            formula=lp["formula"],
            property_class=lp.get("property_class", "progress"),
        )
        for lp in parsed.get("liveness_properties", [])
    ]

    timing_constraints = [
        TimingConstraint(
            name=tc["name"],
            type=tc["type"],
            value=tc["value"],
            unit=tc["unit"],
            source_requirement=tc["source_requirement"],
        )
        for tc in parsed.get("timing_constraints", [])
    ]

    area_constraints = [
        AreaConstraint(
            name=ac["name"],
            budget=ac["budget"],
            unit=ac["unit"],
            source_requirement=ac["source_requirement"],
        )
        for ac in parsed.get("area_constraints", [])
    ]

    power_constraints = [
        PowerConstraint(
            name=pc["name"],
            budget=pc["budget"],
            unit=pc["unit"],
            mode=pc["mode"],
            source_requirement=pc["source_requirement"],
        )
        for pc in parsed.get("power_constraints", [])
    ]

    nfc = NFCConstraints(
        timing=timing_constraints,
        area=area_constraints,
        power=power_constraints,
    )

    spec = FormalSpec(
        run_id=run_id,
        status="success",
        design_name=nl_spec.design_name,
        tla_module_name=tla_module_name,
        tla_spec_path=str(tla_path),
        tla_cfg_path=str(cfg_path),
        tlc_verified=False,
        tla_syntax_valid=True,
        state_variables=state_variables,
        invariants=invariants,
        liveness_properties=liveness_properties,
        nfc_constraints=nfc,
        abstractions_applied=parsed.get("abstractions_applied", []),
        open_ambiguities=parsed.get("open_ambiguities", []),
        error_log=[],
        notes=parsed.get("notes"),
    )

    output_path.write_text(spec.model_dump_json(indent=2))
    print(f"[Stage 1] Wrote {output_path}")

    # Success — clear any prior stage1 retry count.
    retry_counts["stage1"] = 0
    return {**state, "retry_counts": retry_counts, "halt": False}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_tla_syntax(tla_content: str, module_name: str, cfg_content: str) -> list[str]:
    """Lightweight structural checks that catch the most common LLM mistakes."""
    errors: list[str] = []

    if f"MODULE {module_name}" not in tla_content:
        errors.append(
            f"TLA+ module header '---- MODULE {module_name} ----' not found in .tla content"
        )
    if "VARIABLES" not in tla_content:
        errors.append("VARIABLES declaration missing from .tla content")
    if "Init ==" not in tla_content and "Init==" not in tla_content:
        errors.append("Init predicate not defined in .tla content")
    if "Next ==" not in tla_content and "Next==" not in tla_content:
        errors.append("Next predicate not defined in .tla content")
    if "Spec ==" not in tla_content and "Spec==" not in tla_content:
        errors.append("Spec temporal formula not defined in .tla content")
    if "====" not in tla_content:
        errors.append("TLA+ module closing ==== missing")
    if "INIT" not in cfg_content:
        errors.append(".cfg file missing INIT line")
    if "NEXT" not in cfg_content:
        errors.append(".cfg file missing NEXT line")

    return errors


def _write_failure(
    state: PipelineState,
    output_path: Path,
    run_id: str,
    nl_spec: NLSpec,
    retry_counts: dict,
    attempt: int,
    error: str,
    tla_module_name: str = "Unknown",
    tla_path: str = "",
    cfg_path: str = "",
) -> PipelineState:
    """Write a failed FormalSpec artifact and increment retry counter."""
    retry_counts["stage1"] = attempt + 1
    should_halt = retry_counts["stage1"] >= MAX_RETRIES

    print(
        f"[Stage 1] FAILURE (attempt {attempt + 1}): {error}  "
        f"{'Halting.' if should_halt else 'Will retry.'}"
    )

    spec = FormalSpec(
        run_id=run_id,
        status="failed",
        design_name=nl_spec.design_name,
        tla_module_name=tla_module_name,
        tla_spec_path=tla_path,
        tla_cfg_path=cfg_path,
        tlc_verified=False,
        tla_syntax_valid=False,
        state_variables=[],
        invariants=[],
        liveness_properties=[],
        nfc_constraints=NFCConstraints(),
        abstractions_applied=[],
        open_ambiguities=[],
        error_log=[error],
        notes=f"Attempt {attempt + 1} of {MAX_RETRIES}",
    )
    output_path.write_text(spec.model_dump_json(indent=2))

    return {**state, "retry_counts": retry_counts, "halt": should_halt}