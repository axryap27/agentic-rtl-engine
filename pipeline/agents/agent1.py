"""
Agent 1 — Natural language prompt → SpecSummary (JSON(S)).

One-shot LLM call. No tools. No retry loop (retries are driven externally by
the LangGraph node that calls this function).

SDK: OpenAI-compatible via LLM_BASE_URL / LLM_API_KEY / LLM_MODEL from .env.
temperature=0.0, response_format={"type":"json_object"}.

System prompt is built once and reused across all calls from the same process
so the proxy can cache it.
"""

import json
import os

import openai
from pydantic import ValidationError

from pipeline.schemas.summary_schema import SpecSummary
from pipeline.usage import log_usage

# ---------------------------------------------------------------------------
# LLM client — instantiated once, shared across all calls in this process.
# ---------------------------------------------------------------------------

_client: openai.OpenAI | None = None


def _get_client() -> openai.OpenAI:
    global _client
    if _client is None:
        _client = openai.OpenAI(
            base_url=os.environ["LLM_BASE_URL"],
            api_key=os.environ["LLM_API_KEY"],
        )
    return _client


# ---------------------------------------------------------------------------
# System prompt — built once at module load so proxy caching is stable.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a hardware-design specification analyst.

Your task is to parse a natural language hardware description and return a
structured JSON object that exactly matches this schema:

{
  "module_name": "<snake_case identifier for the hardware module>",
  "description": "<concise plain-English description of the module's behavior>",
  "ports": [
    {
      "name": "<port name>",
      "direction": "<'input' or 'output'>",
      "width": <integer bit width>
    }
  ],
  "test_vectors": [
    {
      "inputs": {"<port_name>": <integer value>},
      "expected": {"<port_name>": <integer value>}
    }
  ],
  "reset_port": "<name of reset port, or null if none>",
  "reset_active_low": <true if reset asserts at 0, false otherwise>
}

Rules:
- Every output port in the design must appear in at least one test vector's
  "expected" dict.
- Every input port (except clk and reset) must appear in at least one test
  vector's "inputs" dict.
- Include clk as an input port with width 1 if the design is clocked.
- CLOCK CONTRACT: the test harness owns the clock. Each test vector corresponds
  to EXACTLY ONE rising clock edge applied by the harness. Do NOT model clock
  phases by toggling clk across vectors (e.g. 0,1,0,1) — the harness drives the
  clock automatically and ignores any clk value you provide.
- If you include clk in a vector's "inputs", you MUST hold it constant at 1 in
  every vector. Never use 0, and never alternate it.
- "expected" is the value of each output sampled AFTER that single rising edge.
  For a state machine or counter, this means the design advances by exactly one
  step per vector; write the "expected" sequence as the post-edge values for one
  step per vector (do NOT assume the design advances every other vector).
- MEMORY / REGISTER FILE designs: if the design has an internal addressable
  memory array (a register file or RAM), that array is INTERNAL storage, NOT a
  port — do not list it in "ports" (only the address/data/enable inputs and the
  read-data output are ports). A REGISTERED read port has ONE cycle of latency:
  the read-data observed AFTER edge i reflects the memory cell at the read address
  as sampled at edge i — i.e. it reflects writes from cycles strictly BEFORE i
  (read-before-write: if you write and read the SAME address in the same vector,
  the read output shows the OLD value that vector and the NEW value the NEXT
  vector). The memory powers up UNINITIALISED and is NOT reset, so a read of any
  address that was not written in a strictly-earlier vector is X (undefined): that
  vector's "expected" dict MUST be EMPTY (do not assert the read output for an
  uninitialised cell — this is the one case where an output is omitted from
  "expected"). Begin with at least one warm-up write whose read targets an
  as-yet-unwritten cell (empty "expected"), and only assert the read output once
  it reads a cell written in an earlier vector.
- Do NOT include explanatory text outside the JSON object.
- Respond ONLY with the JSON object — no markdown, no commentary.
"""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run(nl_prompt: str) -> SpecSummary:
    """
    Convert a natural-language hardware description to a validated SpecSummary.

    Args:
        nl_prompt: Free-form natural language description of the hardware module.

    Returns:
        SpecSummary validated by Pydantic v2.

    Raises:
        openai.OpenAIError: on LLM API failure.
        pydantic.ValidationError: if the model returns JSON that doesn't match
            the SpecSummary schema.
        json.JSONDecodeError: if the model returns non-JSON text despite the
            json_object response format.
    """
    client = _get_client()
    model = os.environ["LLM_MODEL"]

    response = client.chat.completions.create(
        model=model,
        temperature=0.0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": nl_prompt},
        ],
    )

    # Record token usage to the local ledger (never raises; see pipeline/usage.py).
    log_usage(agent="agent1", model=model, usage=getattr(response, "usage", None))

    raw_text = response.choices[0].message.content or ""
    data = json.loads(raw_text)
    return SpecSummary.model_validate(data)
