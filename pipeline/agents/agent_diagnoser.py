"""
Diagnoser Agent — classifies a cocotb failure as a spec fault or a refinement fault.

Reads:
    artifacts/<run_id>/04_evaluation.json   (phase, failed_vectors, raw)
    artifacts/<run_id>/02_formal_spec.json  (the FormalSpec before refinement)
    artifacts/<run_id>/refinement_chain.json (the rule applications that were made)

Returns:
    {"failure_type": "spec" | "refinement", "explanation": str}

Build failures (phase == "build") are classified immediately as "spec" without
an LLM call — the Verilog is malformed and a fresh spec + refinement is the only
viable recovery path.

Test failures (phase == "test") require LLM reasoning to distinguish:

  "spec"        — the FormalSpec describes the wrong behavior. The core logic,
                  variables, or transitions are incorrect. The refinement did the
                  right thing with bad input. Fix: Agent 3 revises the FormalSpec.

  "refinement"  — the FormalSpec is correct but the refinement engine applied a
                  rule with wrong parameters (wrong reset values, wrong action in
                  a clock domain, wrong update expressions). The RTL structure is
                  correct but specific values or mappings are off.
                  Fix: backtrack the refinement chain and re-run the engine.

SDK: OpenAI-compatible via LLM_BASE_URL / LLM_API_KEY / LLM_MODEL.
temperature=0.0, response_format={"type": "json_object"}.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import openai

_client: openai.OpenAI | None = None


def _get_client() -> openai.OpenAI:
    global _client
    if _client is None:
        _client = openai.OpenAI(
            base_url=os.environ["LLM_BASE_URL"],
            api_key=os.environ["LLM_API_KEY"],
        )
    return _client


_SYSTEM_PROMPT = """\
You are a hardware design debugging analyst specialising in formal verification
and refinement-calculus-based RTL synthesis.

The pipeline works as follows:
  1. Agent 3 produces a FormalSpec (JSON(TLA)) — a structured description of the
     hardware's variables, initial values, transitions, and invariants.
  2. The Refinement Engine applies a sequence of rules (Initialization, Iteration,
     Assignment, Alternation, SequentialComposition, IntroduceVariable) to transform
     the abstract FormalSpec into an RTL-style spec with concrete clock domains,
     reset values, and register assignments.
  3. Compiler 2 translates the RTL-style spec into synthesizable Verilog-2001.
  4. A cocotb testbench (assumed correct) verifies the Verilog against test vectors.

You will be shown the failure report, the FormalSpec, and the refinement chain.
Classify the root cause as exactly one of:

"spec" — The FormalSpec itself describes the wrong behavior. Signs:
  - Test vectors expect fundamentally different logic than what the spec describes.
  - The spec is missing a variable, transition, or initial value.
  - The core operation is wrong (e.g. incrementing when the test expects shifting).
  - Wrong port direction or missing enable/reset signal in the spec.

"refinement" — The FormalSpec is correct but the refinement engine applied a rule
with wrong parameters. Signs:
  - The RTL structure is correct (right ports, right module shape, right operation
    type) but specific constants or expressions are wrong.
  - Reset value is off (e.g. resets to 1 instead of 0).
  - Counter wraps at the wrong modulus.
  - A register is combinational when it should be clocked, or vice versa.
  - A wrong variable was marked as the reset action target.

Respond ONLY with this JSON object — no markdown, no explanation outside it:
{
  "failure_type": "spec" or "refinement",
  "explanation": "One or two sentences explaining which artifact is at fault and why."
}
"""


def diagnose(run_id: str) -> dict:
    """
    Classify a cocotb failure as a spec fault or refinement fault.

    Args:
        run_id: The pipeline run identifier (used to locate artifacts/).

    Returns:
        {"failure_type": "spec" | "refinement", "explanation": str}

    Never raises — falls back to {"failure_type": "spec", ...} on any error so
    the pipeline always has a valid routing signal.
    """
    artifact_dir = Path("artifacts") / run_id

    # Load evaluation result
    eval_path = artifact_dir / "04_evaluation.json"
    try:
        eval_data = json.loads(eval_path.read_text())
    except Exception as exc:
        return {
            "failure_type": "spec",
            "explanation": f"Could not read 04_evaluation.json ({exc}); defaulting to spec revision.",
        }

    phase = eval_data.get("phase", "test")

    # Build failures: Verilog didn't compile — fresh spec + refinement is the
    # only viable recovery since Compiler 2 is deterministic.
    if phase == "build":
        return {
            "failure_type": "spec",
            "explanation": (
                f"Verilog build failed (iverilog error): {eval_data.get('error', '')}. "
                "Routing to spec revision so the full pipeline reruns with a fresh spec."
            ),
        }

    # Load FormalSpec and refinement chain for LLM context
    formal_path = artifact_dir / "02_formal_spec.json"
    try:
        formal_data = json.loads(formal_path.read_text())
    except Exception:
        formal_data = {}

    chain_path = artifact_dir / "refinement_chain.json"
    try:
        chain_data = json.loads(chain_path.read_text())
    except Exception:
        chain_data = []

    # Format the failure context for the LLM
    failed_vectors = eval_data.get("failed_vectors", [])
    raw_log = eval_data.get("raw", "")[:2000]

    user_message = f"""\
=== SIMULATION FAILURE ===
Phase: {phase}
Summary: {eval_data.get("error", "unknown")}

Failed test vectors ({len(failed_vectors)} total):
{json.dumps(failed_vectors, indent=2)}

Raw simulation output (truncated to 2000 chars):
{raw_log}

=== FORMAL SPEC (JSON(TLA)) ===
{json.dumps(formal_data, indent=2)}

=== REFINEMENT CHAIN ({len(chain_data)} steps applied) ===
{json.dumps(chain_data, indent=2)}

Classify the root cause. Return the JSON object only.
"""

    try:
        client = _get_client()
        model = os.environ["LLM_MODEL"]
        response = client.chat.completions.create(
            model=model,
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user_message},
            ],
        )
        raw = response.choices[0].message.content or ""
        result = json.loads(raw)
        if result.get("failure_type") not in ("spec", "refinement"):
            result["failure_type"] = "spec"
        return result
    except Exception as exc:
        return {
            "failure_type": "spec",
            "explanation": f"Diagnoser LLM call failed ({exc}); defaulting to spec revision.",
        }
