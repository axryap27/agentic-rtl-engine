"""Stage 3 — RTL Codegen node.

Reads ``02_pluscal_impl.json``, calls Claude Sonnet 4.6 to generate
synthesizable Verilog, runs ``verilator --lint-only`` on the output,
retries once with lint errors injected if the first pass fails, then
writes ``03_rtl_output.json`` and the ``.v`` file.

Retry policy
------------
The graph's conditional edge re-enters this node when
``state["retry_counts"]["stage3"] > 0`` and < MAX_RETRIES.  On each call
the node reads any prior lint errors from the saved JSON artifact and
injects them into the re-prompt.

Lint tool selection
-------------------
``verilator --lint-only`` is preferred.  If verilator is not on PATH,
the node falls back to ``iverilog -Wall -t null``.  If neither is
available the lint step is skipped and ``lint_tool`` is set to ``"none"``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Optional

from pipeline.llm import call_claude
from pipeline.schemas import (
    PortEntry,
    PlusCalImpl,
    RTLOutput,
)
from pipeline.state import PipelineState

MAX_RETRIES = 2  # 1 original attempt + 1 lint-error retry

# System prompt (large, reused across retries → gets prompt-cached)
_SYSTEM_PROMPT = textwrap.dedent("""\
    You are an expert RTL engineer who generates synthesizable Verilog from
    PlusCal / BSV-annotated hardware specifications.

    ## Output format

    Your response MUST be a single JSON object with the following keys.
    Do NOT include any text outside the JSON.

    {
      "top_module_name": "<valid Verilog identifier, must equal design_name>",
      "verilog_content": "<complete synthesizable Verilog source as one string; use \\n for newlines>",
      "port_list": [
        {
          "name": "<port name>",
          "direction": "<input | output | inout>",
          "width": <integer bit-width>,
          "description": "<one-sentence description>"
        }
      ],
      "compilation_path": "<bsv | direct_structural>",
      "assumptions_made": ["<list any ambiguities resolved with conservative defaults>"],
      "notes": "<optional string or null>"
    }

    ## Verilog style rules

    1. The module must be synthesizable.  No `initial` blocks in RTL (only
       permitted inside `// synthesis translate_off` guards for simulation
       models, which are not required here).
    2. No `#delay` statements.
    3. No non-synthesizable system tasks (`$display`, `$finish`, etc.) in the
       RTL module itself.
    4. Use `always @(posedge clk)` for all sequential logic.
    5. Synchronous active-low reset: `if (!rst_n) ... else ...` inside the
       clocked always block.  Default data width: 32 bits when not specified.
    6. All outputs must be driven on every path through the combinational
       logic.  Use default assignments at the top of combinational always
       blocks to prevent latches.
    7. Parameterize widths where the spec hints at parameterizability.
    8. Use meaningful wire/reg names that match the PlusCal variable names.
    9. Include a brief comment header describing the design.
    10. Do NOT use SystemVerilog constructs (no `logic`, no `always_ff`,
        no `always_comb`, no `typedef`).  Target plain Verilog-2001.

    ## Mapping guidance

    - PlusCal `variables` → `reg` declarations
    - PlusCal `await` guard → FSM state transition enable condition
    - PlusCal `while TRUE` loop → free-running clocked logic (no FSM state
      needed if there is only one loop)
    - PlusCal `with` nondeterministic choice → priority-encoded selection
    - Each `bsv_mapping: "Reg"` variable → a `reg` register
    - Each `bsv_mapping: "rule <name>"` process → a clocked always block or
      FSM arc labelled with the rule name in a comment

    ## Port conventions (always include these)

    - `input wire clk`  — main clock
    - `input wire rst_n` — synchronous active-low reset
    - Additional ports from the design spec
