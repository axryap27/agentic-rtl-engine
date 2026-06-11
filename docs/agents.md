# Runtime Agents

The pipeline has exactly **three** runtime LLM components: **Agent 1**, **Agent 3**,
and the **Diagnoser**. Everything else under `pipeline/` is deterministic Python.

> Historical note: an "Agent 2" (LLM testbench writer) and a separate "Rule Picker"
> were in the original design. Agent 2 became the deterministic
> [cocotb generator](verification.md) (the test vectors already fully specify the
> bench), and the Rule Picker was folded into Agent 3 as the `pick_rule` call type.
> `pipeline/refinement/rule_picker.py` is now a deprecated stub.

---

## Two transports

There are two LLM transports, split by agent — a *transport* split, not a model split
(the proxy itself routes to Claude).

**Agent 1 and the Diagnoser** use an **OpenAI-compatible proxy** via the `openai`
package:

```python
import openai, os
client = openai.OpenAI(base_url=os.environ["LLM_BASE_URL"],
                       api_key=os.environ["LLM_API_KEY"])
model = os.environ["LLM_MODEL"]
```

**Agent 3** is the deliberate exception: it uses the **Anthropic SDK directly**
(`anthropic` package) with its own `ANTHROPIC_API_KEY` and `AGENT3_MODEL`, because it
is a distinct, tool-using Claude agent. Do **not** collapse Agent 3 onto the proxy.

Conventions across all three: `temperature=0.0` for determinism (Agent 3 auto-detects
models that reject the parameter and retries without it — see below); proxy calls use
`response_format={"type": "json_object"}`; Agent 3 enforces JSON via its prompt and a
tolerant extractor. System prompts are built once and reused across calls for prompt
caching.

---

## Agent 1

**File:** `pipeline/agents/agent1.py` · **Transport:** proxy · **Returns:** `SpecSummary`

A single structured-output call: natural-language prompt → `SpecSummary`
(`module_name`, `description`, typed `ports`, golden `test_vectors`, reset polarity).
One shot, no tools, `temperature=0.0`, `response_format={"type":"json_object"}`. Token
usage is logged via `usage.log_usage`. Retries are driven externally by LangGraph
(`_MAX_STAGE1_RETRIES = 1`), not inside the agent.

The `SpecSummary` is the contract for both downstream branches: Stage 2 reads
`ports` + `test_vectors` to build the testbench; Stage 3 reads `ports` + `description`
(and port widths) to author the formal spec.

---

## Agent 3

**File:** `pipeline/agents/agent3.py` · **Transport:** Anthropic SDK ·
**Model:** `AGENT3_MODEL` (default `claude-opus-4-5`)

One persona serving **five call types**. Three are tool-using (they may call a
read-only `read_artifact` tool in a bounded loop); two are strictly one-shot.

| Call type | Tools? | Signature | Returns |
|---|:---:|---|---|
| `generate_formal_spec` | ✓ | `(summary: SpecSummary) -> FormalSpec` | a fresh formal spec |
| `revise_on_tlc` | ✓ | `(spec: FormalSpec, tlc_errors: str) -> FormalSpec` | spec revised against TLC errors |
| `revise_on_cocotb` | ✓ | `(spec: FormalSpec, sim_log: str) -> FormalSpec` | spec revised against a sim failure |
| `pick_rule` | ✗ | `(applicable_rules: list[dict], spec: dict, *, system_prompt=None) -> dict` | `{"rule_name": str, "params": dict}` |
| `critique_refinement` | ✗ | `(abstract_spec, concrete_spec, *, abstraction_mapping=None) -> dict` | `{"verdict": "accept"\|"reject", "issues": [...], "reasoning": str}` |

### The bounded-action-space invariant

`pick_rule` is the load-bearing safety property. It is a **one-shot structured-output
call with no tools and no internal loop**. The Refinement Engine hands it the
*filtered* set of currently-applicable rules plus the current spec, and it returns
exactly one `(rule_name, params)` choice — no TLA+, no free-form reasoning the engine
would have to parse. The engine validates the choice is in the applicable set, applies
the rule deterministically, and appends to the chain. Giving `pick_rule` tools or a
loop would break this invariant and must be flagged for re-decision.

