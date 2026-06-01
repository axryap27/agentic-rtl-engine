# Current Problems

Last updated: 2026-06-01

---

## Critical — These break the pipeline entirely

---

### BUG-1: The refinement engine never actually runs

**File:** `pipeline/nodes/stage3.py`, line 212

**What's happening:**
The refinement engine has its own internal format for describing a hardware spec. It expects `variables` to be a list of objects, each with fields like `abstract`, `reset_value`, and `clocked`. It also expects actions to be under the key `"actions"`.

Stage 3 passes the spec directly from `FormalSpec.model_dump()` — but that produces a completely different shape: `variables` is a plain dictionary (name → type info, no `abstract` or `clocked` fields), and actions are stored under the key `"transitions"`, not `"actions"`.

The engine calls `spec.get("actions", [])` and gets an empty list, so it thinks there's nothing to refine and stalls immediately. Even before that, every rule's `is_applicable()` tries to loop over `spec["variables"]` expecting a list of dicts, but instead gets a plain dict — causing an `AttributeError: 'str' object has no attribute 'get'` on the very first iteration.

There is no translation step anywhere in the codebase that converts the FormalSpec format into the engine's expected format. The comment in the code even says "after translation into the engine's variable/action shape" — but that translation function was never written.

**Proposed fix:**
Write a small translation function (e.g. `formal_spec_to_engine_format(spec_dict)`) that converts `FormalSpec.model_dump()` output into the shape the engine expects. This means:
- Convert `variables` from `{name: {type, width}}` into `[{name, type, width, abstract: False, reset_value: None, clocked: True}]`
- Rename `transitions` to `actions`
- Fill in any missing fields with sensible defaults

Call this function in `stage3.py` before passing the spec to the engine.

---

### BUG-2: The cocotb retry loop does nothing

**File:** `pipeline/graph.py`, lines 80–134

**What's happening:**
When Verilog synthesis fails cocotb verification, the pipeline is supposed to revise the formal spec based on the failure and re-run Stage 3. The function `run_stage3_revise_cocotb()` does correctly revise the spec and write it back to disk. But then it calls `run_stage3(state)` — and `run_stage3()` starts by unconditionally calling `agent1.generate_formal_spec(summary)`, which generates a completely new spec from scratch using the original NL prompt.

The revised spec that was just saved to disk is immediately overwritten with a freshly generated one that knows nothing about the cocotb failure. The correction is thrown away before it can be used.

**Proposed fix:**
`run_stage3()` should check whether a revised spec already exists (e.g. by checking a flag in the pipeline state, or by looking for a `"revised": true` field in the artifact). If a revised spec is present, skip the LLM call and use it directly. Alternatively, split the function into two: one that generates a fresh spec and one that uses whatever spec is already on disk.

---

### BUG-3: Agent 3 uses the wrong LLM SDK

**File:** `pipeline/agents/agent3.py`, lines 31 and 81

