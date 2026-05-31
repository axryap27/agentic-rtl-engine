# Agentic RTL Engine — Claude Guide

Four-stage LangGraph pipeline: NL prompt → TLA+ formal spec → refinement-calculus-guided RTL-style TLA+ → synthesizable Verilog-2001, verified by cocotb.

---

## Artifact chain

Artifacts live at `artifacts/<run_id>/`. Each stage reads its input and writes its output as JSON on disk. **LangGraph routes solely on the `status` field of the output JSON** — never on Python return values or exceptions.

| File | Written by | Read by | `status` values |
|------|-----------|---------|-----------------|
| `00_nl_spec.json` | user / `main.py` | Stage 1 | — |
| `01_formal_spec.json` | Stage 1 | Stage 2, Stage 3 (retry) | `success`, `error` |
| `02_pluscal_impl.json` | Stage 2 | Stage 3 | `success`, `error` |
| `03_rtl_output.json` | Stage 3 | Stage 4, cocotb | `success`, `partial`, `error` |
| `04_evaluation.json` | Stage 4 | LangGraph terminal | `success`, `error` |
| `refinement_chain.json` | Refinement Engine | debugging, Stage 3 | — |

Every stage node **must write its output JSON before returning**, even on failure. The conditional edge function reads `status` from the artifact and routes to `retry_<N>`, `advance`, or `halt`. Failing to write the artifact will crash the router.

---

## RefinementRule interface

Every rule in `pipeline/refinement/rules/` must subclass `RefinementRule` from `base.py` and implement exactly three methods:

```python
def is_applicable(self, spec: dict) -> bool:
    """Return True if this rule can fire on the current spec."""

def apply(self, spec: dict, params: dict) -> dict:
    """Apply the rule deterministically. Returns the refined spec."""

def describe(self) -> str:
    """One-line human description shown to the Rule Picker LLM."""
```

`apply()` must be **pure** — same inputs always produce the same output. The engine depends on this for backtracking: it replays a saved `refinement_chain.json` from scratch to reach any prior state.

Tier-1 rules (MVP): `Initialization`, `Iteration`, `SequentialComposition`, `Assignment`, `Alternation`, `IntroduceVariable`.

Tier-2 (stretch): `ParallelComposition`, `ExpandFrame`, `ContractFrame`, `WeakenPrecondition`, `StrengthenPostcondition`.

See `docs/refinement_rules.md` for the formal definitions and `docs/architecture.md` for the hardware meaning of each rule.

---

## LLM clients (two transports today; a third agent is planned)

The pipeline has **2 LLM-using runtime agents** today — Agent 1 and Agent 3 — split across two transports. A planned end-of-pipeline diagnoser agent (see `docs/handoff_runtime_agents.md` §8) would make it 3 when built. Agent 2's original role is now a deterministic templated generator at `pipeline/cocotb/generator.py` — no LLM.

**Agent 1** — OpenAI-compatible SDK (`openai` package) via the proxy. One-shot structured-output call, no tools, no loop:

```python
import openai, os
client = openai.OpenAI(
    base_url=os.environ["LLM_BASE_URL"],
    api_key=os.environ["LLM_API_KEY"],
)
model = os.environ["LLM_MODEL"]
```

**Agent 3** — Claude Agent SDK direct to Anthropic. Tool-using; the SDK does NOT route through the proxy:

```python
import os
from claude_agent_sdk import ClaudeAgentClient   # exact import depends on SDK version
client = ClaudeAgentClient(api_key=os.environ["ANTHROPIC_API_KEY"])
```

Agent 3 has four entry points sharing one persona / system prompt: `generate_formal_spec`, `revise_on_tlc`, `pick_rule`, `revise_on_cocotb`. **Critical:** `pick_rule` runs with `tools=[]` (empty tool surface) to preserve the bounded-action-space invariant — see `docs/handoff_runtime_agents.md` §5. Its return shape is exactly `{"rule_name": str, "params": dict}`.

For all clients: `temperature=0.0`, structured-output mode when expecting JSON, system prompts reused across retries for prompt caching — do not regenerate them per call.

---

## Verilog output constraints

Stage 3 and Compiler 2 must emit **Verilog-2001 only** (not SystemVerilog):

- No `logic`, no `always_ff`, no `always_comb`
- Use `always @(posedge clk)` for clocked logic
- Use `always @(*)` for combinational logic
- No `initial` blocks in synthesizable modules (only in testbenches)
- Every `output` must be declared `reg` or driven by `assign`

Lint: `verilator --lint-only <file>.v` or `iverilog -Wall -t null <file>.v`.

---

## Retry protocol

| Failure | Inject into next prompt as | Max retries |
|---------|---------------------------|-------------|
| TLC rejects TLA+ | `"tlc_errors": "<full TLC stderr>"` | 3 |
| Verilog lint fails | `"lint_errors": "<full lint stderr>"` | 2 |
| Refinement stalls (no rule reaches RTL-style) | Backtrack N steps, re-prompt Rule Picker | engine-managed |

Never swallow errors. Always write `"status": "error"` and the error text to the artifact before returning. The router cannot act on an unwritten or status-less artifact.

---

## Code style

- **State:** `TypedDict` in `pipeline/state.py` — keep it thin: only `run_id`, `retry_counts`, `halt`
- **Schemas:** Pydantic v2 in `pipeline/schemas.py` — every artifact has a matching model
- **Agents/nodes:** one file per stage under `pipeline/agents/` (agents) or `pipeline/nodes/` (stage runners)
- No global mutable state between pipeline runs
- Pydantic models use `model_validate()` and `model_dump()`, not deprecated v1 `.parse_obj()` / `.dict()`

---

## Dev commands

```bash
# Run full pipeline on the default 2-bit counter spec
python3.11 main.py

# D flip-flop integration test (Stage 1 + Stage 3, Stage 2 bypassed)
python3.11 tests/test_dff.py

# Lint a generated Verilog file
verilator --lint-only artifacts/<run_id>/output.v
# or
iverilog -Wall -t null artifacts/<run_id>/output.v
```

Copy `.env.example` to `.env` and fill in `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` (for Agent 1 + Agent 2 via the proxy) and `ANTHROPIC_API_KEY` (for Agent 3 via Claude Agent SDK direct) before running.

---

## Custom slash commands

| Command | What it does |
|---------|-------------|
| `/add-refinement-rule <Name>` | Scaffolds a new rule file, registers it, updates docs |
| `/validate-artifacts <run_id>` | Validates all artifact JSONs against Pydantic schemas |
| `/check-tla <run_id>` | Runs TLC on the generated TLA+ spec |
| `/lint-rtl <run_id>` | Lints the generated Verilog file |
| `/trace-refinement <run_id>` | Pretty-prints the refinement chain step by step |
