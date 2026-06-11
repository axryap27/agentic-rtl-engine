# Agentic RTL Engine — Claude Guide

Four-stage LangGraph pipeline: NL prompt → TLA+ formal spec → refinement-calculus-guided RTL-style TLA+ → synthesizable Verilog-2001, verified by cocotb.

---

## Artifact chain

Artifacts live at `artifacts/<YYYY-MM-DD>/<HHMMSS>-<module>-<hash>/` — `run_id` is that date-prefixed relative path (`pipeline/run_dirs.py`; the module name is folded into the leaf after the run, `artifacts/latest` symlinks the newest run, and `python3.11 main.py --clean-artifacts [N]` prunes all but the N newest). Each stage reads its input and writes its output as JSON on disk. **LangGraph routes solely on the `status` field of the output JSON** — never on Python return values or exceptions.

This table is the authoritative artifact map (mirrors `pipeline/graph.py`). The 3-agent design does **not** produce `01_formal_spec.json` or `02_pluscal_impl.json`.

| File | Written by | Read by | `status` values |
|------|-----------|---------|-----------------|
| `00_nl_spec.json` | user / `main.py` | Stage 1 | — (not a stage output) |
| `01_summary.json` | Stage 1 (Agent 1) | Stage 2, Stage 3, Stage 4 (vector check) | `success`, `error` |
| `02_testbench_meta.json` (+ `02_testbench.py`) | Stage 2 (cocotb generator) | Stage 4 | `success`, `error` |
| `02_formal_spec.json` | Stage 3 (Agent 3) | Stage 4, Diagnose | `success`, `error` |
| `02_vector_check.json` (+ `02_testbench_specvec.py`) | Stage 4 pre-flight (vector check) | cocotb (runs the specvec bench), debugging | `success` — never routed on |
| `03_rtl_output.json` | Stage 3 (Compiler 2) | Stage 4, cocotb | `success`, `partial`, `error` |
| `04_evaluation.json` | Stage 4 (cocotb runner) | LangGraph terminal, Diagnose, `main.py` banner | `success`, `error` |
| `04_soak.json` (+ `04_soak_testbench.py`) | Stage 4 post-pass (soak) | debugging (referenced by `main.py` banner) | `success`, `failed`, `skipped` — never routed on |
| `04_diagnosis.json` | Diagnose node | LangGraph routing | `success`, `error` |
| `refinement_chain.json` | Refinement Engine | Stage 3 (backtrack), Stage 4 (vector-check replay), Diagnose, debugging | — (not routed on) |
| `refinement_decisions.jsonl` | Stage 3 (`pick_rule` wrapper, append-only) | debugging | — (best-effort, not routed on) |

Note: `02_formal_spec.json` is written by **Stage 3** (Agent 3), not Stage 2 — the `02_` prefix reflects ordering on disk, not which stage produces it. Filenames are kept as the code uses them; do not rename.

Stage 4 pre-flight (`pipeline/cocotb/vector_check.py`): replays `refinement_chain.json` over `02_formal_spec.json` through the independent spec interpreter (`pipeline/cocotb/spec_sim.py`), derives expected outputs from Agent 1's **input** stimulus, and writes the corrected bench `02_testbench_specvec.py` — cocotb runs that instead of Agent 1's bench, so a wrong Agent-1 golden vector cannot fail correct RTL. Best-effort: on any failure (missing chain, undriven output port, interpreter gap) it silently falls back to `02_testbench.py` and writes no report. Agent-1/spec disagreements are recorded as `vector_disagreement` on `04_evaluation.json` (whose status stays `success`).

Stage 4 post-pass (`pipeline/cocotb/soak.py`): only after the directed bench passes, soaks the RTL against the spec interpreter for `RTL_SOAK_CYCLES` deterministic random cycles (default 2000, `0` disables; seed = crc32 of the run-dir leaf, so it replays from artifacts). A `failed` soak is a deterministic codegen/composition bug: surfaced loudly (soak block on `04_evaluation.json` + `main.py` banner) but it does **not** flip `04_evaluation` status — a metered Agent-3 revision cannot fix it. Skipped soaks are fail-soft and never break Stage 4.

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

Verified-derivation rules: `LoopIntroduction` fires on a transition carrying `spec_statement: true` + a `postcondition` and installs a verified loop **only** after `pipeline/refinement/obligations.py` discharges the Morgan/Back O1/O2/O3 iteration obligations against the real expression semantics — an exhaustive proof when the input-space product ≤ 65536, else sampled falsification. On failed obligations `apply()` is a pure no-op: that is the engine's strike/backtrack signal. On success the discharged audit `{invariant, variant, guard, mode, cases_checked, obligations}` is recorded on `action["refinement"]` and a loop marker `{init, body, variant, guard}` is installed. `ScheduleHandshakeFSM` is deterministic, no proof needed (soundness lives in `LoopIntroduction`): it consumes the loop marker and schedules it onto the hardened IDLE(0)/BUSY(1)/DONE(2) start/done FSMD — load on `(state = 0 OR state = 2) AND start = 1` (back-to-back safe), combinational `done = (state = 2)`.

