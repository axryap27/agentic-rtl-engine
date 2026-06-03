# Current Problems — Resolved Ledger

**Last updated:** 2026-06-02
**Branch:** `pipeline-dev`
**Status:** All audited bugs closed (fixed or ratified won't-fix). Test suite: **58 passed**, fully deterministic / no live LLM calls. Working tree has uncommitted changes (committed by the user, not by tooling).

This file is the closed ledger for the bug-fix sweep on `pipeline-dev`. Every entry is resolved. The only open items are **deferred follow-ups** and **setup still pending** (below) — neither is a defect.

---

## Status at a glance

| Bug | Severity | Title | Disposition |
|-----|----------|-------|-------------|
| BUG-1 | — | Refinement engine format mismatch | ✅ Fixed (bridge) |
| BUG-2 | — | cocotb retry loop discarded revision | ✅ Fixed |
| BUG-3 | Critical | Agent 3 uses the Anthropic SDK | ✅ Won't-fix (intentional; see decision) |
| BUG-4 | Critical | `tools=[]` crashes every `pick_rule` call | ✅ Fixed |
| BUG-5 | Low | Stale `agents/agent2.py` dead code | ✅ Fixed (deleted) |
| BUG-6 | — | `pick_rule` fallback used wrong key | ✅ Fixed |
| BUG-6b | High | `pick_rule` docstring wrong field names | ✅ Fixed (docstring) |
| BUG-7 | — | Refinement engine output discarded | ✅ Fixed |
| BUG-8 | Medium | `compiler2.py` docstring wrong stage/artifact | ✅ Fixed (docstring) |
| BUG-9 | Medium | `tests/test_dff.py` documented but missing | ✅ Resolved (written, deterministic) |
| BUG-10 | Medium | CLAUDE.md artifact table mismatched code | ✅ Fixed |
| BUG-12 | — | `refinement_templates/` thought dead | ✅ Not a bug (in active use) |
| BUG-13 | Medium | No Pydantic guard on the `status` envelope | ✅ Fixed (`ArtifactEnvelope`) |
| BUG-14 | Low | TLA+ module footer shorter than header | ✅ Fixed (3 sites) |
| BUG-15 | Low | `agent3.py` hardcoded model name | ✅ Fixed (env override primary) |
| BUG-16 | Low | runner subprocess missing `PYTHONPATH` | ✅ Fixed |
| BUG-17 | Critical | Bit width dropped → multi-bit truncation | ✅ Fixed (bridge↔compiler2 width channel) |
| BUG-18 | Critical | Free input ports (`d`, `en`) never declared | ✅ Fixed (bridge + compiler2 guard) |
| BUG-N2 | Medium | `stage2.py` docstring cited deleted `agent2.py` | ✅ Fixed (docstring) |
| BUG-N3 | Low | cocotb 1.x `units=` clock API | ✅ Fixed (`unit=`) |
| CLAUDE.md drift | — | "use OpenAI SDK not anthropic" + `schemas.py` path | ✅ Fixed (verifier-caught) |

The two highest-value finds were **not** in the team's original audit: **BUG-17** (silent multi-bit truncation, surfaced during the sweep) and **BUG-18** (free input ports never declared — the DFF wouldn't elaborate and the counter's enable was silently dropped, surfaced by writing the BUG-9 DFF test).

---

## Setup still pending (not a bug — deferred work)

### Agent 3 SDK / API key — TO DO before any live full-pipeline run

Agent 3 is wired to the **Anthropic SDK** and needs `ANTHROPIC_API_KEY` set in `.env` to a real key. Until then it raises a clear, intentional error at call time (`_get_api_key()` in `agent3.py`), so Stages 1–2 still run but Stage 3 cannot.

What's left to do:
1. **Provision the key.** Create/set up a **separate Anthropic Console account** (`console.anthropic.com`) with **pay-as-you-go API credits** for Agent 3. This is billed independently from any Claude **subscription** (e.g. Max/Pro) — a subscription **cannot** drive the raw SDK; you need Console API credits. A small prepaid amount goes a long way (Agent 3 makes only a handful of calls per run).
2. **Add it to `.env`:** replace the placeholder `ANTHROPIC_API_KEY=__AGENT3_CLAUDE_AGENT_SDK_KEY__NOT_CONFIGURED_YET__` with the real key.
3. **(Optional) Pick the model:** `AGENT3_MODEL` overrides the default `claude-opus-4-5`. For cheap dev iterations, set a smaller model (e.g. a Haiku/Sonnet); raise to Opus for max quality. This is Agent 3's **own** model, intentionally distinct from the proxy's `LLM_MODEL` used by Agent 1 (see BUG-15).

Note on the other transports: **Agent 1 and the diagnoser** use the OpenAI-compatible **proxy** (`LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL`, currently the writingmate proxy routing to Claude). As of 2026-06-02 **no live LLM call has been made through any transport** — the entire test/verification effort is deterministic (hand-built specs + stub `pick_rule`), so no proxy or API tokens have been consumed yet.

### Deferred follow-up

- **Refinement should prefer explicit-wrap over `% 2^k` for sized counters.** Sizing is fixed (BUG-17), but verilator still emits a cosmetic `WIDTHTRUNC` on the `count <= (count + 1) % 4` idiom. Emitting `IF count = MAX THEN 0 ELSE count + 1` instead is fully lint-clean on both iverilog and verilator. Not a correctness issue; a polish item for the refinement/pick_rule policy.

---

## Diagnoser / routing agent (added this session)

A two-way failure classifier sits between Stage 4 and the Stage 3 revision paths:

- **`pipeline/agents/agent_diagnoser.py`** — reads `04_evaluation.json`, `02_formal_spec.json`, `refinement_chain.json`. Build failures (`phase == "build"`) are classified `"spec"` with **no** LLM call; test failures use an LLM to distinguish a spec fault (wrong core logic) from a refinement fault (wrong parameters — reset values, clock domains, update expressions).
- **`pipeline/nodes/diagnose.py`** — thin node wrapping the diagnoser; writes `04_diagnosis.json`, sets `state["last_diagnosis"]`, defaults to `"spec"` on any error so routing always has a valid signal.
- **`pipeline/nodes/stage3.py`** — `run_stage3_backtrack_refinement()`: keeps the FormalSpec, truncates `refinement_chain.json` by `BACKTRACK_STEPS`, replays to the truncation point, injects the diagnosis into the `pick_rule` prompt, re-runs the engine; pre-backtrack history saved to `refinement_chain_prefix.json`.
- **`pipeline/state.py`** — added `last_diagnosis: str | None`.
- **`pipeline/nodes/stage4.py`** — failure branch writes the full structured runner result (`phase`, `failed_vectors`, `raw`).
- **`pipeline/graph.py`** — added `diagnose` + `stage3_backtrack_refinement` nodes; `_route_after_stage4` routes to `"diagnose"` on failure; `_route_after_diagnose` forks the two revision paths.

The diagnoser is a runtime LLM agent (proxy transport). It will also need the proxy keys to run live.

---

## Critical — fixed

### BUG-18 (FIXED): Free input ports referenced in transitions are never declared

**Files:** `pipeline/refinement/bridge.py` (`engine_spec_to_rtl_tla`, new `_free_inputs` / `_scan_identifiers`); `pipeline/compilers/compiler2.py` (new `_undeclared_inputs`, `_scan_verilog_identifiers`).

**What was happening:** a "free" input identifier — appearing only in a transition guard or update expression, not a FormalSpec/engine `variable`, and so never entering the RTL-style TLA+ `VARIABLES` block — was emitted into Verilog *without a port declaration*. Two confirmed reproductions:
- **D flip-flop (hard error):** `variables: {q}`, `transitions: [{Capture, "TRUE", {q: "d"}}]`. Compiler 2 emitted `q <= d;` with `d` undeclared → `iverilog -Wall -t null` failed: `error: Unable to bind wire/reg/memory 'd' in 'dff'`.
- **2-bit counter (silent, worse):** Tick guarded by `en = 1`. Compiler 2 does not translate `UpdatePipeline` guards, so `en` never appeared in Verilog at all — the enable was **silently dropped** and the counter counted unconditionally (no error, wrong hardware).

**Root cause:** `d`/`en` are free identifiers absent from `VARIABLES` entirely. Compiler 2 already infers an input port for any VARIABLES entry not driven by CombinationalLogic/UpdatePipeline (that is how `in_a`/`in_b` become inputs in `SAMPLE_TLA`), but it never *saw* `d`/`en`.

**Fix (primary in bridge, defensive guard in Compiler 2):**
- **(A) Bridge — primary.** `engine_spec_to_rtl_tla` scans every action's update expressions and every *non-reset* action's guard for identifiers that are not a declared variable, not `clk`/`reset`, not a reserved word, and not a numeric literal, and injects them into `VARIABLES` (`\* width: 1`, sorted). Compiler 2's existing "not driven → input port" classifier then declares them, keeping its port model uniform. The reset action's own guard is deliberately excluded (the bridge replaces it with a hardcoded `IF reset = 1 THEN ...`, so the Initialization rule's formal guard `rst = TRUE` is never emitted — scanning it would manufacture a dangling `rst` port).
- **(B) Compiler 2 — defensive.** `_undeclared_inputs` scans the translated RHS of every emitted assign/clocked assignment for undeclared identifiers and declares each as a scalar input, so Compiler 2 never emits a module referencing an undeclared wire even when the bridge is bypassed (hand-written TLA+). `/* FORMAL_ONLY */` block comments are stripped before the scan.

**Verified:** `tests/test_dff.py` runs the hand-built DFF through `formal_spec_to_engine_spec → engine.run(stub) → engine_spec_to_rtl_tla → compile_tla_to_verilog` and asserts `always @(posedge clk)` present, `d` declared `input`, `q` is `output reg`, **and the Verilog elaborates clean under `iverilog -Wall -t null` (exit 0)** — plus no-spurious-`rst`, determinism, the counter-`en`-declared regression, and the bridge-bypass defensive case. DFF is also `verilator --lint-only` clean. Independently re-derived: DFF + counter both declare their free inputs and elaborate clean; counter retains `[1:0]` width.

---

### BUG-17 (FIXED): Bit width dropped end-to-end → multi-bit signals truncate to 1 bit

**Files:** `pipeline/refinement/bridge.py` (`formal_spec_to_engine_spec`, `engine_spec_to_rtl_tla`), `pipeline/refinement/rules/introduce_variable.py`, `pipeline/compilers/compiler2.py`.

**What was happening (found in this sweep — missing from the original audit):** a `FormalSpec` variable carries a declared `width` (e.g. `{count: {type: Nat, width: 2}}`), but width was discarded on the way to RTL. `engine_spec_to_rtl_tla` emitted a bare `count`, and Compiler 2 emitted `output reg count` / `count <= ...` with **no `[1:0]` range**, so every multi-bit signal silently truncated to 1 bit. verilator flags `WIDTHTRUNC`; iverilog does not, so it could slip through.

**Fix:** extended the bridge↔compiler2 contract to carry width — `formal_spec_to_engine_spec` copies `var.width` into each engine-spec variable; `IntroduceVariable` propagates a `width` field (default 1) so rule-introduced signals are sized; `engine_spec_to_rtl_tla` annotates each VARIABLES entry with `\* width: N` (invisible to TLC; `clk`/`reset` always 1); Compiler 2's `extract_variables` captures that width before stripping comments and a `_range()` helper emits a `[N-1:0]` prefix when `N > 1` (scalar otherwise).

**Verified:** regression tests `test_bug17_width_carried_to_verilog_range` (asserts `output reg [1:0] count`) and `test_bug17_width2_counter_lint_clean_no_widthtrunc` (width-2 counter via IF-THEN-ELSE, `verilator --lint-only` clean, no `WIDTHTRUNC`).

**Residual (cosmetic, deferred):** verilator still warns `WIDTHTRUNC` for the `count <= (count + 1) % 4` idiom even with the `[1:0]` range — a verilator inference quirk on `% 2^k`, not a real truncation. With explicit-wrap (`IF count = 3 THEN 0 ELSE count + 1`) it is fully clean on both linters. See the deferred follow-up above.

---

### BUG-3 (RESOLVED — won't fix; Anthropic SDK is intentional)

**File:** `pipeline/agents/agent3.py`.

**Decision (2026-06-02):** Agent 3 **stays on the Anthropic SDK** with its own `ANTHROPIC_API_KEY` (locked decision #3: Agent 3 is a distinct, tool-using Claude agent). Not a defect. The original report's three concerns were re-examined: "bypasses the proxy" is by design; "no prompt caching" is a future enhancement (the Anthropic SDK supports caching); "requires a second credential" is the only real operational concern, resolved by provisioning a dedicated key (see *Setup still pending*) rather than collapsing Agent 3 onto the proxy. The proxy itself routes to Claude (`LLM_MODEL=~anthropic/...`), so "OpenAI SDK vs Claude" was a false dichotomy — the only real axis is transport, and Agent 3 keeps the direct Anthropic transport. No code change; the placeholder hard-error at `_get_api_key()` is the intended guard until the real key is set.

---

### BUG-4 (FIXED): `tools=[]` crashes every `pick_rule` call

**File:** `pipeline/agents/agent3.py` (the `pick_rule` `messages.create` call).

**What was happening:** `pick_rule()` passed `tools=[]` to `client.messages.create()`. The Anthropic API treats the presence of `tools` as "tool-calling requested" then rejects an empty list, so every `pick_rule` call would raise an API validation error once a live key is present. The empty list was meant to *enforce* the bounded-action-space invariant (no tools on `pick_rule`) — correct intent, wrong mechanism.

**Fix:** removed the `tools=[]` argument entirely. Omitting the field is what actually enforces "no tool surface," and the API accepts it — the bounded-action-space invariant is preserved (arguably strengthened: there is now genuinely no tools field on the call). A comment documents why it is omitted rather than passed empty.

---

## High — fixed

### BUG-6b (FIXED): `pick_rule` docstring documented the wrong field names

**File:** `pipeline/agents/agent3.py`. The docstring claimed `applicable_rules` entries have keys `"rule_name"`/`"description"`; the engine actually sends `{"name", "describe"}`. Updated the docstring to the real keys and pointed at the engine site that builds them (`{"name": r.__class__.__name__, "describe": r.describe()}`). Docstring-only; runtime and invariant unchanged.

---

## Medium — fixed

### BUG-8 (FIXED): `compiler2.py` docstring referenced the wrong stage/artifact

**File:** `pipeline/compilers/compiler2.py`. Docstring claimed it reads "Stage 2 output, `02_pluscal_impl.json`". Corrected to state the input is RTL-style TLA+ produced in-memory by `bridge.engine_spec_to_rtl_tla()`, called from Stage 3 — not read from any artifact JSON. Docstring-only.

### BUG-9 (RESOLVED): `tests/test_dff.py` documented but missing

**File:** previously absent; now `tests/test_dff.py`. CLAUDE.md documents `python3.11 tests/test_dff.py` as the DFF integration test; the file did not exist. Per the user's decision it was written as a **deterministic, NO-LLM** test that bypasses Agent 1/Agent 3 (hand-built DFF FormalSpec + stub `pick_rule`, mirroring `tests/test_refinement_convergence.py`), so it needs **no keys** and stays in the green suite. It doubles as the **acceptance test for BUG-18** (gates on `iverilog` elaborating the DFF clean). Runnable as a pytest module and via `python3.11 tests/test_dff.py` (dual-mode `__main__`), exactly as CLAUDE.md documents; the `iverilog` gate skips gracefully only if `iverilog` is genuinely absent.

### BUG-10 (FIXED): CLAUDE.md artifact table mismatched the code

**File:** `CLAUDE.md`. The table listed `01_formal_spec.json`/`02_pluscal_impl.json`, neither of which exists. Replaced with the authoritative map mirroring `pipeline/graph.py` (`00_nl_spec`, `01_summary`, `02_testbench_meta`/`02_testbench.py`, `02_formal_spec`, `03_rtl_output`, `04_evaluation`, `04_diagnosis`, `refinement_chain`), with a note that `02_formal_spec.json` is written by Stage 3 (the `02_` prefix is on-disk ordering). The optional `02→03_formal_spec.json` rename was left out of scope — filenames are kept exactly as the code uses them.

### BUG-13 (FIXED): No Pydantic guard on the `status` envelope

**Files:** `pipeline/schemas/envelope.py` (new); `pipeline/nodes/stage1.py`–`stage4.py`, `diagnose.py`; `pipeline/schemas/__init__.py`. LangGraph routes on the `"status"` field, but each stage hand-patched `dict["status"] = "..."` with no validation — a typo like `"sucess"` would silently misroute. Added `ArtifactEnvelope` (`status: Literal["success","error","partial"]`, `error: str | None`, `extra="allow"` so the payload passes through) plus `write_artifact()`/`write_error()` helpers; every status-bearing write now goes through them, so a bad status raises `ValidationError` at write time. The artifact-write contract is preserved (failure paths still always write). Verified by `tests/test_envelope.py` (incl. "invalid status → no file written"). Intentionally validates only the routing fields, not each stage's payload shape — a thin guard, not a per-stage schema.

### BUG-N2 (FIXED): `stage2.py` docstring cited the deleted `agent2.py`

**File:** `pipeline/nodes/stage2.py`. Removed the `pipeline/agents/agent2.py` reference (real generator is `pipeline/cocotb/generator.py`) and relabeled the module header from "Agent 2 / cocotb testbench generator" to "deterministic cocotb testbench generator." Coordinated with BUG-5 (which deletes `agent2.py`). Docstring-only; `grep -rn "agent2" pipeline tests` → none.

---

## Low — fixed

### BUG-5 (FIXED): stale `agents/agent2.py` dead code

Confirmed nothing imports it, then deleted `pipeline/agents/agent2.py`. Coordinated with BUG-N2 (docstring reference removed first).

### BUG-N3 (FIXED): cocotb 1.x `units=` clock API

**File:** `pipeline/cocotb/generator.py`. Changed the `Clock(...)` and generated `Timer(...)` calls from the removed-in-2.x `units="ns"` to singular `unit="ns"`. Verified against installed **cocotb 2.0.1** via `tests/test_cocotb_roundtrip.py` (generate → iverilog build → vvp run; pass/fail/build-fail paths all green).

### BUG-14 (FIXED): TLA+ module footer shorter than header

**Files:** `pipeline/compilers/compiler1.py`, `pipeline/refinement/bridge.py` (two emit sites). The footer formula gave `42 + len(name)` vs. the header's `49 + len(name)` — 7 chars short, which TLC may reject. Changed all three sites to `len(sep)*2 + len(name) + 9` so footer length equals header length. Regression tests assert `len(footer) >= len(header)` at each emitter.

### BUG-15 (FIXED): `agent3.py` hardcoded model name

**File:** `pipeline/agents/agent3.py`. Down-scoped from the audit (which assumed the rejected proxy migration). Made the env override the clear primary: `_AGENT3_MODEL = os.environ.get("AGENT3_MODEL") or _DEFAULT_AGENT3_MODEL`, with `_DEFAULT_AGENT3_MODEL` documented as a sane default only, and a comment that this is Agent 3's dedicated Anthropic model — intentionally distinct from the proxy's `LLM_MODEL` (Agent 1). Do not collapse the two.

### BUG-16 (FIXED): runner subprocess missing `PYTHONPATH`

**File:** `pipeline/cocotb/runner.py`. Injected `"PYTHONPATH": str(Path(__file__).resolve().parents[2])` (repo root) into the subprocess env so the generated testbench can import `pipeline.*` even in a fresh shell. Verified end-to-end via `tests/test_cocotb_roundtrip.py` (cocotb 2.0.1).

---

## CLAUDE.md doc drift (FIXED — caught by adversarial verification)

The bug-fix sweep's own changes were adversarially verified; two stale CLAUDE.md claims were found and corrected:
- **LLM-client section** said "Use the OpenAI-compatible SDK, not the `anthropic` SDK" with no carve-out — now contradicted by the ratified BUG-3 decision. Rewritten to split transports: Stages 1–2 + diagnoser use the OpenAI-compatible proxy; **Agent 3 uses the Anthropic SDK directly** (`ANTHROPIC_API_KEY`/`AGENT3_MODEL`).
- **Code-style section** said schemas live in `pipeline/schemas.py` (a file). The code uses a `pipeline/schemas/` **package** (`summary_schema.py`, `tla_schema.py`, `envelope.py`, …). Corrected, and noted that `ArtifactEnvelope` validates the `status` field.

---

## Not bugs (recorded for clarity)

- **BUG-1, BUG-2, BUG-6, BUG-7** — earlier seam fixes (FormalSpec↔engine bridge, cocotb retry handoff, `pick_rule` fallback key, engine output wiring), all resolved before this sweep.
- **BUG-12** — `refinement_templates/` is **not** dead code: `stage3.py` imports all six pass configurations to structure the multi-pass refinement loop.
- **`docs/handoff_runtime_agents.md`** still describes Agent 2 / `agent2.py` — this is a design-phase historical doc that predates the deletion, not a current error. BUG-5 records the deletion.
