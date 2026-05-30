# Handoff — Runtime Agents Design Decisions

Context for the Claude instance coordinating the 5 development agents. This captures
decisions made in a separate session about the **runtime** agents (the LLM-using
components inside the running pipeline), so the dev agents can be configured against
a consistent target.

This is a delta/clarifier on `docs/architecture.md`, not a replacement.

---

## 1. Two senses of "agent" in this project

- **Development agents** (5 of them, mapped to people A–E in `architecture.md`'s team
  table) — these *build* the pipeline code. They have no runtime presence.
- **Runtime agents** (the topic of this doc) — LLM calls inside the executing
  pipeline. These are what the dev agents must produce.

Don't conflate them.

---

## 2. Runtime agents (final count: **3**, not 4)

After consolidation (see §4), the running pipeline has three LLM-using spots:

| Runtime agent | Input | Output | File |
|---|---|---|---|
| **Agent 1** | NL prompt | `SpecSummary` (JSON(S)) | `pipeline/agents/agent1.py` |
| **Agent 2** | `SpecSummary` | cocotb `.py` testbench | `pipeline/agents/agent2.py` |
| **Agent 3** | depends on task type (see §4) | depends on task type | `pipeline/agents/agent3.py` |

Everything else in `pipeline/` is **deterministic** Python or external tooling
(`compilers/`, `refinement/engine.py`, `refinement/rules/*`, `cocotb/runner.py`,
`graph.py`). No LLM calls there.

---

## 3. "Just an LLM call" still needs a file

Each runtime agent is a single structured-output LLM call, not a tool-using agent
loop. But each one still gets its own module (~30–80 lines):

- The system/user prompt (will grow long, especially Agent 3's)
- The Pydantic schema binding for structured output (`SpecSummary`, `FormalSpec`)
- The call site (Anthropic SDK call, parse, return typed object)
- Retry / revision entry points (Agent 3 has several — see §4)

Do **not** inline all LLM calls into `graph.py`. Keep them per-file for testability,
prompt maintenance, and owner separation.

---

## 4. **DECISION: Agent 3 absorbs the Rule Picker (Version A)**

`architecture.md` lists a separate "Rule Picker (LLM)" component. **We are
collapsing the Rule Picker into Agent 3.** This means:

- Delete (or do not create) `pipeline/refinement/rule_picker.py`. The Refinement
  Engine still exists and is still deterministic — it just calls into Agent 3 at
  each refinement step instead of into a separate picker module.
- Agent 3 becomes one *persona/prompt/knowledge base* serving multiple **call
  types**, each of which remains a single-shot structured-output call:

```python
# pipeline/agents/agent3.py — sketch of the call surface
def generate_formal_spec(summary: SpecSummary) -> FormalSpec: ...
def revise_on_tlc(spec: FormalSpec, tlc_errors: str) -> FormalSpec: ...
def pick_rule(current_tla: str, applicable_rules: list[RuleSpec]) -> RuleChoice: ...
def revise_on_cocotb(spec: FormalSpec, sim_log: str) -> FormalSpec: ...
```

`RuleChoice` is `(rule_name: str, params: dict)`.

### Why Version A (not a tool-using stateful agent)

We considered making Agent 3 a true tool-using agent that owns the entire formal
branch internally (calling `run_tlc`, `list_applicable_rules`, `apply_rule` as
tools in a loop). We **rejected** that for the MVP because it would break the
bounded-action-space safety claim in `architecture.md`. See §5.

---

## 5. Safety property dev agents must preserve

The architecture's central anti-hallucination claim is:

> The LLM never writes TLA+ during refinement. It receives the current spec plus
> the filtered list of applicable rules, and returns a single structured choice
> (rule_name, parameters). The engine applies it.

This must remain true after Agent 3 absorbs the Rule Picker. Specifically:

- `pick_rule` is **one-shot structured output**. No tool use. No internal loop.
- The call receives the *filtered* applicable rule list from the Refinement
  Engine (not the whole library) plus the current spec.
- The return shape is exactly `(rule_name, params)` — nothing else, no TLA+
  text, no free-form reasoning that the engine would then have to parse.
- The Refinement Engine remains the loop driver. It calls `pick_rule`, validates
  the choice is in the applicable set, calls `rule.apply(...)`, appends to
  `refinement_chain.json`, and either continues or backtracks.

If a dev agent wants to make `pick_rule` agentic (give it tools, let it iterate),
they need to flag it for a re-decision — that changes the safety story in
`architecture.md` §"LLM action space at each refinement step".

---

## 6. File layout implications for dev agents

Already exists on branch `JSON_summary`:
- `pipeline/schemas/summary_schema.py` — `SpecSummary` (JSON(S))
- `pipeline/schemas/tla_schema.py` — `FormalSpec` (JSON(TLA))

Empty stubs to fill (current branch state):
- `pipeline/agents/agent1.py`
- `pipeline/agents/agent2.py`
- `pipeline/agents/agent3.py` — **owns the rule-pick call type too**
- `pipeline/compilers/compiler1.py`, `compiler2.py`
- `pipeline/refinement/engine.py`
- `pipeline/refinement/rules/{initialization,iteration,sequential_composition,assignment,alternation,introduce_variable}.py`
- `pipeline/refinement/rules/base.py` — `RefinementRule` ABC
- `pipeline/cocotb/runner.py`
- `pipeline/graph.py`, `pipeline/state.py`

**Do not create** (decision change from `architecture.md`):
- `pipeline/refinement/rule_picker.py` — its responsibility is now inside `agent3.py`

The team-table mapping in `architecture.md` still holds, except person **D**'s
"Rule Picker LLM" line item is now part of Agent 3, owned by the same person.

---

## 7. Open items / not decided here

- Whether Agent 2 should get a light validation tool loop (e.g. `pyflakes`
  check before returning the testbench). Currently planned as one-shot, but
  cheap to upgrade later — doesn't affect any safety property.
- Whether `revise_on_cocotb` and `revise_on_tlc` share a prompt or have
  separate ones. Implementation detail for whoever writes `agent3.py`.
- Backtracking policy for the refinement loop ("roll back N steps") — left to
  the engine implementer.
