# Handoff — Runtime Agents Design Decisions

Context for the Claude instance coordinating the development agents. This captures
decisions about the **runtime** agents (the LLM-using components inside the running
pipeline), so the dev agents can be configured against a consistent target.

This is a delta/clarifier on `docs/architecture.md`, not a replacement.

---

## 1. Two senses of "agent" in this project

- **Development agents** (5 of them, mapped to people A–E in `architecture.md`'s team
  table) — these *build* the pipeline code. They have no runtime presence.
- **Runtime agents** (the topic of this doc) — LLM calls inside the executing
  pipeline. These are what the dev agents must produce.

Don't conflate them.

---

## 2. Runtime LLM agents (current count: **2**, planned **3**)

After §4 consolidation AND the cocotb-branch Agent 2 simplification (see §3), the
running pipeline has two LLM-using spots today, with a third planned (see §8):

| Runtime agent | Input | Output | File |
|---|---|---|---|
| **Agent 1** | NL prompt | `SpecSummary` (JSON(S)) | `pipeline/agents/agent1.py` |
| **Agent 3** | depends on call type (see §4) | depends on call type | `pipeline/agents/agent3.py` |

Agent 2 was reframed as a deterministic templated generator at
`pipeline/cocotb/generator.py` — see §3.

Everything else in `pipeline/` is **deterministic** Python or external tooling
(`compilers/`, `refinement/engine.py`, `refinement/rules/*`, `cocotb/generator.py`,
`cocotb/runner.py`, `graph.py`). No LLM calls there.

---

## 3. Agent files and their LLM shapes

Each runtime LLM agent gets its own module (~30–80 lines + system prompts):

- **Agent 1** — single structured-output call via the OpenAI-compatible proxy
  (`LLM_BASE_URL` + `LLM_API_KEY` from `.env`). Output validated by `SpecSummary`
  Pydantic schema in `pipeline/schemas/summary_schema.py`.
- **Agent 3** — tool-using agent via the Claude Agent SDK direct to Anthropic
  (`ANTHROPIC_API_KEY` from `.env`, separate from the proxy key). Four entry points
  share one persona; `pick_rule` runs with `tools=[]` to preserve §5.

**Agent 2 was reframed as `pipeline/cocotb/generator.py`** — a deterministic
templated function, not an LLM call. `SpecSummary.test_vectors` already fully
specifies the testbench (per-vector input/expected dicts), so an LLM added no value
and only widened the hallucination surface. The companion deterministic runner
lives at `pipeline/cocotb/runner.py`.

Do **not** inline LLM calls into `graph.py`. Keep them per-file for testability,
prompt maintenance, and SDK-key separation.

---

## 4. **DECISION: Agent 3 absorbs the Rule Picker (Version A)**

`architecture.md` lists a separate "Rule Picker (LLM)" component. **We are
collapsing the Rule Picker into Agent 3.** This means:

- Delete (or do not create) `pipeline/refinement/rule_picker.py`. The Refinement
  Engine still exists and is still deterministic — it just calls into Agent 3 at
  each refinement step instead of into a separate picker module.
- Agent 3 becomes one *persona/prompt/knowledge base* serving multiple **call
  types**, each implemented through the Claude Agent SDK:

```python
# pipeline/agents/agent3.py — sketch of the call surface
def generate_formal_spec(summary: SpecSummary) -> FormalSpec: ...
def revise_on_tlc(spec: FormalSpec, tlc_errors: str) -> FormalSpec: ...
def pick_rule(current_tla: str, applicable_rules: list[RuleSpec]) -> RuleChoice: ...
def revise_on_cocotb(spec: FormalSpec, sim_log: str) -> FormalSpec: ...
```

`RuleChoice` is `(rule_name: str, params: dict)`.

### Why Version A

The first three of those entry points use Agent 3's tool surface (tlc_run,
read_artifact, doc lookup) freely. `pick_rule` is the exception — see §5.

---

## 5. Safety property dev agents must preserve

The architecture's central anti-hallucination claim is:

> The LLM never writes TLA+ during refinement. It receives the current spec plus
> the filtered list of applicable rules, and returns a single structured choice
> (rule_name, parameters). The engine applies it.

This must remain true even though Agent 3 is a Claude Agent SDK tool-using agent
overall. Specifically:

- `pick_rule` runs with `tools=[]` (no tool surface). One-shot structured output,
  no internal loop.
- The call receives the *filtered* applicable rule list from the Refinement Engine
  (not the whole library) plus the current spec.
- The return shape is exactly `(rule_name, params)` — nothing else, no TLA+ text,
  no free-form reasoning that the engine would then have to parse.
- The Refinement Engine remains the loop driver. It calls `pick_rule`, validates
  the choice is in the applicable set, calls `rule.apply(...)`, appends to
  `refinement_chain.json`, and either continues or backtracks.

If a dev agent wants to give `pick_rule` tools or let it iterate, they need to
flag it for a re-decision — that changes the safety story in `architecture.md`
§"LLM action space at each refinement step".

---

## 6. File layout implications for dev agents

Already exists on branches:
- `pipeline/schemas/summary_schema.py` — `SpecSummary` (JSON(S))
- `pipeline/schemas/tla_schema.py` — `FormalSpec` (JSON(TLA))
- `pipeline/cocotb/generator.py` — deterministic testbench generator (replaces Agent 2)
- `pipeline/cocotb/runner.py` — deterministic cocotb runner

Empty stubs to fill:
- `pipeline/agents/agent1.py`
- `pipeline/agents/agent3.py` — **owns the rule-pick call type too, implemented via Claude Agent SDK**
- `pipeline/compilers/compiler1.py`, `compiler2.py`
- `pipeline/refinement/engine.py`
- `pipeline/refinement/rules/{initialization,iteration,sequential_composition,assignment,alternation,introduce_variable}.py`
- `pipeline/refinement/rules/base.py` — `RefinementRule` ABC
- `pipeline/graph.py`, `pipeline/state.py`

**Do not create:**
- `pipeline/refinement/rule_picker.py` — responsibility is inside `agent3.py`.
- `pipeline/agents/agent2.py` — Agent 2's role moved to `pipeline/cocotb/generator.py`
  (deterministic).

The team-table mapping in `architecture.md` still holds, except: person **D**'s
"Rule Picker LLM" line item is now part of Agent 3 (same person owns it); person
**E**'s "Agent 2 (cocotb testbench generator)" line item is now a deterministic
templating job under `pipeline/cocotb/` (same person owns it, less prompt
engineering, more template design).

---

## 7. Open items / not decided here

- ~~Whether Agent 2 should get a light validation tool loop~~ — **CLOSED.** Agent
  2 reframed as deterministic templated generator; no LLM, so no tool loop needed.
- Whether `revise_on_cocotb` and `revise_on_tlc` share a prompt or have separate
  ones. Implementation detail for whoever writes `agent3.py`.
- Backtracking policy for the refinement loop ("roll back N steps") — left to the
  engine implementer.
- Diagnoser-agent design — see §8.

---

## 8. Planned: end-of-pipeline diagnoser agent

User has scoped (not yet implemented) a third runtime LLM agent that triggers
only when the cocotb runner returns `status: fail` at the end of the pipeline.

**Purpose:** Analyze the cocotb error trace + a high-level summary of what each
pipeline stage produced. Decide which stage the bug most likely originated in.
Instruct LangGraph to restart the pipeline from that stage with a routing hint.
Coarse-grained — not line-level fix recommendations. Pipeline becomes a closed
loop on failure: cocotb fail → diagnoser → routed re-entry → retry.

**Why this matters now:** failure traces produced by `pipeline/cocotb/runner.py`
should be designed with this future consumer in mind. Structured fields (test,
cycle, signal, expected, got) route better than raw stderr. Tighten the trace now
and the diagnoser slots in cleanly later.

**Open design choices:**

- LLM transport: same proxy as Agent 1, or direct Anthropic like Agent 3? (Lean
  toward proxy unless tool use is needed.)
- One-shot or tool-using? (Probably one-shot — the decision space is "which
  stage" + "what hint", which is small.)
- Routing target options: `{stage_1_agent1, stage_1_agent3_generate, stage_1_agent3_revise_tlc, stage_2_refinement, stage_3_compiler, halt}` — exact enum TBD.
- Per-run invocation budget: cap at 2–3 to prevent infinite loops; halt the run if
  exceeded.
- Where it lives: `pipeline/agents/diagnoser.py` (suggested).

When this is built, §2's runtime LLM agent count rises from 2 to 3 again.