""")

# Prompt builders
def _build_user_prompt(impl: PlusCalImpl, lint_errors: list[str] | None = None,) -> str:
    pluscal_text = ""
    try:
        pluscal_text = Path(impl.pluscal_path).read_text()
    except Exception:
        pluscal_text = "(PlusCal source file not readable)"

    sv_lines = []
    for sv in impl.state_variables:
        sv_lines.append(
            f"  - name={sv.name}, concrete_type={sv.concrete_type}, "
            f"bsv_mapping={sv.bsv_mapping}, from_abstract={sv.abstract_variable}"
        )

    proc_lines = []
    for proc in impl.processes:
        proc_lines.append(
            f"  - name={proc.name}: {proc.description} "
            f"(bsv_mapping={proc.bsv_mapping})"
        )

    lines = [
        f"Design name: {impl.design_name}",
        f"PlusCal status: {impl.status}",
        f"Refinement depth: {impl.refinement_depth}",
        "",
        "State variables:",
        *sv_lines,
        "",
        "Processes:",
        *proc_lines,
        "",
        "Preserved invariants: " + ", ".join(impl.preserved_invariants),
        "Preserved liveness:   " + ", ".join(impl.preserved_liveness),
        "",
        "Open issues from refinement stage:",
        *([f"  - {oi}" for oi in impl.open_issues] or ["  (none)"]),
        "",
        "PlusCal source:",
        "```",
        pluscal_text,
        "```",
    ]

    if lint_errors:
        lines += [
            "",
            "PREVIOUS VERILOG ATTEMPT FAILED LINT.  Fix ALL of the following "
            "issues and regenerate the complete Verilog from scratch:",
        ]
        lines += [f"  - {e}" for e in lint_errors]

    return "\n".join(lines)


# Lint helpers
def _detect_lint_tool() -> Optional[str]:
    if shutil.which("verilator"):
        return "verilator"
    if shutil.which("iverilog"):
        return "iverilog"
    return None


def _run_lint(verilog_content: str, top_module: str, lint_tool: str) -> tuple[bool, list[str]]:
    """Write verilog_content to a temp file and lint it.

    Returns (passed: bool, messages: list[str]).
    """
    with tempfile.NamedTemporaryFile(
        suffix=".v", mode="w", delete=False, prefix="rtl_lint_"
    ) as tmp:
        tmp.write(verilog_content)
        tmp_path = tmp.name

    try:
        if lint_tool == "verilator":
            cmd = ["verilator", "--lint-only", "--Wall", tmp_path]
        else:  # iverilog
            cmd = ["iverilog", "-Wall", "-t", "null", tmp_path]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = (result.stdout + result.stderr).strip()
        passed = result.returncode == 0
        messages = [ln for ln in output.splitlines() if ln.strip()]
        return passed, messages

    except subprocess.TimeoutExpired:
        return False, ["Lint tool timed out after 60 seconds"]
    except FileNotFoundError:
        return False, [f"Lint tool '{lint_tool}' not found on PATH"]
    finally:
        try:
            Path(tmp_path).unlink()
        except Exception:
            pass


# Response parser
def _parse_response(raw: str) -> dict:
    raw = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", raw)
    if fence_match:
        raw = fence_match.group(1).strip()
    return json.loads(raw)


# Node entry point
def stage3_node(state: PipelineState) -> PipelineState:
    run_id = state["run_id"]
    retry_counts = dict(state.get("retry_counts", {}))
    attempt = retry_counts.get("stage3", 0)

    artifacts_dir = Path("artifacts") / run_id

    # ── Read and validate input ──────────────────────────────────────────────
    with open(artifacts_dir / "02_pluscal_impl.json") as f:
        raw_impl = json.load(f)
    impl = PlusCalImpl.model_validate(raw_impl)

    # ── Paths ────────────────────────────────────────────────────────────────
    rtl_dir = artifacts_dir / "rtl"
    rtl_dir.mkdir(exist_ok=True)
    output_path = artifacts_dir / "03_rtl_output.json"

    # If a previous failed attempt saved lint errors, inject them.
    prior_lint_errors: list[str] = []
    if attempt > 0 and output_path.exists():
        try:
            prev = json.loads(output_path.read_text())
            prior_lint_errors = prev.get("error_log", [])
        except Exception:
            pass

    # ── Call Claude ──────────────────────────────────────────────────────────
    print(f"[Stage 3] Calling Claude (attempt {attempt + 1}/{MAX_RETRIES}) ...")
    try:
        raw_response = call_claude(
            system=_SYSTEM_PROMPT,
            user=_build_user_prompt(
                impl,
                lint_errors=prior_lint_errors if attempt > 0 else None,
            ),
            max_tokens=4096,
            temperature=0.0,
        )
        parsed = _parse_response(raw_response)
    except Exception as exc:
        return _write_failure(
            state, output_path, rtl_dir, run_id, impl,
            retry_counts, attempt,
            error=f"LLM call or JSON parse failed: {exc}",
        )

    # ── Extract fields ───────────────────────────────────────────────────────
    try:
        top_module_name: str = parsed["top_module_name"]
        verilog_content: str = parsed["verilog_content"]
        compilation_path: str = parsed.get("compilation_path", "direct_structural")
        assumptions_made: list[str] = parsed.get("assumptions_made", [])
    except KeyError as exc:
        return _write_failure(
            state, output_path, rtl_dir, run_id, impl,
            retry_counts, attempt,
            error=f"Missing required key in LLM response: {exc}",
        )

    port_list_raw: list[dict] = parsed.get("port_list", [])
    port_list = [
        PortEntry(
            name=pe["name"],
            direction=pe["direction"],
            width=int(pe.get("width", 1)),
            description=pe.get("description", ""),
        )
        for pe in port_list_raw
    ]

    # ── Write Verilog file ───────────────────────────────────────────────────
    verilog_path = rtl_dir / f"{impl.design_name}.v"
    verilog_path.write_text(verilog_content)
    print(f"[Stage 3] Wrote {verilog_path}")

    # ── Lint ─────────────────────────────────────────────────────────────────
    lint_tool_name = _detect_lint_tool()
    compilation_log: list[str] = []

    if lint_tool_name is None:
        lint_passed = True  # cannot lint — treat as passed, record assumption
        lint_tool_used = "none"
        assumptions_made.append("Lint skipped: neither verilator nor iverilog found on PATH")
        compilation_log.append("WARNING: lint skipped — no lint tool available")
        print("[Stage 3] No lint tool found; skipping lint")
    else:
        lint_passed, lint_messages = _run_lint(verilog_content, top_module_name, lint_tool_name)
        lint_tool_used = lint_tool_name
        compilation_log.extend(lint_messages)
        if lint_passed:
            print(f"[Stage 3] Lint passed ({lint_tool_name})")
        else:
            print(f"[Stage 3] Lint FAILED ({lint_tool_name}): {lint_messages}")

    # ── On lint failure, request a retry if budget allows ────────────────────
    if not lint_passed:
        lint_errors_for_retry = [
            m for m in compilation_log
            if "Error" in m or "error" in m or "Warning" in m or "warning" in m
        ] or compilation_log

        # Write partial output so the next attempt can read the lint errors.
        partial = RTLOutput(
            run_id=run_id,
            status="partial",
            design_name=impl.design_name,
            compilation_path=compilation_path,
            bsv_source_path=None,
            verilog_path=str(verilog_path),
            top_module_name=top_module_name,
            port_list=port_list,
            lint_passed=False,
            lint_tool=lint_tool_used,
            compilation_log=compilation_log,
            assumptions_made=assumptions_made,
            error_log=lint_errors_for_retry,
        )
        output_path.write_text(partial.model_dump_json(indent=2))

        retry_counts["stage3"] = attempt + 1
        should_halt = retry_counts["stage3"] >= MAX_RETRIES
        print(
            f"[Stage 3] Lint failure recorded. "
            f"{'Halting after max retries.' if should_halt else 'Will retry with lint errors injected.'}"
        )
        return {**state, "retry_counts": retry_counts, "halt": should_halt}

    # ── Success ───────────────────────────────────────────────────────────────
    compilation_log.insert(0, f"Verilog generated by Claude Sonnet 4.6, path={compilation_path}")

    output = RTLOutput(
        run_id=run_id,
        status="success",
        design_name=impl.design_name,
        compilation_path=compilation_path,
        bsv_source_path=None,
        verilog_path=str(verilog_path),
        top_module_name=top_module_name,
        port_list=port_list,
        lint_passed=True,
        lint_tool=lint_tool_used,
        compilation_log=compilation_log,
        assumptions_made=assumptions_made,
        error_log=[],
    )

    output_path.write_text(output.model_dump_json(indent=2))
    print(f"[Stage 3] Wrote {output_path}")

    retry_counts["stage3"] = 0
    return {**state, "retry_counts": retry_counts, "halt": False}


# Failure helper
def _write_failure(
    state: PipelineState,
    output_path: Path,
    rtl_dir: Path,
    run_id: str,
    impl: PlusCalImpl,
    retry_counts: dict,
    attempt: int,
    error: str,
) -> PipelineState:
    retry_counts["stage3"] = attempt + 1
    should_halt = retry_counts["stage3"] >= MAX_RETRIES

    print(
        f"[Stage 3] FAILURE (attempt {attempt + 1}): {error}  "
        f"{'Halting.' if should_halt else 'Will retry.'}"
    )

    output = RTLOutput(
        run_id=run_id,
        status="failed",
        design_name=impl.design_name,
        compilation_path="direct_structural",
        bsv_source_path=None,
        verilog_path=str(rtl_dir / f"{impl.design_name}.v"),
        top_module_name=impl.design_name,
        port_list=[],
        lint_passed=False,
        lint_tool="none",
        compilation_log=[],
        assumptions_made=[],
        error_log=[error],
    )
    output_path.write_text(output.model_dump_json(indent=2))

    return {**state, "retry_counts": retry_counts, "halt": should_halt}