**What's happening:**
The project is designed to route all LLM calls through a proxy server configured by environment variables (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`). Agent 1 does this correctly using the OpenAI-compatible SDK. Agent 3 completely ignores this setup and directly calls Anthropic's API using the `anthropic` Python package — which requires a separate `ANTHROPIC_API_KEY` and bypasses the proxy entirely.

This means Agent 3 won't work in any environment where the proxy is the intended access point (e.g., a university-provisioned API key). It also means Agent 3 doesn't benefit from the proxy's prompt caching.

**Proposed fix:**
Replace the `anthropic.Anthropic` client in `agent3.py` with an `openai.OpenAI` client configured from `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL`, matching the pattern in `agent1.py`. Rewrite the `_run_with_tools` and `pick_rule` methods to use the OpenAI chat completions API.

---

### BUG-4: Passing `tools=[]` crashes the Anthropic SDK

**File:** `pipeline/agents/agent3.py`, line 331

**What's happening:**
In `pick_rule()`, the code calls `client.messages.create(tools=[])` with an explicitly empty tools list. The Anthropic API rejects this — if you pass `tools`, it must contain at least one entry. This raises an API validation error on every single call to `pick_rule`, making it completely non-functional.

This is related to BUG-3: once Agent 3 is migrated to the OpenAI SDK (the correct approach), this particular crash goes away. But if someone keeps the Anthropic SDK, the `tools` parameter must simply be omitted when there are no tools to pass.

**Proposed fix:**
Remove the `tools=[]` argument from the `client.messages.create()` call. Only pass `tools` when the list is non-empty.

---

## High — Wrong behavior or broken integrations

---

### BUG-5: `agent2.py` is stale dead code with the wrong cocotb API

**File:** `pipeline/agents/agent2.py`

**What's happening:**
This file appears to be an old copy of the testbench generator that was never cleaned up. The actual testbench generator used by the pipeline is `pipeline/cocotb/generator.py`, which `stage2.py` imports correctly. `agent2.py` is never imported by anything.

On top of being unused, it uses `units="ns"` which is the cocotb 1.x API. The correct keyword in cocotb 2.x is `unit="ns"`. If this file were ever imported, it would produce testbenches with a broken time unit that silently gets ignored.

**Proposed fix:**
Delete `pipeline/agents/agent2.py`. It's dead code that only causes confusion.

---

### BUG-6: The `pick_rule` interface uses inconsistent field names

**File:** `pipeline/refinement/engine.py` line 312 vs `pipeline/agents/agent3.py` line 297

**What's happening:**
When the engine asks the LLM to pick a refinement rule, it sends a list of available rules. Each entry has the keys `"name"` and `"describe"`. However, the docstring and prompt in `agent3.py` call these fields `"rule_name"` and `"description"`. The LLM receives one set of names but the surrounding code and documentation describe a different set.

This inconsistency can confuse whoever reads the code, and if the LLM prompt is ever updated to match the docstring, it would break the parsing logic that expects `"name"`.

**Proposed fix:**
Pick one consistent naming convention and apply it everywhere. The engine's actual output keys (`"name"` and `"describe"`) are what matters at runtime, so update the `agent3.py` docstring and prompt to match: use `"name"` and `"describe"`.

---

### BUG-7: The refinement engine's output is discarded

**File:** `pipeline/nodes/stage3.py`, lines 221–222

**What's happening:**
After the refinement engine runs, Stage 3 checks whether the result contains a key called `"tla_source"`. But `engine.run()` returns the refined spec dict — which never has a `"tla_source"` key. The engine was not designed to emit TLA+ source; that's Compiler 2's job.

Because the check always fails, the code falls back to the original, unrefined TLA+ from Compiler 1. The entire output of the refinement engine is ignored and thrown away. Compiler 2 synthesizes Verilog from the same spec it would have gotten without any refinement.

**Proposed fix:**
After the engine returns the refined spec dict, pass it directly to Compiler 2's input instead of trying to find a `"tla_source"` string in it. Compiler 2 should accept the structured spec dict and generate TLA+ from that. Remove the dead `"tla_source"` check.

---

### BUG-8: `compiler2.py` references the wrong stage and artifact in its docstring

**File:** `pipeline/compiler/compiler2.py`, lines 5–6

**What's happening:**
The module docstring says it reads from "Stage 2 output, `02_pluscal_impl.json`". Stage 2 is actually the testbench generator and has nothing to do with Compiler 2. Compiler 2 reads the output of Stage 3 / the Refinement Engine, which is `03_rtl_output.json`.

**Proposed fix:**
Update the docstring to say "Stage 3 / Refinement Engine output, `03_rtl_output.json`".

---

### BUG-9: `tests/test_dff.py` is documented but doesn't exist

**File:** `CLAUDE.md`, line 109 — file missing from `tests/`

**What's happening:**
The CLAUDE.md developer guide documents a command `python3.11 tests/test_dff.py` as a key integration test (D flip-flop roundtrip through Stage 1 and Stage 3). The file does not exist. Anyone following the dev guide to verify their setup will get a `ModuleNotFoundError`.

**Proposed fix:**
Create `tests/test_dff.py` as a minimal integration test that runs a D flip-flop NL spec through Stages 1 and 3 (bypassing Stage 2) and checks that the Verilog output contains a clocked always block. Alternatively, remove the reference from CLAUDE.md if this test is intentionally deferred.

---

## Medium — Documentation errors and schema gaps

---

### BUG-10: CLAUDE.md artifact table doesn't match reality

**File:** `CLAUDE.md`, artifact chain table

**What's happening:**
The table documents `01_formal_spec.json` (Stage 1) and `02_pluscal_impl.json` (Stage 2). Neither file exists. The actual artifact chain is:
- Stage 1 writes `01_summary.json`
- Stage 2 writes `02_testbench_meta.json` and `02_testbench.py`
- Stage 3 writes `02_formal_spec.json` (note: Stage 3 writes a file named 02, which itself is confusing)
- Stage 3 also writes `03_rtl_output.json`

**Proposed fix:**
Update the table to reflect the actual filenames. Also consider renaming `02_formal_spec.json` to `03_formal_spec.json` since it is produced by Stage 3, not Stage 2.

---

### BUG-11: `main.py` reads fallback artifacts in the wrong order

**File:** `main.py`, line 104

**What's happening:**
When the pipeline halts before completing, `main.py` tries to read `02_formal_spec.json` before `01_summary.json` as a fallback. But `02_formal_spec.json` is written by Stage 3. If the pipeline failed during Stage 1, that file won't exist, but `01_summary.json` might. The current order means a Stage 1 failure will silently try to open a file that doesn't exist.

**Proposed fix:**
Reverse the order to `["01_summary.json", "02_formal_spec.json"]` so the earliest available artifact is tried first.

---

### BUG-12: `refinement_templates/` is dead code from an old architecture

**Files:** `pipeline/refinement_templates/pass1_fsm.py` through `pass6_checker.py`

**What's happening:**
These six files define prompt templates for a multi-pass LLM architecture that was apparently replaced by the current three-agent design. None of them are imported or used anywhere in the codebase.

**Proposed fix:**
Delete the `pipeline/refinement_templates/` directory. If they might be useful for reference, move them to `docs/` or a `legacy/` folder with a note explaining they are not active.

---

### BUG-13: No Pydantic schema validates the status envelope

**Files:** `pipeline/schemas.py`, `pipeline/nodes/stage1.py`, `pipeline/nodes/stage2.py`, etc.

**What's happening:**
Every artifact JSON has an outer wrapper like `{"status": "success", "error": null, ...data...}`. The `status` field is what LangGraph uses to decide routing. But there's no Pydantic model for this envelope — it's manually assembled by patching a `model_dump()` result after the fact. A typo like `"sucess"` or `"sucesss"` would be completely invisible and would silently route the pipeline to the wrong branch.

**Proposed fix:**
Create a generic `ArtifactEnvelope` Pydantic model with a `status: Literal["success", "error", "partial"]` field and an optional `error: str | None` field. Wrap every artifact in this model before writing to disk, so Pydantic catches any status typo at write time.

---

## Low — Minor issues

---

### BUG-14: TLA+ module footer line is too short

**File:** `pipeline/compiler/compiler1.py`, line 298

**What's happening:**
TLA+ specs use a specific format: the module header is a line of dashes like `---- MODULE name ----` and the footer is a line of equals signs that must be at least as long as the header. The length calculation for the footer drops the ` MODULE ` part (8 characters), making the footer shorter than the header for any non-trivial module name. TLC may reject or misparse specs with a short footer.

**Proposed fix:**
Change the length formula to include the 8-character `" MODULE "` segment: `len(sep) * 2 + len(" MODULE ") + len(name) + 2`.

---

### BUG-15: `agent3.py` hardcodes the model name

**File:** `pipeline/agents/agent3.py`, line 89

**What's happening:**
The fallback model name `"claude-opus-4-5"` is hardcoded. While an environment variable override exists, the fallback string may refer to a model that has been retired. Once BUG-3 is fixed (migrating to the OpenAI-compatible SDK), this should use `LLM_MODEL` from the environment with no hardcoded fallback, matching the pattern in `agent1.py`.

**Proposed fix:**
After fixing BUG-3, change this to `model = os.environ["LLM_MODEL"]` with no default, consistent with the rest of the codebase.

---

### BUG-16: `runner.py` subprocess may not have `PYTHONPATH` set

**File:** `pipeline/cocotb/runner.py`, lines 170–180

**What's happening:**
The cocotb testbench subprocess inherits the parent process's environment. If the user runs `python main.py` without having set `PYTHONPATH` to the repo root beforehand, the testbench won't be able to import from `pipeline.*`. This works on the developer's machine if they've set up their environment, but will silently fail in a fresh environment.

**Proposed fix:**
Explicitly set `PYTHONPATH` in the subprocess environment to include the repo root. You can get the repo root with `Path(__file__).resolve().parents[2]` and inject it as `env["PYTHONPATH"] = str(repo_root)`.
