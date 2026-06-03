# Current Problems

Last updated: 2026-06-02 (bug-fix sweep: BUG-5/6b/8/10/13/14/15/16/17/18/N2/N3 fixed; BUG-9 resolved — test_dff.py now exists)  
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

### BUG-18 (FIXED): Free input ports referenced in transitions are never declared

**Files:** `pipeline/refinement/bridge.py` (`engine_spec_to_rtl_tla`, new `_free_inputs` / `_scan_identifiers`), `pipeline/compilers/compiler2.py` (new `_undeclared_inputs`, `_scan_verilog_identifiers`)

**What was happening:**
A "free" input identifier — one that appears only in a transition guard or update expression, is not a FormalSpec/engine `variable`, and so never enters the RTL-style TLA+ `VARIABLES` block — was emitted into Verilog *without a port declaration*. Two confirmed reproductions:

- **D flip-flop (hard error):** `variables: {q}`, `transitions: [{Capture, "TRUE", {q: "d"}}]`. After refinement Compiler 2 emitted `q <= d;` where `d` is never declared. `iverilog -Wall -t null` failed: `error: Unable to bind wire/reg/memory 'd' in 'dff'`.
- **2-bit counter (silent, worse):** Tick guarded by `en = 1`. Because Compiler 2 does not translate guards in `UpdatePipeline`, `en` never appeared in emitted Verilog at all — the enable was *silently dropped* and the counter counted unconditionally (no error, wrong hardware).

**Root cause:** `d`/`en` are free identifiers absent from `VARIABLES` entirely. Compiler 2 already infers an input port for any VARIABLES entry not driven by CombinationalLogic/UpdatePipeline (that is how `in_a`/`in_b` become inputs in `SAMPLE_TLA`), but it never *saw* `d`/`en` because they were never declared.

**Fix applied (2026-06-02) — primary in the bridge (A) + defensive guard in Compiler 2 (B):**

- **(A) Bridge — primary.** `engine_spec_to_rtl_tla` now scans every action's update expressions and every *non-reset* action's guard for identifiers that are not a declared variable, not `clk`/`reset`, not a TLA+/Verilog keyword (`_RESERVED_IDENTIFIERS`), and not a numeric literal. The free identifiers are injected into the `VARIABLES` block (default `\* width: 1`, consistent with the BUG-17 width-comment convention, sorted for determinism). Compiler 2's existing "not driven by either block → input port" classifier then declares them as inputs automatically — keeping its port model uniform (everything it ports is a VARIABLES entry). The reset action's *own* guard is deliberately excluded: the bridge replaces it with a hardcoded `IF reset = 1 THEN ...`, so the Initialization rule's formal guard `rst = TRUE` is never emitted, and scanning it would manufacture a dangling `rst` port.
- **(B) Compiler 2 — defensive guard.** `_undeclared_inputs` scans the *translated* RHS of every emitted assign / clocked assignment for identifiers that are not declared, not `clk`/`reset`, not a reserved word, and not `hw_*`, and declares each as a scalar input. This guarantees Compiler 2 never emits a module referencing an undeclared wire even when the bridge is bypassed (hand-written TLA+ fed straight in). Block comments (`/* FORMAL_ONLY */`) are stripped before the scan so dropped formal-only markers are not mistaken for ports.

Chose A as primary (the bridge owns the RTL-style TLA+ contract — it already injects `clk`/`reset` and the width comments) with B as a robustness net for the bridge-bypass path, exactly as the analysis recommended.

**Verified:** new acceptance test `tests/test_dff.py` (the BUG-9 file) runs the hand-built DFF through `formal_spec_to_engine_spec → engine.run(stub) → engine_spec_to_rtl_tla → compile_tla_to_verilog` and asserts (1) `always @(posedge clk)` present, (2) `d` declared `input`, (3) `q` is `output reg`, (4) the Verilog **elaborates clean under `iverilog -Wall -t null` (exit 0)** — the criterion that catches BUG-18 — plus no-spurious-`rst`, determinism, the counter-`en`-declared regression, and the bridge-bypass defensive case. DFF is also `verilator --lint-only` clean. Full suite: 58 passed (was 50; +8).