All 8 rules above are registered in `TIER1_RULES` (`pipeline/refinement/rules/__init__.py` = `engine.RULE_REGISTRY`).

Tier-2 (stretch, unimplemented): `ParallelComposition`, `ExpandFrame`, `ContractFrame`, `WeakenPrecondition`, `StrengthenPostcondition`.

See `docs/refinement.md` for the implemented rules and the engine, and `docs/background.md` for the refinement-calculus definitions and each rule's hardware meaning.

---

## LLM client

There are **two** LLM transports, split by agent:

**Stage 1 (Agent 1) and the diagnoser** use the **OpenAI-compatible proxy** (`openai` package), configured via environment variables. (Stage 2 makes **no** LLM calls — it is a deterministic template-based testbench generator, `pipeline/cocotb/generator.py`; there is no `agent2.py`.)

```python
import openai, os
client = openai.OpenAI(
    base_url=os.environ["LLM_BASE_URL"],
    api_key=os.environ["LLM_API_KEY"],
)
model = os.environ["LLM_MODEL"]
```

**Agent 3** is the deliberate exception: it uses the **Anthropic SDK directly** (`anthropic` package) with its own `ANTHROPIC_API_KEY` and `AGENT3_MODEL` (locked decision #3 — Agent 3 is a distinct, tool-using Claude agent). Do **not** collapse Agent 3 onto the proxy. See `pipeline/agents/agent3.py` and `docs/agents.md` for the rationale. Note the proxy itself routes to Claude (`LLM_MODEL=anthropic/...`), so this is a transport split, not a model split.

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
| TLC rejects TLA+ | `"tlc_errors": "<full TLC stderr>"` → Agent 3 `revise_on_tlc` | 3 (`MAX_TLC_RETRIES`, `stage3.py`) |
| cocotb fails (incl. iverilog compile/elaboration) | diagnoser classifies → Agent 3 `revise_on_cocotb(spec, sim_log)` **or** refinement backtrack | 2 total revision+backtrack attempts (`_MAX_COCOTB_RETRIES`, `graph.py`) |
| Refinement stalls (no rule reaches RTL-style) | Backtrack N steps, re-prompt Rule Picker | engine-managed |

Never swallow errors. Always write `"status": "error"` and the error text to the artifact before returning. The router cannot act on an unwritten or status-less artifact.

---

## Code style

- **State:** `TypedDict` in `pipeline/state.py` — keep it thin: only `run_id`, `retry_counts`, `halt`, `last_diagnosis`
- **Schemas:** Pydantic v2 in the `pipeline/schemas/` package (`summary_schema.py`, `tla_schema.py`, `envelope.py`, …) — every artifact has a matching model; the `ArtifactEnvelope` model validates the `status` field every stage writes
- **Agents/nodes:** one file per stage under `pipeline/agents/` (agents) or `pipeline/nodes/` (stage runners)
- No global mutable state between pipeline runs
- Pydantic models use `model_validate()` and `model_dump()`, not deprecated v1 `.parse_obj()` / `.dict()`

---

## Dev commands

```bash
# Run full pipeline on the default 2-bit counter spec
python3.11 main.py

# Prune artifacts/ to the N newest runs (default 10)
python3.11 main.py --clean-artifacts [N]

# D flip-flop integration test (deterministic, no LLM: hand-built FormalSpec
# -> bridge -> refinement engine -> Compiler 2)
python3.11 tests/test_dff.py          # or: python3.11 -m pytest tests/test_dff.py -v

# Build the OPTIONAL native C++17 verification core (CMake + pybind11; runs ctest,
# installs pipeline.refinement._rtlcore — pure-Python fallback when not built)
./core/build.sh

# Lint a generated Verilog file
verilator --lint-only artifacts/latest/output.v
# or
iverilog -Wall -t null artifacts/latest/output.v
```

Copy `.env.example` to `.env` and fill in `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, and `ANTHROPIC_API_KEY` (Agent 3 — Stages 1–2 run without it; optional `AGENT3_MODEL` override) before running. Optional runtime knobs: `OBLIGATIONS_BACKEND` / `SPECSIM_BACKEND` (`auto` | `python` | `cpp` — `cpp` raises if the native core is not built) select the obligation-kernel / spec-sim backend; `RTL_SOAK_CYCLES` (default 2000, `0` disables) sizes the Stage 4 soak.

---

## Custom slash commands

| Command | What it does |
|---------|-------------|
| `/add-refinement-rule <Name>` | Scaffolds a new rule file, registers it, updates docs |
| `/validate-artifacts <run_id>` | Validates all artifact JSONs against Pydantic schemas |
| `/check-tla <run_id>` | Runs TLC on the generated TLA+ spec |
| `/lint-rtl <run_id>` | Lints the generated Verilog file |
| `/trace-refinement <run_id>` | Pretty-prints the refinement chain step by step |
