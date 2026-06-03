"""
Agent 3 — Formal specification author and rule picker.

One persona / system prompt / knowledge base; four entry points:

    generate_formal_spec(summary)                -> FormalSpec
    revise_on_tlc(spec, tlc_errors)             -> FormalSpec
    pick_rule(applicable_rules, spec)            -> {"rule_name": str, "params": dict}
    revise_on_cocotb(spec, sim_log)             -> FormalSpec

SDK choice: Anthropic Python SDK (`anthropic` package) speaking directly to
Anthropic's /v1/messages endpoint using ANTHROPIC_API_KEY from .env.
This is the actual runtime backing of the Claude Agent SDK pattern.

NOTE: `pick_rule` is a ONE-SHOT structured-output call with NO tools and NO
internal loop. Giving it tools would break the bounded-action-space invariant.
See docs/handoff_runtime_agents.md §5.

Key detection: if ANTHROPIC_API_KEY is the placeholder value or is missing, a
clear, actionable error is raised at call time so stages 1-2 can still run
before the key is configured.
"""

from __future__ import annotations

import json
import os
from typing import Any

try:
    import anthropic as _anthropic_module
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

from pipeline.schemas.summary_schema import SpecSummary
from pipeline.schemas.tla_schema import FormalSpec

# ---------------------------------------------------------------------------
# Key validation
# ---------------------------------------------------------------------------

_PLACEHOLDER_SENTINEL = "__AGENT3_CLAUDE_AGENT_SDK_KEY__NOT_CONFIGURED_YET__"


def _get_api_key() -> str:
    """
    Return the ANTHROPIC_API_KEY, raising a clear error if it is not configured.

    Raises:
        RuntimeError: if the key is missing or is the placeholder sentinel.
        ImportError: if the anthropic package is not installed.
    """
    if not _ANTHROPIC_AVAILABLE:
        raise ImportError(
            "Agent 3 requires the 'anthropic' package. "
            "Install it with: pip install anthropic"
        )
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key or key == _PLACEHOLDER_SENTINEL or key.startswith("replace-with"):
        raise RuntimeError(
            "Agent 3 Claude Agent SDK key not configured — "
            "set ANTHROPIC_API_KEY in .env to a real Anthropic API key. "
            f"Current value is placeholder: {key!r}"
        )
    return key


# ---------------------------------------------------------------------------
# Client factory — instantiated lazily so import never fails
# ---------------------------------------------------------------------------

_client: Any | None = None


def _get_client() -> Any:
    """Return a cached anthropic.Anthropic client, creating one if needed."""
    global _client
    if _client is None:
        key = _get_api_key()
        _client = _anthropic_module.Anthropic(api_key=key)
    return _client


# ---------------------------------------------------------------------------
# Model constant
# ---------------------------------------------------------------------------

# Agent 3 runs on its OWN dedicated Anthropic model via the Anthropic SDK
# (locked decision #3 / BUG-3: Agent 3 is a distinct, tool-using Claude agent).
# This is intentionally SEPARATE from the proxy's LLM_MODEL used by Agent 1 —
# do not collapse the two. The AGENT3_MODEL env var is the primary way to set
# this; the literal below is only a sane default if the env var is unset.
_DEFAULT_AGENT3_MODEL = "claude-opus-4-5"
_AGENT3_MODEL = os.environ.get("AGENT3_MODEL") or _DEFAULT_AGENT3_MODEL


# ---------------------------------------------------------------------------
# Shared persona / system prompt (reused across all call types for caching)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a formal hardware specification engineer specialising in TLA+ and
refinement-calculus-guided RTL design.

Your knowledge base:
- TLA+ semantics: VARIABLES, Init, Next, invariants, action composition
- The JSON(TLA) intermediate representation used by this pipeline:
    {
      "module_name": str,
      "description": str,
      "variables": {
        "<name>": {"type": "Nat|Bit", "width": <int>}
      },
      "initial": {"<name>": "<expression>"},
      "transitions": [
        {
          "label": str,
          "condition": str,       // plain English: AND, OR, NOT
          "updates": {"<name>": "<next-value expression>"}
        }
      ],
      "invariants": [str]
    }