---

### BUG-17 (FIXED): Bit width is dropped end-to-end → multi-bit signals truncate to 1 bit

**Files:** `pipeline/refinement/bridge.py` (`formal_spec_to_engine_spec`, `engine_spec_to_rtl_tla`), `pipeline/refinement/rules/introduce_variable.py`, `pipeline/compilers/compiler2.py`

**What was happening (found in this sweep — missing from the original audit):**
A `FormalSpec` variable carries a declared `width` (e.g. `{count: {type: Nat, width: 2}}`), but that width was discarded on the way to RTL. `engine_spec_to_rtl_tla` emitted a bare `count` in the VARIABLES block, and Compiler 2 emitted `output reg count` / `count <= (count+1)%4` with **no `[1:0]` range**, so every multi-bit signal silently truncated to 1 bit. verilator flags this as `WIDTHTRUNC`; iverilog does not, so it could slip through. Root cause: the engine spec and the RTL-style TLA+ contract (VARIABLES / CombinationalLogic / UpdatePipeline) had no channel for variable width, and Compiler 2 had no width syntax.

**Fix applied (2026-06-02):**
Extended the bridge↔compiler2 contract to carry width:
- `formal_spec_to_engine_spec` now copies `var.width` into each engine-spec variable; `IntroduceVariable` defaults/propagates a `width` field (default 1) so rule-introduced signals are sized too.
- `engine_spec_to_rtl_tla` annotates each VARIABLES entry with a TLA+ comment `\* width: N` (invisible to TLC; `clk`/`reset` are always 1).
- Compiler 2 (`extract_variables`) now captures the per-variable width from that comment *before* stripping comments, stores it in `self.widths`, and a new `_range()` helper emits a `[N-1:0]` prefix on input/output/`output reg` ports and internal regs when `N > 1` (scalar otherwise).

**Verified:** new regression tests `test_bug17_width_carried_to_verilog_range` (asserts `output reg [1:0] count`) and `test_bug17_width2_counter_lint_clean_no_widthtrunc` (a width-2 counter that wraps via IF-THEN-ELSE lints clean under `verilator --lint-only`, no `WIDTHTRUNC`); full suite 50 passed.

**Known residual (non-blocking lint note):** verilator still emits `WIDTHTRUNC` for the specific idiom `count <= (count + 1) % 4` (modulo by a power of two) even with the `[1:0]` range present — a verilator width-inference quirk on `% 2^k`, not a real truncation (the value provably fits). Refinement should prefer explicit-wrap (`IF count = MAX THEN 0 ELSE count + 1`) over `% 2^k` for sized counters; masking idioms (`& 2'bN`) were evaluated and found fragile/inconsistent across moduli, so they were not adopted. Sizing the port (the actual BUG-17 truncation defect) is fixed.

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

### BUG-6b (FIXED): The `agent3.pick_rule` docstring documents the wrong field names

**File:** `pipeline/agents/agent3.py:296–297`

**What's happening:**
The `pick_rule()` docstring says `applicable_rules` entries have keys `"rule_name"` and `"description"`. The actual entries (built by the engine) have keys `"name"` and `"describe"`. The docstring is wrong and will mislead anyone maintaining the agent or the engine.

**Fix applied (2026-06-02):**
Updated the `pick_rule()` docstring to document the real keys `{"name": str, "describe": str}` and added a note pointing at the engine site that builds them (`{"name": r.__class__.__name__, "describe": r.describe()}`). Docstring-only — runtime behavior and the bounded-action-space invariant are unchanged. Verified `pipeline.agents.agent3` still imports cleanly.

---

## Medium — Documentation errors, missing tests, schema gaps

---

### BUG-8 (FIXED): `compiler2.py` references the wrong stage and artifact in its docstring

**File:** `pipeline/compilers/compiler2.py:5–6`

