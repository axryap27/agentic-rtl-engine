# Agentic RTL Engine — Claude Guide

Four-stage LangGraph pipeline: NL prompt → TLA+ formal spec → refinement-calculus-guided RTL-style TLA+ → synthesizable Verilog-2001, verified by cocotb.

---

## Artifact chain

Artifacts live at `artifacts/<run_id>/`. Each stage reads its input and writes its output as JSON on disk. **LangGraph routes solely on the `status` field of the output JSON** — never on Python return values or exceptions.

This table is the authoritative artifact map (mirrors `pipeline/graph.py`). The 3-agent design does **not** produce `01_formal_spec.json` or `02_pluscal_impl.json`.

| File | Written by | Read by | `status` values |
|------|-----------|---------|-----------------|
| `00_nl_spec.json` | user / `main.py` | Stage 1 | — (not a stage output) |
| `01_summary.json` | Stage 1 (Agent 1) | Stage 2, Stage 3 | `success`, `error` |
| `02_testbench_meta.json` (+ `02_testbench.py`) | Stage 2 (cocotb generator) | Stage 4 | `success`, `error` |
| `02_formal_spec.json` | Stage 3 (Agent 3) | Stage 4, Diagnose | `success`, `error` |
| `03_rtl_output.json` | Stage 3 (Compiler 2) | Stage 4, cocotb | `success`, `partial`, `error` |
| `04_evaluation.json` | Stage 4 (cocotb runner) | LangGraph terminal, Diagnose | `success`, `error` |
| `04_diagnosis.json` | Diagnose node | LangGraph routing | `success`, `error` |
| `refinement_chain.json` | Refinement Engine | debugging, Stage 3 (backtrack) | — (not routed on) |

Note: `02_formal_spec.json` is written by **Stage 3** (Agent 3), not Stage 2 — the `02_` prefix reflects ordering on disk, not which stage produces it. Filenames are kept as the code uses them; do not rename.

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

## LLM client

There are **two** LLM transports, split by agent:

**Stages 1–2 and the diagnoser** use the **OpenAI-compatible proxy** (`openai` package), configured via environment variables:

```python
import openai, os
client = openai.OpenAI(
    base_url=os.environ["LLM_BASE_URL"],
    api_key=os.environ["LLM_API_KEY"],
)
model = os.environ["LLM_MODEL"]
```

**Agent 3** is the deliberate exception: it uses the **Anthropic SDK directly** (`anthropic` package) with its own `ANTHROPIC_API_KEY` and `AGENT3_MODEL` (locked decision #3 — Agent 3 is a distinct, tool-using Claude agent). Do **not** collapse Agent 3 onto the proxy. See `pipeline/agents/agent3.py` and `docs/current_problems.md` (BUG-3) for the rationale. Note the proxy itself routes to Claude (`LLM_MODEL=anthropic/...`), so this is a transport split, not a model split.

Always use `temperature=0.0` for code and spec generation (exception: some newer models such as Claude Opus 4.8 have deprecated `temperature` and return 400 if it is sent — Agent 3's `_create` wrapper auto-detects this, strips the parameter, and retries, so `temperature=0.0` stays the default wherever it is still supported). Always use `response_format={"type": "json_object"}` when expecting structured output (proxy calls) — Agent 3 enforces JSON via its prompt. System prompts are intentionally reused across retries for prompt caching — do not regenerate them per call.

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
- **Schemas:** Pydantic v2 in the `pipeline/schemas/` package (`summary_schema.py`, `tla_schema.py`, `envelope.py`, …) — every artifact has a matching model; the `ArtifactEnvelope` model validates the `status` field every stage writes
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

Copy `.env.example` to `.env` and fill in `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` before running.

---

## Custom slash commands

| Command | What it does |
|---------|-------------|
| `/add-refinement-rule <Name>` | Scaffolds a new rule file, registers it, updates docs |
| `/validate-artifacts <run_id>` | Validates all artifact JSONs against Pydantic schemas |
| `/check-tla <run_id>` | Runs TLC on the generated TLA+ spec |
| `/lint-rtl <run_id>` | Lints the generated Verilog file |
| `/trace-refinement <run_id>` | Pretty-prints the refinement chain step by step |