- Refinement calculus rules: Initialization, Iteration, SequentialComposition,
  Assignment, Alternation, IntroduceVariable.
- Verilog-2001 constraints: no SystemVerilog keywords, always @(posedge clk)
  for clocked logic, always @(*) for combinational logic.

When generating or revising a FormalSpec:
- Every variable in "variables" must appear in "initial" and in every
  transition's "updates" dict (use the current value expression if unchanged).
- Invariants must be expressed as plain-English boolean clauses.
- Conditions use AND, OR, NOT — Compiler 1 translates to TLA+ syntax.

Respond ONLY with the requested JSON object — no markdown fences, no commentary.
"""

# ---------------------------------------------------------------------------
# Tool definitions for spec-authoring call types
# (pick_rule deliberately receives NO tools)
# ---------------------------------------------------------------------------

_TOOLS: list[dict] = [
    {
        "name": "read_artifact",
        "description": (
            "Read a pipeline artifact file from disk. "
            "Use to retrieve previously generated specs or logs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the artifact file.",
                }
            },
            "required": ["path"],
        },
    },
]


def _handle_tool_call(tool_name: str, tool_input: dict) -> str:
    """Execute a tool call and return the result as a string."""
    if tool_name == "read_artifact":
        path = tool_input.get("path", "")
        try:
            with open(path) as f:
                return f.read()
        except OSError as exc:
            return f"ERROR reading {path}: {exc}"
    return f"ERROR: unknown tool '{tool_name}'"


# ---------------------------------------------------------------------------
# Internal agentic loop (tool-using call types)
# ---------------------------------------------------------------------------

def _run_with_tools(user_message: str) -> str:
    """
    Run an agentic tool-use loop with ANTHROPIC_API_KEY and the Agent 3 model.

    Keeps calling the model until it returns a stop_reason of 'end_turn'
    (no more tool calls pending). Returns the final text content.

    Used by: generate_formal_spec, revise_on_tlc, revise_on_cocotb.
    NOT used by: pick_rule (which has no tools and no loop).
    """
    client = _get_client()
    messages: list[dict] = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model=_AGENT3_MODEL,
            max_tokens=4096,
            temperature=0.0,
            system=_SYSTEM_PROMPT,
            tools=_TOOLS,
            messages=messages,
        )

        # Collect text and tool_use blocks from the response
        tool_calls = []
        text_parts: list[str] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(block)

        if not tool_calls:
            # No tool calls — model is done
            return "".join(text_parts).strip()

        # Append assistant turn with the mixed content
        messages.append({"role": "assistant", "content": response.content})

        # Execute each tool call and append results
        tool_results = []
        for tc in tool_calls:
            result_text = _handle_tool_call(tc.name, tc.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": result_text,
            })
        messages.append({"role": "user", "content": tool_results})

        # Guard: if stop_reason was end_turn despite tool_calls somehow, exit
        if response.stop_reason == "end_turn":
            return "".join(text_parts).strip()


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def generate_formal_spec(summary: SpecSummary) -> FormalSpec:
    """
    Generate a FormalSpec (JSON(TLA)) from a SpecSummary (JSON(S)).

    Tool-using call. Agent may read artifacts if needed.

    Returns:
        FormalSpec validated by Pydantic v2.
    """
    user_message = f"""\
Generate a complete FormalSpec JSON(TLA) object for the following hardware module.

SpecSummary:
{summary.model_dump_json(indent=2)}