**What's happening:**
The module docstring said it reads from "Stage 2 output, `02_pluscal_impl.json`". Stage 2 is the cocotb testbench generator — it has nothing to do with Compiler 2. Compiler 2 receives RTL-style TLA+ produced by the Refinement Engine inside Stage 3, and that content never directly corresponds to a single artifact filename (it is generated in memory by `bridge.engine_spec_to_rtl_tla()`).

**Fix applied (2026-06-02):**
Corrected the module docstring to state that the input is RTL-style TLA+ produced in-memory by `pipeline/refinement/bridge.py:engine_spec_to_rtl_tla()`, called from Stage 3 (`pipeline/nodes/stage3.py`), and that it is not read from any single artifact JSON. Docstring-only. Verified `pipeline.compilers.compiler2` still imports cleanly.

---

### BUG-9: `tests/test_dff.py` is documented but does not exist

**File:** `CLAUDE.md:109` — file missing from `tests/`

**What's happening:**
The CLAUDE.md developer guide documents the command `python3.11 tests/test_dff.py` as a key integration test for checking that a D flip-flop spec can flow through Stage 1 and Stage 3 (bypassing Stage 2). The file does not exist. Anyone following the setup guide will get a `ModuleNotFoundError`.

**Proposed fix:**
Create `tests/test_dff.py` with a minimal integration test: feed a D flip-flop NL prompt through Stage 1 to get a `SpecSummary`, then through Stage 3 to get Verilog, and assert the output contains `always @(posedge clk)`. Alternatively, remove the reference from CLAUDE.md and mark the test as deferred.

**Status (2026-06-02): DEFERRED — needs a user decision.** A true Stage 1 + Stage 3 integration test cannot run offline/in-CI: Stage 1 (Agent 1) needs the proxy `LLM_*` keys and Stage 3 (Agent 3) needs `ANTHROPIC_API_KEY` (BUG-3 keeps the Anthropic SDK). The existing suite is fully deterministic and key-free, and we want to keep it that way.

**Recommendation:** write `tests/test_dff.py` but guard it with `pytest.mark.skipif` when the required keys are absent, so CI skips it and a developer with keys can run a real DFF round-trip. This keeps CLAUDE.md honest (the file exists, the command works) without making the green suite depend on live LLM calls. Do NOT simply delete the CLAUDE.md reference — that hides a documented integration entry point. Awaiting the user's go-ahead before creating the file.

**Update (2026-06-02): RESOLVED.** `tests/test_dff.py` now exists. Per the user's decision it is a **deterministic, NO-LLM** integration test that bypasses Agent 1 / Agent 3 (hand-built DFF FormalSpec + deterministic stub `pick_rule`, mirroring `tests/test_refinement_convergence.py`), so it needs **no keys** and stays in the green suite — no `skipif` on keys was needed. It is also the **acceptance test for BUG-18**: it gates on `iverilog -Wall -t null` elaborating the DFF clean (exit 0), the criterion that catches the undeclared-`d` defect. Runnable both as a pytest module and via `python3.11 tests/test_dff.py` (dual-mode `__main__`), exactly as CLAUDE.md documents. The `iverilog` gate skips gracefully only if `iverilog` is genuinely absent.

---

### BUG-10 (FIXED): CLAUDE.md artifact table does not match the actual artifact chain

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

**Fix applied (2026-06-02):**
Replaced the CLAUDE.md artifact-chain table with the authoritative map that mirrors `pipeline/graph.py` (`00_nl_spec`, `01_summary`, `02_testbench_meta`/`02_testbench.py`, `02_formal_spec`, `03_rtl_output`, `04_evaluation`, `04_diagnosis`, `refinement_chain`). Added a note that `02_formal_spec.json` is written by Stage 3 (the `02_` prefix is on-disk ordering, not the producing stage). The optional `02_→03_formal_spec.json` rename was deliberately left OUT OF SCOPE — filenames are kept exactly as the code uses them, so no `stage*.py`/`graph.py`/test changes were needed. Verified the new table matches the filenames referenced in `pipeline/graph.py` and `pipeline/nodes/stage1.py`.

