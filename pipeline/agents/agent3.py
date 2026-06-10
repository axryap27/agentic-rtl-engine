"""
Agent 3 — Formal specification author and rule picker.

One persona / system prompt / knowledge base; five entry points:

    generate_formal_spec(summary)                -> FormalSpec
    revise_on_tlc(spec, tlc_errors)             -> FormalSpec
    pick_rule(applicable_rules, spec)            -> {"rule_name": str, "params": dict}
    revise_on_cocotb(spec, sim_log)             -> FormalSpec
    critique_refinement(abstract, concrete, ...) -> {"verdict": str, "issues": [...], "reasoning": str}

SDK choice: Anthropic Python SDK (`anthropic` package) speaking directly to
Anthropic's /v1/messages endpoint using ANTHROPIC_API_KEY from .env.
This is the actual runtime backing of the Claude Agent SDK pattern.

NOTE: `pick_rule` is a ONE-SHOT structured-output call with NO tools and NO
internal loop. Giving it tools would break the bounded-action-space invariant.
See docs/agents.md (the bounded-action-space invariant).

NOTE: `critique_refinement` is ALSO a one-shot structured-output call with NO
tools — it is a pure read-only refinement-correctness critic that returns an
accept/reject verdict. It is the runtime backing of pass6_checker: pass6 cannot
work as an engine pass (it has no rule to "pick"), so it runs here as its own
gating critic call. The no-tool design here is about keeping the critic a simple,
mockable verdict function; the bounded-action-space invariant proper is still
about `pick_rule` only.

Key detection: if ANTHROPIC_API_KEY is the placeholder value or is missing, a
clear, actionable error is raised at call time so stages 1-2 can still run
before the key is configured.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

try:
    import anthropic as _anthropic_module
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

from pipeline.schemas.summary_schema import SpecSummary
from pipeline.schemas.tla_schema import FormalSpec
from pipeline.usage import log_usage, check_budget
from pipeline.refinement_templates import pass6_checker

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
# messages.create wrapper — adapt to models that dropped `temperature`
# ---------------------------------------------------------------------------
# Some models (e.g. Claude Opus 4.8) have DEPRECATED the `temperature` parameter
# and return 400 if it is sent. We don't hardcode the list: the first time a
# model rejects temperature we strip it, retry, and remember the model so later
# calls omit it up front. A 400 is not billed, so the one-time probe is free.
# This preserves temperature=0.0 (determinism) for models that still accept it.
_NO_TEMPERATURE: set[str] = set()


def _create(client: Any, **kwargs: Any) -> Any:
    """client.messages.create that adapts to models lacking `temperature`."""
    model = kwargs.get("model", "")
    if model in _NO_TEMPERATURE:
        kwargs.pop("temperature", None)
    try:
        return client.messages.create(**kwargs)
    except _anthropic_module.APIStatusError as exc:
        message = str(getattr(exc, "message", "") or exc).lower()
        if (getattr(exc, "status_code", None) == 400
                and "temperature" in message and "temperature" in kwargs):
            _NO_TEMPERATURE.add(model)
            kwargs.pop("temperature", None)
            return client.messages.create(**kwargs)
        raise


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
# Budget guard — refuse calls before cumulative Agent 3 spend hits the cap.
# Reads the usage ledger via pipeline.usage.check_budget. Cap + reserve are
# read from the environment on every check so they can be tuned in .env without
# code edits (defaults: $100 total, $0.50 pre-flight reserve for the in-flight
# call that hasn't been logged yet). check_budget raises usage.BudgetExceeded,
# which propagates to the stage node and becomes a status=error artifact.
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return default


def _check_budget() -> None:
    """Raise usage.BudgetExceeded if Agent 3 is at/over its USD budget."""
    check_budget(
        "agent3",
        _env_float("AGENT3_BUDGET_USD", 100.0),
        _env_float("AGENT3_BUDGET_RESERVE_USD", 0.50),
    )


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
          "updates": {"<name>": "<next-value expression>"},
          "combinational": bool   // optional, default false; true = a CONTINUOUS
                                  // assign (wire), not a clocked register update
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
- In conditions AND in update expressions, use SYMBOLIC comparison operators:
  `=` (equal), `/=` (not equal), `<`, `>`, `<=`, `>=`. Do NOT write English
  comparison words like "equals", "less than", or "greater than" — they are not
  parsed and will produce broken RTL. Boolean connectives may stay as AND/OR/NOT.
  Example: write `count < 3` and `count = 3`, never `count less than 3`.
- For ARITHMETIC, also use symbolic operators: `%` for modulo (e.g.
  `(acc + din) % 256`), `+ - * /` as usual. NEVER write the word `mod` — it is
  not translated and produces broken RTL plus a phantom input port. Write
  `(acc + din) % 256`, never `(acc + din) mod 256`.
- Do NOT declare the clock or the reset signal as state variables. The clock is
  implicit (every clocked update happens on the rising edge) and the reset is
  handled by the pipeline. Model only the true state registers of the design;
  reference the reset only inside a guard if needed, never as a "variables" entry
  or an "updates" target.
- Do NOT declare DATA or CONTROL INPUTS as state variables either. An enable, a
  mode/op select, a data-in bus, or ANY signal the environment drives INTO the
  design is a FREE INPUT: reference it inside guards and update expressions, but
  never list it under "variables" or "initial", and never make it an "updates"
  target. Only signals the design itself registers and drives — its outputs and
  genuine internal state — belong in "variables". A free input declared as a
  variable is emitted as an `output reg`, so the design can never receive it
  (e.g. an accumulator whose `din` becomes an output can never be fed data).
  LITMUS TEST: if a signal's update in every action is just itself ("x" -> "x")
  — it only ever holds its own value and is never computed from other state —
  then it is an input you wrongly promoted to a variable; remove it from
  "variables"/"initial"/"updates" and reference it as a free input instead.
- For a MEMORY / register file / RAM (an addressable array of words), declare the
  storage as a SINGLE variable with a "depth" field:
    "mem": {"type": "Nat", "width": <word bits>, "depth": <number of words>}
  This is an array of `depth` words, each `width` bits. To WRITE one element,
  put the index INSIDE the update KEY in brackets:
    a write transition's updates = {"mem[waddr]": "wdata"}   (write wdata to mem[waddr])
  To READ one element, reference `mem[raddr]` in an update EXPRESSION, e.g. a
  registered read port: {"rdata": "mem[raddr]"}. The addresses (`waddr`,
  `raddr`), the write data (`wdata`), and the write-enable (`we`) are FREE INPUTS
  — never list them under "variables". A memory is NOT reset and NOT initialised:
  omit it from "initial", and do NOT give it a reset value or list it in the reset
  transition (only its read-port register, e.g. `rdata`, resets). Model the write
  and the read as SEPARATE transitions (a we-gated write `we = 1`, and an
  unconditional read `TRUE`). The read port is a REGISTERED read (a clocked
  register with one cycle of latency) — `rdata` is genuine clocked state, never a
  combinational/continuous signal.
- WHEN REFINING a register file (choosing rules): apply Iteration to BOTH the
  write transition AND the read transition — both are clocked registers, so both
  must be made clocked (the memory write and the read register `rdata` alike).
  Keep a memory-element write as a SINGLE flat indexed update
  (`{"mem[waddr]": "wdata"}`); do NOT split it with Alternation or
  SequentialComposition (the we-gate rides inside the update, exactly like an
  enable in any other register). Never put the memory in reset_values.
- COMBINATIONAL OUTPUTS. An output that must reflect CURRENT-cycle state with NO
  clock latency — a FIFO `full`/`empty` flag, a comparator result, a decoder
  output, a derived valid/ready — is a CONTINUOUS ASSIGNMENT (a wire), not a
  register. Put it in its OWN transition marked `"combinational": true` with
  `"condition": "TRUE"`, e.g.
    {"label": "Flags", "condition": "TRUE", "combinational": true,
     "updates": {"full": "count = 4", "empty": "count = 0"}}
  A combinational target is a WIRE: never list it in "initial", never give it a
  reset value or list it in the reset transition, and NEVER apply Iteration,
  Alternation, or SequentialComposition to a combinational transition. A
  REGISTERED output (one cycle of latency, e.g. a FIFO `dout`) stays an ordinary
  clocked transition. If a flag that should be combinational is left as an
  ordinary (clocked) output it silently lags by a cycle and the design fails
  verification — so mark every such flag `"combinational": true`.
- MULTI-WAY NEXT-STATE. Express any multi-way next value (e.g. an occupancy
  counter) as ONE FLAT else-if priority chain:
    IF g1 THEN e1 ELSE IF g2 THEN e2 ELSE <hold>
  with compound AND guards. NEVER write a mid-expression conditional such as
  `count + (IF wr THEN 1 ELSE 0) - (IF rd THEN 1 ELSE 0)`, and NEVER nest an IF
  inside a THEN branch — the compiler only translates an IF in the leading/ELSE
  position and leaks an embedded IF as broken RTL. Put every branch in the ELSE
  chain instead.
- FIFO RECIPE (a register file plus flow control): a we-gated write
  `{"mem[wptr]": "din", "wptr": "(wptr + 1) % DEPTH"}` guarded `wr_en = 1 AND
  full = 0`; a registered read `{"dout": "mem[rptr]", "rptr": "(rptr + 1) %
  DEPTH"}` guarded `rd_en = 1 AND empty = 0`; an occupancy `count` updated by a
  flat else-if chain (simultaneous read+write holds count, write-only +1,
  read-only -1, else hold); a COMBINATIONAL Flags transition for `full`
  (`count = DEPTH`) and `empty` (`count = 0`). Reset clears wptr/rptr/count/dout
  but NOT the memory and NOT the flags. When refining, apply Iteration to the
  write, read, and counter transitions; leave the combinational Flags transition
  alone.
- FSMD — A CONTROL FSM SEQUENCING A MULTI-CYCLE DATAPATH (e.g. a sequential
  multiplier/divider, a serial CRC, any start/done compute that takes several
  clocks). Model it as ONE clocked transition whose `updates` advance EVERY
  register together each clock, plus a combinational `done` flag. Use a `state`
  register with INTEGER-encoded states (IDLE=0, BUSY=1, DONE=2 — never symbolic
  names) and a `count` register for the iteration counter. The handshake and the
  per-state datapath all ride inside the flat else-if guard chains:
    state':  IF (state = 0 OR state = 2) AND start = 1 THEN 1   (start in IDLE or DONE -> BUSY)
             ELSE IF state = 1 AND count = 1 THEN 2             (last cycle -> DONE)
             ELSE IF state = 1 THEN 1                           (stay BUSY)
             ELSE IF state = 2 THEN 0 ELSE 0                    (DONE w/o start -> IDLE; else idle)
  CRITICAL HANDSHAKE RULE: the load/restart guard must accept start when NOT BUSY
  — i.e. in IDLE *or* DONE: `(state = 0 OR state = 2) AND start = 1`. DONE is a
  single cycle, so a start pulse that coincides with the previous result's DONE
  must RELOAD, not be dropped. An IDLE-only guard (`state = 0 AND start = 1`)
  silently swallows a back-to-back start that lands in DONE and the next operation
  never runs — do NOT write the load guard that way.
  Each datapath register LOADS on the start branch (`(state = 0 OR state = 2) AND
  start = 1`), STEPS on the BUSY branch (`state = 1 ...`), and HOLDS otherwise —
  e.g. an iteration counter `count`: `IF (state = 0 OR state = 2) AND start = 1
  THEN <N> ELSE IF state = 1 THEN count - 1 ELSE count`. Make `done` COMBINATIONAL: a Flags
  transition `{"done": "state = 2"}` with `"combinational": true`. The operands
  and `start` are FREE INPUTS. Refine with ONLY Initialization (reset the
  datapath/FSM registers to 0) + Iteration on the single clocked step; the
  combinational done is never iterated or reset.
- ABSTRACT-SPEC AUTHORING (the VERIFIED-REFINEMENT path). When the design is a
  pure arithmetic / algorithmic FUNCTION under a resource constraint that implies
  a SEQUENTIAL (multi-cycle) implementation — a sequential multiplier, a divider,
  a serial CRC, any "compute f(a,b) over several clocks" datapath — you MAY author
  an ABSTRACT specification statement and let the refinement engine DERIVE the
  datapath, instead of hand-writing the shift-add chains. Author it as a single
  transition with:
    "spec_statement": true,
    "condition": "TRUE",
    "updates": {"<output>": "<postcondition relation>"}   // e.g. {"product": "a * b"}
  and add a top-level "postcondition" field on that transition stating the relation
  the implementation must establish (e.g. "product = a * b"). Declare the target
  output variable(s) but leave them ABSTRACT — the refinement engine makes them
  concrete by deriving and VERIFYING the loop. The operands (a, b) and `start` are
  FREE INPUTS; do NOT author them as variables. Reset and the handshake (`state`,
  `done`) are introduced by the refinement RULES, not by you — do NOT author them
  in the abstract spec. CONTRAST: the concrete FSMD recipe just above (hand-writing
  the shift-add chains in a clocked transition) remains valid and is still the right
  choice for designs you can author directly; the abstract form is the NEW option
  that yields a machine-VERIFIED datapath — prefer it for sequential arithmetic
  functions where you can state the postcondition relation cleanly.
- SHIFT/BIT OPS ARE ARITHMETIC. There are NO `<<`, `>>`, bitwise `&`/`|`,
  bit-select `x[0]`, part-select `x[7:4]`, or concatenation `{a,b}` operators —
  they produce broken RTL. Express them with `* / %` instead: a LEFT shift by one
  is `x * 2`, a RIGHT shift by one is `x / 2`, and the LOW BIT is `(x % 2) = 1`.
  A shift-add multiplier is exactly this: each BUSY cycle, `IF (mplier % 2) = 1
  THEN product + mcand ELSE product` for the accumulator, `mcand * 2` to shift
  the multiplicand left, and `mplier / 2` to shift the multiplier right (give the
  shifting multiplicand and the product enough width — 8x8 needs a 16-bit
  product and a 16-bit shifting multiplicand).
- PROPOSING THE DERIVATION (when picking a rule on an abstract spec statement).
  When you are refining an abstract spec statement (a transition carrying
  `spec_statement: true` with a `postcondition`) and `LoopIntroduction` is in the
  applicable set, pick it and PROPOSE its params: `action_name` (the spec-statement
  action), `postcondition` (the relation to establish), `invariant`, `variant`,
  `guard`, `init`, `body`, `mapping`, `fresh_vars`, `input_widths`. For a shift-add
  multiplier of `product = a * b`:
    action_name:   the abstract spec-statement action (e.g. "Compute")
    postcondition: "product = a * b"
    invariant:     "product + mplier * mcand = a * b"
    variant:       "count"
    guard:         "count > 0"
    init:          {"product": "0", "mcand": "a", "mplier": "b", "count": "N"}
    body:          {"product": "IF (mplier % 2) = 1 THEN product + mcand ELSE product",
                    "mcand": "mcand * 2", "mplier": "mplier / 2", "count": "count - 1"}
    mapping:       {"product": "product"}   (the accumulator IS the output)
    fresh_vars:    [mcand, mplier, count] with their bit widths
    input_widths:  the operand port widths (from a, b)
  (N is the operand bit width; reuse the SHIFT/BIT-ARE-ARITHMETIC rule above —
  `*2` left-shift, `/2` right-shift, `(x % 2) = 1` low bit.) The engine AUTO-CHECKS
  these obligations against the real semantics and REJECTS a wrong invariant or
  body (you will be re-prompted) — so propose the CORRECT loop invariant, not a
  guess. After `LoopIntroduction` succeeds, the next applicable set will offer
  `ScheduleHandshakeFSM` — pick it to schedule the verified loop onto the clocked
  IDLE/BUSY/DONE start/done FSMD (params `{"action_name": <the loop action>}`).
  Then pick `Initialization` to reset the loop and control registers to 0. So the
  full verified derivation chain for a sequential multiplier is: abstract
  `product = a * b` -> LoopIntroduction (propose invariant/body) ->
  ScheduleHandshakeFSM -> Initialization.

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

def _run_with_tools(user_message: str, call_type: str | None = None) -> str:
    """
    Run an agentic tool-use loop with ANTHROPIC_API_KEY and the Agent 3 model.

    Keeps calling the model until it returns a stop_reason of 'end_turn'
    (no more tool calls pending). Returns the final text content.

    Used by: generate_formal_spec, revise_on_tlc, revise_on_cocotb.
    NOT used by: pick_rule (which has no tools and no loop).

    call_type labels the usage-ledger entries with the originating entry point.
    Each loop iteration is a separate billable call, so each is logged.
    """
    client = _get_client()
    messages: list[dict] = [{"role": "user", "content": user_message}]

    while True:
        _check_budget()  # pre-flight: stop before exceeding the Agent 3 budget
        response = _create(
            client,
            model=_AGENT3_MODEL,
            max_tokens=4096,
            temperature=0.0,
            system=_SYSTEM_PROMPT,
            tools=_TOOLS,
            messages=messages,
        )

        # Record token usage for this iteration (never raises).
        log_usage(
            agent="agent3",
            model=_AGENT3_MODEL,
            usage=getattr(response, "usage", None),
            call_type=call_type,
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
# Robust JSON extraction from model output
# ---------------------------------------------------------------------------
# Opus 4.8 is a reasoning model and sometimes prepends an explanation before the
# JSON on the revise_* calls, despite the system prompt's "JSON only" rule. Parse
# defensively: strict parse first, then strip a markdown fence, then extract the
# first balanced {...} object (string-aware, so braces inside string values do
# not throw off the depth count). Prose has no braces, so it is skipped.

def _extract_json(text: str) -> dict:
    """Parse a JSON object from model output that may include prose or fences."""
    s = (text or "").strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    if s.startswith("```"):
        inner = re.sub(r"^```[A-Za-z0-9]*\s*\n?", "", s)
        inner = re.sub(r"\n?```\s*$", "", inner).strip()
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            s = inner

    start = s.find("{")
    if start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(s[start:i + 1])

    raise ValueError(
        f"Agent 3 returned no parseable JSON object. First 200 chars: {s[:200]!r}"
    )


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
    raw = _run_with_tools(user_message, call_type="generate_formal_spec")
    data = _extract_json(raw)
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
    raw = _run_with_tools(user_message, call_type="revise_on_tlc")
    data = _extract_json(raw)
    return FormalSpec.model_validate(data)


def pick_rule(applicable_rules: list[dict], spec: dict, *, system_prompt: str | None = None) -> dict:
    """
    Choose which refinement rule to apply next.

    BOUNDED-ACTION-SPACE INVARIANT: this call uses NO tools and NO internal
    loop. The model must return a structured choice in one shot.

    See docs/agents.md (the bounded-action-space invariant).

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

    # Pre-flight budget check (same cap as the tool-using call types).
    _check_budget()

    # NO tools — this is a one-shot structured-output call.
    # The `tools` argument is OMITTED entirely (not passed as []): the Anthropic
    # API rejects an explicitly empty tools list, and omitting it is what actually
    # enforces the bounded-action-space invariant (no tool surface on pick_rule).
    response = _create(
        client,
        model=_AGENT3_MODEL,
        max_tokens=512,
        temperature=0.0,
        system=system_prompt if system_prompt is not None else _SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    # Record token usage for this one-shot call (never raises).
    log_usage(
        agent="agent3",
        model=_AGENT3_MODEL,
        usage=getattr(response, "usage", None),
        call_type="pick_rule",
    )

    raw = ""
    for block in response.content:
        if block.type == "text":
            raw += block.text

    raw = raw.strip()
    result = _extract_json(raw)

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
    raw = _run_with_tools(user_message, call_type="revise_on_cocotb")
    data = _extract_json(raw)
    return FormalSpec.model_validate(data)


def critique_refinement(
    abstract_spec: dict,
    concrete_spec: dict,
    *,
    abstraction_mapping: dict | None = None,
) -> dict:
    """
    Refinement-correctness critic — a pure, read-only accept/reject GATE.

    This is the runtime backing of pass6_checker. It is a ONE-SHOT structured-
    output call with NO tools and NO internal loop. The critic independently
    checks that `concrete_spec` correctly refines `abstract_spec` and returns a
    verdict that gates compilation in stage3: 'accept' → compile, 'reject' → halt
    with the critic's issues surfaced in the artifact.

    Args:
        abstract_spec: The abstract engine-spec dict (pre-refinement).
        concrete_spec: The refined engine-spec dict (post-refinement, RTL-style).
        abstraction_mapping: Optional explicit abstraction mapping; falls back to
            concrete_spec.get("abstraction_mapping", {}) then {}.

    Returns:
        {
          "verdict":  "accept" | "reject",
          "issues":   [str, ...],   # human-readable problems (empty on accept)
          "reasoning": str          # one-paragraph justification
        }

    The verdict is normalised: any non-'accept' verdict (including the critic's
    own 'fail'/'unknown' vocabulary, or a malformed response) is treated as
    'reject' so the gate fails CLOSED — a bad/uncertain refinement never compiles.
    """
    client = _get_client()

    if abstraction_mapping is None:
        abstraction_mapping = concrete_spec.get("abstraction_mapping", {}) or {}

    user_message = pass6_checker.USER_TEMPLATE.format(
        abstract_spec_json=json.dumps(abstract_spec, indent=2),
        concrete_spec_json=json.dumps(concrete_spec, indent=2),
        mapping_json=json.dumps(abstraction_mapping, indent=2),
    )

    # Pre-flight budget check (same cap as the other Agent 3 call types).
    _check_budget()

    # NO tools — one-shot structured-output critic call. The pass6_checker
    # SYSTEM prompt elicits the accept/reject verdict JSON (see that module).
    response = _create(
        client,
        model=_AGENT3_MODEL,
        max_tokens=1024,
        temperature=0.0,
        system=pass6_checker.SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )

    # Record token usage for this one-shot call (never raises).
    log_usage(
        agent="agent3",
        model=_AGENT3_MODEL,
        usage=getattr(response, "usage", None),
        call_type="critique_refinement",
    )

    raw = ""
    for block in response.content:
        if block.type == "text":
            raw += block.text
    raw = raw.strip()

    result = _extract_json(raw)
    return _normalise_verdict(result)


def _normalise_verdict(result: dict) -> dict:
    """
    Coerce a critic response into the canonical accept/reject verdict shape.

    Fails CLOSED: only an explicit 'accept' verdict accepts; everything else
    (the critic's 'fail'/'unknown', a missing/garbled field, or a non-dict)
    becomes 'reject' so a bad or uncertain refinement is never compiled.
    """
    if not isinstance(result, dict):
        return {
            "verdict": "reject",
            "issues": ["Critic returned a non-object response."],
            "reasoning": f"Unparseable critic output: {result!r}",
        }

    raw_verdict = str(result.get("verdict", "")).strip().lower()
    verdict = "accept" if raw_verdict == "accept" else "reject"

    issues = result.get("issues")
    if not isinstance(issues, list):
        issues = []
    issues = [str(i) for i in issues]

    reasoning = str(result.get("reasoning", "") or "")

    # If the critic used its native 'fail'/'unknown' vocabulary or any other
    # non-accept verdict, record that as an issue so the gate's error is honest.
    if verdict == "reject" and raw_verdict not in ("reject", ""):
        issues = issues + [f"Critic verdict was '{raw_verdict}' (treated as reject)."]

    return {"verdict": verdict, "issues": issues, "reasoning": reasoning}