Return a single JSON object matching the FormalSpec schema. No other text.
"""
    raw = _run_with_tools(user_message)
    data = json.loads(raw)
    return FormalSpec.model_validate(data)


def revise_on_tlc(spec: FormalSpec, tlc_errors: str) -> FormalSpec:
    """
    Revise a FormalSpec in response to TLC model-checker errors.

    Tool-using call.

    Args:
        spec: The FormalSpec that TLC rejected.
        tlc_errors: Full TLC stderr output.

    Returns:
        Revised FormalSpec validated by Pydantic v2.
    """
    user_message = f"""\
The following FormalSpec caused TLC errors. Revise it to fix the errors.

Current FormalSpec:
{spec.model_dump_json(indent=2)}

TLC errors:
{tlc_errors}

Return a single corrected JSON object matching the FormalSpec schema. No other text.
"""
    raw = _run_with_tools(user_message)
    data = json.loads(raw)
    return FormalSpec.model_validate(data)


def pick_rule(applicable_rules: list[dict], spec: dict, *, system_prompt: str | None = None) -> dict:
    """
    Choose which refinement rule to apply next.

    BOUNDED-ACTION-SPACE INVARIANT: this call uses NO tools and NO internal
    loop. The model must return a structured choice in one shot.

    See docs/handoff_runtime_agents.md §5.

    Args:
        applicable_rules: List of rule descriptor dicts, each with at least:
            {"name": str, "describe": str}
            (these are the keys the refinement engine actually sends — see
            pipeline/refinement/engine.py where it builds
            {"name": r.__class__.__name__, "describe": r.describe()}).
        spec: Current refinement engine spec dict.

    Returns:
        {"rule_name": str, "params": dict}
        where rule_name is one of the names in applicable_rules.
    """
    client = _get_client()

    rules_text = json.dumps(applicable_rules, indent=2)
    spec_text = json.dumps(spec, indent=2)

    user_message = f"""\
You are choosing the next refinement rule to apply to a hardware specification.

Current spec:
{spec_text}

Applicable rules (choose exactly one):
{rules_text}

Return a JSON object with exactly these fields:
  "rule_name": the name of the chosen rule (must match one of the names above)
  "params": a dict of parameters for that rule (may be empty dict if none needed)

No other text, no markdown. Return only the JSON object.
"""

    # NO tools — this is a one-shot structured-output call.
    # The `tools` argument is OMITTED entirely (not passed as []): the Anthropic
    # API rejects an explicitly empty tools list, and omitting it is what actually
    # enforces the bounded-action-space invariant (no tool surface on pick_rule).
    response = client.messages.create(
        model=_AGENT3_MODEL,
        max_tokens=512,
        temperature=0.0,
        system=system_prompt if system_prompt is not None else _SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = ""
    for block in response.content:
        if block.type == "text":
            raw += block.text

    raw = raw.strip()
    result = json.loads(raw)

    # Validate shape
    if "rule_name" not in result or "params" not in result:
        raise ValueError(
            f"pick_rule response missing required fields. Got: {result!r}"
        )
    if not isinstance(result["params"], dict):
        raise TypeError(
            f"pick_rule 'params' must be a dict, got {type(result['params'])}"
        )

    return {"rule_name": result["rule_name"], "params": result["params"]}


def revise_on_cocotb(spec: FormalSpec, sim_log: str) -> FormalSpec:
    """
    Revise a FormalSpec in response to a failing cocotb simulation.

    Tool-using call.

    Args:
        spec: The FormalSpec whose generated RTL failed cocotb.
        sim_log: Full cocotb simulation log / assertion errors.

    Returns:
        Revised FormalSpec validated by Pydantic v2.
    """
    user_message = f"""\
The RTL generated from the following FormalSpec failed a cocotb testbench.
Revise the FormalSpec to fix the behavioral issue.

Current FormalSpec:
{spec.model_dump_json(indent=2)}

Cocotb simulation log:
{sim_log}

Return a single corrected JSON object matching the FormalSpec schema. No other text.
"""
    raw = _run_with_tools(user_message)
    data = json.loads(raw)
    return FormalSpec.model_validate(data)