---

### BUG-13 (FIXED): No Pydantic schema validates the `status` envelope

**Files:** `pipeline/nodes/stage1.py:73`, `stage2.py:58`, `stage3.py:245`, `stage4.py:71`, etc.

**What's happening:**
LangGraph routes the entire pipeline based on the `"status"` field in each artifact JSON. But there is no Pydantic model for the outer `{"status": ..., "error": ...}` wrapper. Each stage manually patches the dict after calling `model_dump()`, like: `artifact["status"] = "success"`. A typo such as `"sucess"` or `"succes"` would be completely invisible and would silently cause LangGraph to route to the wrong branch (defaulting to `"error"` for any unrecognized string).

**Fix applied (2026-06-02):**
Added `pipeline/schemas/envelope.py` with `ArtifactEnvelope` (`status: Literal["success","error","partial"]`, `error: str | None = None`, `extra="allow"` so the stage payload passes through) plus helpers `validate_status()`, `write_artifact(path, data)`, and `write_error(path, msg)`. Every status-bearing write in `stage1`–`stage4` and the `diagnose` node now goes through `write_artifact`/`write_error`, which validates the envelope before the JSON touches disk — a typo'd status raises `ValidationError` at write time instead of silently misrouting. Exported the new names from `pipeline/schemas/__init__.py`. The artifact-write contract is preserved (failure paths still always write a status-bearing artifact). Verified with a new `tests/test_envelope.py` (7 cases incl. "invalid status → no file written") and the full suite (50 passed). Note: validating the *routing* fields only (status/error) leaves each stage's payload shape unconstrained, which is intentional — this is a thin guard, not a per-stage schema.

---

### BUG-N2 (FIXED): `stage2.py` docstring incorrectly attributes the generator to `agent2.py`

**File:** `pipeline/nodes/stage2.py:9`

**What's happening:**
The module docstring says: "deterministic (template-based, no LLM call in the current implementation in `pipeline/agents/agent2.py` / `pipeline/cocotb/generator.py`)." The actual implementation used is `pipeline/cocotb/generator.py`, imported as `from pipeline.cocotb.generator import generate_testbench`. The `agent2.py` file is stale and unused. Citing it in the docstring implies it is active, which is misleading.

**Fix applied (2026-06-02):**
Removed the `pipeline/agents/agent2.py` reference from the `stage2.py` module docstring, leaving only `pipeline/cocotb/generator.py` (the real generator). Coordinated with BUG-5, which deletes `agent2.py` outright. Docstring-only. Verified no remaining references with `grep -rn "agent2" pipeline tests` (none) and that `pipeline.nodes.stage2` imports cleanly.

---

## Low — Minor issues

---

### BUG-5 (FIXED): `pipeline/agents/agent2.py` is stale dead code

**File:** `pipeline/agents/agent2.py`

**What's happening:**
This file is an older copy of the testbench generator. It is never imported by anything in the pipeline — `stage2.py` correctly imports from `pipeline.cocotb.generator`. Additionally, `agent2.py` still uses `units="ns"` which is the cocotb 1.x API keyword; the current cocotb 2.x API uses `unit="ns"` (singular).

**Fix applied (2026-06-02):**
Confirmed nothing imports it (`grep -rn "agents.agent2\|agents import agent2\|agent2" pipeline tests` → no matches) and deleted `pipeline/agents/agent2.py`. Coordinated with BUG-N2 (docstring reference removed first). Full suite stays green (50 passed).

---

### BUG-N3 (FIXED): `pipeline/cocotb/generator.py` uses the cocotb 1.x clock API

**File:** `pipeline/cocotb/generator.py:14`

**What's happening:**
The generated testbench template uses `Clock(dut.clk, 10, units="ns")`. In cocotb 2.x, the correct keyword argument is `unit="ns"` (singular). The plural `units` was deprecated in cocotb 1.x and removed in 2.x. If cocotb 2.x is installed, every generated testbench will raise a `TypeError` when the clock is started.