The [derivation proposal](#abstract-spec-authoring-and-the-verified-derivation) rides
*inside* this contract, not around it: when `LoopIntroduction` is offered, the `params`
dict carries the proposed invariant/variant/body — the params got richer, but it is
still the same single one-shot structured-output call with no tools and no internal
loop (the `tools` argument is omitted from the API call entirely; see the comment in
`pick_rule`). The bounded action space is unchanged.

`critique_refinement` (the [correctness critic](refinement.md#the-correctness-critic))
is likewise one-shot, no tools.

### Abstract-spec authoring and the verified derivation

For a pure arithmetic/algorithmic function that needs a sequential multi-cycle
implementation (a sequential multiplier, a divider, a serial CRC), Agent 3's system
prompt instructs it to **prefer authoring an abstract spec statement** over
hand-writing the shift-add chains: a single transition with
`"spec_statement": true`, `"condition": "TRUE"`, an abstract update (e.g.
`{"product": "a * b"}`), and a top-level `postcondition` stating the relation the
implementation must establish — both real `FormalSpec` schema fields
(`pipeline/schemas/tla_schema.py`). The target output variable is declared but left
**abstract** (the refinement engine derives and *verifies* the concrete loop);
operands and `start` stay free inputs; reset and the handshake (`state`/`done`) are
introduced by the refinement rules, never authored. The concrete hand-written FSMD
recipe remains valid for directly-authorable designs.

`pick_rule` carries the matching **derivation-proposal** guidance: when refining an
abstract spec statement and `LoopIntroduction` is in the applicable set, it picks it
and proposes the full derivation as params — `action_name`, `postcondition`,
`invariant`, `variant`, `guard`, `init`, `body`, `mapping`, `fresh_vars`,
`input_widths` (e.g. the textbook shift-add invariant
`product + mplier * mcand = a * b`). The engine's
[obligation kernel](refinement.md) auto-checks the proposal against the real
semantics and rejects a wrong invariant or body — `LoopIntroduction.apply()` is a
pure no-op on failed obligations, which the engine counts as a strike (backtrack
after 3). After success the next applicable set offers `ScheduleHandshakeFSM`
(params `{"action_name": <the loop action>}`), then `Initialization`, so the full
verified chain is `LoopIntroduction → ScheduleHandshakeFSM → Initialization`.

### Temperature auto-detection

Some newer models (e.g. Claude Opus 4.8) have deprecated `temperature` and return a
400 if it is sent. Agent 3's internal `_create` wrapper detects this on the first such
error, strips the parameter, retries (the rejected call is not billed), and remembers
the model in a module-level `_NO_TEMPERATURE` set. So `temperature=0.0` stays the
default everywhere it is still accepted, with no per-call configuration.

### Budget guard

Agent 3 is the only agent that talks to a metered API directly, so it has a spend
guard. Before every call, `_check_budget()` compares cumulative spend (from the usage
ledger) plus a reserve against a cap, both read at call time:

| Env var | Default | Meaning |
|---|---|---|
| `AGENT3_BUDGET_USD` | `100.0` | hard cap on cumulative Agent-3 spend |
| `AGENT3_BUDGET_RESERVE_USD` | `0.50` | conservative stand-in for the not-yet-logged in-flight call |

If the cap would be exceeded it raises `usage.BudgetExceeded` *before* the call. See
[the usage ledger](#the-usage-ledger).

### Key not configured

Until `ANTHROPIC_API_KEY` is set to a real key (it ships as a placeholder sentinel),
Agent 3 raises a clear error at call time. Stages 1 and 2 still run; Stage 3 halts.
This key is billed per token and is **separate from any Claude subscription** — a
subscription does not cover direct API usage. See [running.md](running.md#credentials).

---

## The Diagnoser

**File:** `pipeline/agents/agent_diagnoser.py` · **Transport:** proxy ·
**Node:** `pipeline/nodes/diagnose.py`

A two-way failure classifier between Stage 4 and the Stage-3 recovery paths. It reads
`04_evaluation.json` (the structured failure: `phase`, `failed_vectors`, `raw`),
`02_formal_spec.json`, and `refinement_chain.json`, and returns:

```json
{"failure_type": "spec" | "refinement", "explanation": "..."}
```

- **`phase == "build"`** (the Verilog did not compile) → classified `"spec"`
  **with no LLM call** — malformed RTL means a fresh spec + refinement is the only
  viable recovery.
- **`phase == "test"`** (simulation ran, assertions failed) → an LLM call decides:
  - `"spec"` — the FormalSpec describes the wrong behavior (core logic/variables/
    transitions). Recovery: `stage3_revise_cocotb` (Agent 3 revises the spec).
  - `"refinement"` — the spec is right but a rule was applied with wrong parameters
    (reset values, clock domain, update expressions). Recovery:
    `stage3_backtrack_refinement` (truncate the chain, re-pick).

The node always writes a valid `04_diagnosis.json` and sets `state["last_diagnosis"]`,
defaulting to `"spec"` on any error so routing always has a signal.

> A dedicated end-of-pipeline diagnoser agent was the planned home for this logic; it
> is implemented today as the runtime LLM component above.

---

## The usage ledger

**File:** `pipeline/usage.py`

A per-token cost ledger, written as JSONL under `artifacts/usage/<YYYY-MM-DD>.jsonl`
(session-anchored at 08:00; overridable with `USAGE_LOG_DIR` / `USAGE_LOG_PATH`).

- `log_usage(*, agent, model, usage, ...)` — append one record; **never raises** (a
  raising logger would crash a stage before it writes its artifact).
- `check_budget(agent, budget_usd, reserve_usd=0.0)` — pre-flight guard; raises
  `BudgetExceeded` when `agent_cost(agent) + reserve >= budget_usd`.
- `agent_cost`, `record_baseline`, `reprice`, `summarize`, `sessions`, `report`, … —
  reporting and reconciliation helpers (baseline lets you pin an authoritative spend
  figure from the provider console).

Agent 1 and the Diagnoser log usage; Agent 3 logs **and** is gated by `check_budget`.
