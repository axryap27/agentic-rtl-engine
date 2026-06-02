# Current Problems

Last updated: 2026-06-01 (diagnoser + routing wired up)  
Branch: pipeline-dev

---

## Recently Fixed (no longer bugs)

- **BUG-1 (resolved):** The refinement engine format mismatch is fixed. `pipeline/refinement/bridge.py` now provides `formal_spec_to_engine_spec()`, which `stage3.py` calls correctly before passing the spec to the engine.
- **BUG-2 (resolved):** The cocotb retry loop no longer discards the revision. `stage3.py` now exposes a shared `_run_stage3_from_spec(state, spec)` helper. `run_stage3_revise_cocotb()` calls it directly with the revised `FormalSpec` instead of calling `run_stage3()`, which would have regenerated from scratch.
- **BUG-6 (resolved):** The `_make_pick_rule_callable()` fallback now uses `applicable_rules[0]["name"]` (the correct key sent by the engine) instead of `applicable_rules[0]["rule_name"]` (which raised `KeyError`).
- **BUG-7 (resolved):** The refinement engine output is no longer discarded. After the engine runs, `stage3.py` calls `engine_spec_to_rtl_tla()` from `bridge.py` to produce RTL-style TLA+ for Compiler 2.
- **BUG-12 (resolved / was wrong):** The `refinement_templates/` directory is not dead code — `stage3.py` actively imports and uses all six pass configurations to structure the multi-pass refinement loop.

### Diagnoser / routing agent (new in this session)

Three new files implement a two-way failure classification and routing system that sits between Stage 4 and the Stage 3 revision paths:

- **`pipeline/agents/agent_diagnoser.py`** — LLM agent that reads `04_evaluation.json`, `02_formal_spec.json`, and `refinement_chain.json`. Build failures (`phase == "build"`) are classified as `"spec"` without an LLM call. Test failures use an LLM to distinguish a spec fault (wrong core logic) from a refinement fault (wrong parameters — reset values, clock domains, update expressions).
- **`pipeline/nodes/diagnose.py`** — Thin LangGraph node wrapping the diagnoser agent. Writes `04_diagnosis.json` and sets `state["last_diagnosis"]` to `"spec"` or `"refinement"`. Defaults to `"spec"` on any error so routing always has a valid signal.
- **`pipeline/nodes/stage3.py`** — Extended with `run_stage3_backtrack_refinement()`: keeps the FormalSpec unchanged, truncates `refinement_chain.json` by `BACKTRACK_STEPS`, replays the chain to the truncation point via `_replay_chain()`, injects the diagnosis explanation into the pick_rule system prompt, and re-runs the engine from the checkpoint. Pre-backtrack history is saved to `refinement_chain_prefix.json`.
- **`pipeline/state.py`** — Added `last_diagnosis: str | None` field.
- **`pipeline/nodes/stage4.py`** — Failure branch now writes the full structured runner result (`phase`, `failed_vectors`, `raw`) so the diagnoser has all the context it needs.
- **`pipeline/graph.py`** — Updated: removed the local `run_stage3_revise_cocotb` definition, added `diagnose` and `stage3_backtrack_refinement` nodes, updated `_route_after_stage4` to route to `"diagnose"` on failure, added `_route_after_diagnose` to fork between the two revision paths.

---

## Critical — These break the pipeline entirely

---

### BUG-3 (RESOLVED — won't fix; Anthropic SDK is intentional): Agent 3 uses the Anthropic SDK

**File:** `pipeline/agents/agent3.py:31,81`