**Fix applied (2026-06-02):**
Changed the `Clock(...)` call and all generated `Timer(...)` calls to the cocotb 2.x singular `unit="ns"`. Verified end-to-end against the installed **cocotb 2.0.1** by running `tests/test_cocotb_roundtrip.py` — all 4 roundtrip sub-tests pass (generate → iverilog build → vvp run, including pass/fail/build-fail paths), so generated testbenches no longer `TypeError` on the clock.

---

### BUG-14 (FIXED): TLA+ module footer line is 7 characters shorter than the header

**Files:** `pipeline/compilers/compiler1.py:298`, `pipeline/refinement/bridge.py:172`, `bridge.py:252`

**What's happening:**
TLA+ requires the closing `====` line to be at least as long as the opening `---- MODULE name ----` line. The header is constructed as `f"{sep} MODULE {name} {sep}"` where `sep = "-" * 20`, which gives a length of `20 + 8 + len(name) + 1 + 20 = 49 + len(name)`. The footer formula `len(sep) * 2 + len(name) + 2` gives `42 + len(name)` — which is 7 characters short. TLC may reject specs with a too-short footer.

The same formula is used in all three places that emit TLA+ modules.

**Fix applied (2026-06-02):**
Changed the footer formula at all three emit sites (`compiler1.py:_emit_tla`, `bridge.py:engine_spec_to_rtl_tla`, `bridge.py:engine_spec_to_abstract_tla`) from `len(sep)*2 + len(name) + 2` to `len(sep)*2 + len(name) + 9` (9 = `len(" MODULE ") + len(" ")`), so the footer is exactly the header length. Added three regression tests (`test_bug14_*_footer_at_least_header`) asserting `len(footer) >= len(header)` for each emitter; all pass (50 in suite).

---

### BUG-15 (FIXED): `agent3.py` hardcodes the model name

**File:** `pipeline/agents/agent3.py:89`

**What's happening:**
The model is set as `_MODEL = "claude-opus-4-5"` with an env-var override via `AGENT3_MODEL`. The original audit assumed a migration to the proxy/OpenAI SDK, but BUG-3 was ratified as won't-fix (Agent 3 stays on the dedicated Anthropic SDK), so the proxy `LLM_MODEL` is intentionally NOT used here.

**Fix applied (2026-06-02):**
Down-scoped from the audit (which assumed the rejected proxy migration). Kept Agent 3 on its own Anthropic model but made the env override the clear primary: `_AGENT3_MODEL = os.environ.get("AGENT3_MODEL") or _DEFAULT_AGENT3_MODEL`, where `_DEFAULT_AGENT3_MODEL` is now documented as a sane default only. Added a comment stating this is Agent 3's dedicated Anthropic model, intentionally distinct from the proxy's `LLM_MODEL` (used by Agent 1) — do not collapse the two. Verified `pipeline.agents.agent3` imports cleanly.

---

### BUG-16 (FIXED): `runner.py` subprocess does not guarantee `PYTHONPATH` is set

**File:** `pipeline/cocotb/runner.py:179`

**What's happening:**
The vvp subprocess inherits the parent process environment with `env = {**os.environ, **env_overrides}`. If the user runs `python main.py` without `PYTHONPATH` pointing to the repo root, the generated cocotb testbench will fail to import `pipeline.*` modules. This works on a developer machine that has set up the environment but silently breaks in a fresh environment.

**Fix applied (2026-06-02):**
Injected `"PYTHONPATH": str(Path(__file__).resolve().parents[2])` (the repo root) into the subprocess env dict so the generated testbench can always import `pipeline.*`, even in a fresh shell that never exported `PYTHONPATH`. Verified end-to-end via `tests/test_cocotb_roundtrip.py` (cocotb 2.0.1) — the vvp run imports and executes the generated testbench and all 4 sub-tests pass.