**Decision (2026-06-02):** Agent 3 **stays on the Anthropic SDK** with its own `ANTHROPIC_API_KEY`. This is a deliberate architecture choice (locked decision #3: Agent 3 is a distinct, tool-using Claude agent), not a defect. The key will be provisioned from a separate Anthropic account and added to `.env`.

The original report's three concerns were re-examined and found not to justify a migration:
- *"Bypasses the proxy"* — true, but by design; Agent 3 is intentionally a distinct agent.
- *"No prompt caching"* — the Anthropic SDK supports caching too; this is a future enhancement, not a blocker.
- *"Requires a second credential that may be unavailable"* — the only real operational concern, and it is resolved by provisioning a dedicated key rather than collapsing Agent 3 onto the proxy. Note the proxy *does* route to Claude (`LLM_MODEL=~anthropic/claude-sonnet-latest`), so "OpenAI SDK vs Claude" was a false dichotomy; the only real axis was transport, and we keep the direct Anthropic transport for Agent 3.

No code change. The placeholder hard-error at `_get_api_key()` (agent3.py:60) is the intended guard until the real key is set.

---

### BUG-4 (FIXED): Passing `tools=[]` to the Anthropic SDK crashes on every `pick_rule` call

**File:** `pipeline/agents/agent3.py:326` (was :331)

**What was happening:**
`pick_rule()` passed `tools=[]` (an explicitly empty list) to `client.messages.create()`. The Anthropic API treats the presence of the `tools` field as "tool-calling requested" and then rejects an empty list, so every `pick_rule` call raised an API validation error — making rule selection completely non-functional once a live key is present. The empty list was intended to *enforce* the bounded-action-space invariant (no tools on `pick_rule`); correct intent, wrong mechanism.

**Fix applied (2026-06-02):**
Removed the `tools=[]` argument from the `client.messages.create()` call entirely. Omitting the field is what actually enforces "no tool surface" — and the API accepts it. The bounded-action-space invariant is preserved (arguably strengthened: there is now genuinely no tools field on the `pick_rule` call). A comment at the call site documents why the argument is omitted rather than passed empty. Verified the module still imports cleanly.

---

## High — Broken behavior or wrong logic

---

### BUG-6b: The `agent3.pick_rule` docstring documents the wrong field names

**File:** `pipeline/agents/agent3.py:296–297`

**What's happening:**
The `pick_rule()` docstring says `applicable_rules` entries have keys `"rule_name"` and `"description"`. The actual entries (built by the engine) have keys `"name"` and `"describe"`. The docstring is wrong and will mislead anyone maintaining the agent or the engine.

**Proposed fix:**
Update the docstring to say `{"name": str, "describe": str}` to match what the engine actually sends.

---

## Medium — Documentation errors, missing tests, schema gaps

---

### BUG-8: `compiler2.py` references the wrong stage and artifact in its docstring

**File:** `pipeline/compilers/compiler2.py:5–6`

**What's happening:**
The module docstring says it reads from "Stage 2 output, `02_pluscal_impl.json`". Stage 2 is the cocotb testbench generator — it has nothing to do with Compiler 2. Compiler 2 receives RTL-style TLA+ produced by the Refinement Engine inside Stage 3, and that content never directly corresponds to a single artifact filename (it is generated in memory by `bridge.engine_spec_to_rtl_tla()`).

**Proposed fix:**
Update the docstring to say something like: "RTL-style TLA+ produced in-memory by `pipeline/refinement/bridge.py:engine_spec_to_rtl_tla()`, called from Stage 3 (`pipeline/nodes/stage3.py`)."

---

### BUG-9: `tests/test_dff.py` is documented but does not exist

**File:** `CLAUDE.md:109` — file missing from `tests/`

**What's happening:**
The CLAUDE.md developer guide documents the command `python3.11 tests/test_dff.py` as a key integration test for checking that a D flip-flop spec can flow through Stage 1 and Stage 3 (bypassing Stage 2). The file does not exist. Anyone following the setup guide will get a `ModuleNotFoundError`.

**Proposed fix:**
Create `tests/test_dff.py` with a minimal integration test: feed a D flip-flop NL prompt through Stage 1 to get a `SpecSummary`, then through Stage 3 to get Verilog, and assert the output contains `always @(posedge clk)`. Alternatively, remove the reference from CLAUDE.md and mark the test as deferred.

---

### BUG-10: CLAUDE.md artifact table does not match the actual artifact chain

**File:** `CLAUDE.md`, artifact chain table

**What's happening:**
The table lists `01_formal_spec.json` as the Stage 1 output and `02_pluscal_impl.json` as the Stage 2 output. Neither file exists. The authoritative artifact map (documented in both `graph.py` and `stage1.py`) is:

| File | Written by |
|------|-----------|
| `00_nl_spec.json` | `main.py` at run start |
| `01_summary.json` | Stage 1 (Agent 1) |
| `02_testbench_meta.json` + `02_testbench.py` | Stage 2 (cocotb generator) |
| `02_formal_spec.json` | Stage 3 (Agent 3) |
| `03_rtl_output.json` | Stage 3 (Compiler 2) |
| `04_evaluation.json` | Stage 4 (cocotb runner) |
| `refinement_chain.json` | Refinement Engine (inside Stage 3) |

Note: the `02_formal_spec.json` name is also confusing because it is produced by Stage 3, not Stage 2. Renaming it to `03_formal_spec.json` would be cleaner.

**Proposed fix:**
Update the CLAUDE.md table to match the above. Optionally rename `02_formal_spec.json` to `03_formal_spec.json` and update all references in `stage1.py`, `stage3.py`, `graph.py`, and the tests.

---

### BUG-13: No Pydantic schema validates the `status` envelope

**Files:** `pipeline/nodes/stage1.py:73`, `stage2.py:58`, `stage3.py:245`, `stage4.py:71`, etc.

**What's happening:**
LangGraph routes the entire pipeline based on the `"status"` field in each artifact JSON. But there is no Pydantic model for the outer `{"status": ..., "error": ...}` wrapper. Each stage manually patches the dict after calling `model_dump()`, like: `artifact["status"] = "success"`. A typo such as `"sucess"` or `"succes"` would be completely invisible and would silently cause LangGraph to route to the wrong branch (defaulting to `"error"` for any unrecognized string).

**Proposed fix:**
Create a generic `ArtifactEnvelope` Pydantic v2 model with `status: Literal["success", "error", "partial"]` and `error: str | None = None`. Every stage should construct its artifact through this model, so Pydantic catches status typos at write time rather than at routing time.

---

### BUG-N2: `stage2.py` docstring incorrectly attributes the generator to `agent2.py`

**File:** `pipeline/nodes/stage2.py:9`

**What's happening:**
The module docstring says: "deterministic (template-based, no LLM call in the current implementation in `pipeline/agents/agent2.py` / `pipeline/cocotb/generator.py`)." The actual implementation used is `pipeline/cocotb/generator.py`, imported as `from pipeline.cocotb.generator import generate_testbench`. The `agent2.py` file is stale and unused. Citing it in the docstring implies it is active, which is misleading.

**Proposed fix:**
Remove the `pipeline/agents/agent2.py` reference from the docstring.

---

## Low — Minor issues

---

### BUG-5: `pipeline/agents/agent2.py` is stale dead code

**File:** `pipeline/agents/agent2.py`

**What's happening:**
This file is an older copy of the testbench generator. It is never imported by anything in the pipeline — `stage2.py` correctly imports from `pipeline.cocotb.generator`. Additionally, `agent2.py` still uses `units="ns"` which is the cocotb 1.x API keyword; the current cocotb 2.x API uses `unit="ns"` (singular).

**Proposed fix:**
Delete `pipeline/agents/agent2.py`. It only causes confusion.

---

### BUG-N3: `pipeline/cocotb/generator.py` uses the cocotb 1.x clock API

**File:** `pipeline/cocotb/generator.py:14`

**What's happening:**
The generated testbench template uses `Clock(dut.clk, 10, units="ns")`. In cocotb 2.x, the correct keyword argument is `unit="ns"` (singular). The plural `units` was deprecated in cocotb 1.x and removed in 2.x. If cocotb 2.x is installed, every generated testbench will raise a `TypeError` when the clock is started.

**Proposed fix:**
Change `units="ns"` to `unit="ns"` on line 14 of `generator.py`.

---

### BUG-14: TLA+ module footer line is 7 characters shorter than the header

**Files:** `pipeline/compilers/compiler1.py:298`, `pipeline/refinement/bridge.py:172`, `bridge.py:252`

**What's happening:**
TLA+ requires the closing `====` line to be at least as long as the opening `---- MODULE name ----` line. The header is constructed as `f"{sep} MODULE {name} {sep}"` where `sep = "-" * 20`, which gives a length of `20 + 8 + len(name) + 1 + 20 = 49 + len(name)`. The footer formula `len(sep) * 2 + len(name) + 2` gives `42 + len(name)` — which is 7 characters short. TLC may reject specs with a too-short footer.

The same formula is used in all three places that emit TLA+ modules.

**Proposed fix:**
Change the footer formula to `len(sep) * 2 + len(" MODULE ") + len(name) + len(" ") = 49 + len(name)`. In code: `"=" * (len(sep) * 2 + 9 + len(name))` (where 9 = len(" MODULE ") + len(" ")).

---

### BUG-15: `agent3.py` hardcodes the model name

**File:** `pipeline/agents/agent3.py:89`

**What's happening:**
The model is set as `_MODEL = "claude-opus-4-5"` with an env-var override via `AGENT3_MODEL`. Once BUG-3 is fixed (migrating to the OpenAI SDK), there should be only one model env variable (`LLM_MODEL`) with no hardcoded fallback, matching Agent 1's pattern.

**Proposed fix:**
After fixing BUG-3, remove `_MODEL` and replace `_AGENT3_MODEL` with `os.environ["LLM_MODEL"]` — same as Agent 1.

---

### BUG-16: `runner.py` subprocess does not guarantee `PYTHONPATH` is set

**File:** `pipeline/cocotb/runner.py:179`

**What's happening:**
The vvp subprocess inherits the parent process environment with `env = {**os.environ, **env_overrides}`. If the user runs `python main.py` without `PYTHONPATH` pointing to the repo root, the generated cocotb testbench will fail to import `pipeline.*` modules. This works on a developer machine that has set up the environment but silently breaks in a fresh environment.

**Proposed fix:**
Explicitly inject `PYTHONPATH` into the subprocess environment. The repo root can be derived with `Path(__file__).resolve().parents[2]`. Add it to `env_overrides`:
```python
env_overrides["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
```
