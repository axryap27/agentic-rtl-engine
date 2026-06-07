# Test-Comprehensiveness Audit — Agentic RTL Engine

**Date:** 2026-06-06
**Scope:** `tests/` (deterministic, free) and `agentic_tests/` (live-LLM, triple-gated)
**Goal under test:** a user describes a *medium*-complexity digital system in natural language, and the pipeline emits **50–100 lines of perfect, synthesizable Verilog-2001** in a `.v` file, verified by cocotb. "Medium" = FSM+datapath, sync FIFO, UART tx/rx, round-robin arbiter, ALU, SPI/I2C controller — *not* a bare counter/DFF, *not* a CPU.
**Method:** 41-agent parallel audit — 10 test files + 12 source subsystems deep-read, 20 candidate gaps synthesized, top 16 adversarially verified (skeptics searched the repo to refute each by finding existing coverage). **16/16 confirmed real, 0 refuted.** The 6 bug-class findings were then re-verified by hand against the code (see badges below).

---

## 1. Verdict

**The suite is not comprehensive enough to defend the stated goal — and its gaps are masking real bugs.** It proves the project can compile and lint a 2-bit counter and a D flip-flop — designs the goal explicitly excludes as too easy — but there is **zero evidence the pipeline can produce correct medium-complexity RTL** (FIFO/UART/arbiter/ALU/FSM+datapath), the actual target. The two most goal-critical claims are entirely unverified: no test runs the full LangGraph from an NL prompt to a cocotb PASS (G01), and no deterministic test ever *simulates* generated RTL to confirm it computes the right next-state — "correct RTL" is everywhere reduced to "it lints/elaborates" (G03). Worse, the suite is green *while* several confirmed correctness bugs (branch collapse G12, rule-name drift G09, dead critic passes G10, banlist blindness G04) sit on exactly the multi-branch FSM/datapath logic medium designs require — undetected because no fixture exceeds toggle/counter complexity. Where the suite is real (envelope schema, BUG-17/18 regressions, the cocotb good/mutant discriminator), it is genuinely good — but most of that strength is locked behind non-pytest-collectable files or trivial fixtures.

**Bottom line:** G12 + G09 + G10 + G04 together imply the pipeline very likely *cannot* hit the 50–100-line medium-RTL goal today — and the test suite cannot tell you that, because nothing exercises a design big enough to trip them.

---

## 1.5 Resolution Status — Wave 1 (fixes) + Wave 2 (tests) + Wave 3 (D-series fixes) · updated 2026-06-07

**Wave 1 (production fixes, committed in `564cf8a` / `7e59f79`):** all six confirmed bugs are fixed and proven by integration (a 3-branch FSM now emits a correct nested ternary, lints clean, and is rejected if multi-driven). **Wave 2 (deterministic test suite):** ~140 tests added across 9 files; surfaced D1–D5 as xfail. **Wave 3 (D-series fixes, 2026-06-07):** D1–D5 fixed plus two companion fixes the medium fixtures revealed (cocotb runner path resolution + generator input initialisation). **Suite now: 204 passed, 6 xfailed, 0 failed** (Wave 2 was 198 passed / 12 xfailed). The 6 remaining xfails are all G14 cocotb-generator value/clock fragilities (still deferred).

> **Wave 3 bottom line — the headline goal is met end-to-end.** The full LangGraph now runs NL → RTL → **cocotb PASS** on two medium designs offline: a traffic-light FSM (`test_graph_stage4_cocotb_passes`) and a multi-op ALU (`test_alu_freeinput_width_correct`), both asserting the graph's own `04_evaluation.json == "success"` / a real cocotb PASS. D1 (timescale) alone was insufficient — the cocotb **runner** passed a relative `.vvp` path while running vvp with a changed cwd, so the graph's relative artifact paths doubled up and the build silently produced no binary; absolutising the runner's inputs unblocked it. D2's free-input width inference makes the ALU's 2-bit `op` select all four ops correctly. D5's enable gating exposed a generator gap (inputs left X during the reset preamble poison a self-feeding register), fixed by initialising inputs to 0.

### Bug fixes (Wave 1) — ✅ DONE

| Gap | Fix | Where |
|---|---|---|
| **G12** | `branches`/`sequential_steps` composed into a nested ternary (no first-wins collapse) | `bridge.py`, `alternation.py`, `sequential_composition.py` |
| **G13** | refinement chain accumulates across same-`run_id` passes (load-prefix + renumber) | `engine.py` |
| **G04** | banlist catches uppercase TLA+ keywords + bare `FORMAL_ONLY` (no false-positive on lowercase Verilog) | `compiler2.py` |
| **G05** | `MultiDriverError` raised when a var is driven by both comb + seq blocks | `compiler2.py` |
| **G09** | pass templates' rule names reconciled with registry ⇄ `_PASS_CONFIGS` | `pass2_handshake.py`, `pass3_datapath.py`, `stage3.py` |
| **G10** | `pass6_checker` became a real **direct Agent-3 critic gate** (5th call type, accept/reject→route); `pass5_mapping` wired as an engine pass; configs back to 5 passes | `agent3.py`, `stage3.py`, `pass6_checker.py` |
| _bonus_ | `sequential_composition.apply()` purity (was mutating its input `params`) | `sequential_composition.py` |
| _bonus_ | `_var` unused-binding lint nit | `compiler2.py` |

### Test coverage added (Wave 2) — ✅ DONE

| New / changed file | Tests | Covers |
|---|---|---|
| `tests/test_graph_routing.py` | 35 | **G06, G07** — routing tables, `_read_status` crash-shield, write-before-return, pass6 reject→halt |
| `tests/test_pass_templates.py` | 41 | **G09, G10** — rule-name ⇄ registry ⇄ configs consistency; 5 passes; critic wired |
| `tests/test_compiler2_correctness.py` | 22 | **G04, G05** + nested-ternary render |
| `tests/test_branch_collapse.py` | 10 (+1 xfail) | **G12** — branches survive into RTL + functional sim |
| `tests/test_refinement_backtrack.py` | 9 (+1 xfail) | **G08 det-half, G13** — backtrack machinery + chain reconstruction |
| `tests/test_reverse_bridge.py` | 8 (+1 xfail) | **G11** — multi-var multi-bit bridge |
| `tests/test_cocotb_roundtrip.py` | 7 | **G03** — converted to pytest; behavioral checks on **generated** RTL |
| `tests/test_cocotb_generator.py` | 4 (+6 xfail) | **G14** — generator value/clk fragilities |
| `tests/test_end_to_end_offline.py` | 4 (+3 xfail) | **G01, G02** — medium fixtures (traffic-light FSM, ALU) + full-graph offline |
| `tests/test_refinement_convergence.py` | _modified_ | `_step_index` module-global removed → applicability-driven picker |

### New discoveries (Wave 2) — ✅ FIXED in Wave 3 (2026-06-07)

| Bug | Fix | Where | Test (now positive) |
|---|---|---|---|
| **D1** | Compiler 2 emits `` `timescale 1ns / 1ps `` as the first line of every module, so iverilog can represent cocotb's 10 ns clock. | `compiler2.py` (`RTLTLACompiler.compile`) | `test_branch_collapse::test_compiler2_output_simulatable_without_prepended_timescale`, `test_end_to_end_offline::test_graph_stage4_cocotb_passes` |
| **D2** | Free inputs are sized via `_free_input_width`: (1) a Stage-1 SpecSummary port-width hint (threaded through stage3), else (2) inference from a register the input feeds directly (`data' = din` → `din` inherits `data`'s width), else 1. | `bridge.py`, `stage3.py` | `test_reverse_bridge::test_narrow_free_input_feeds_wide_register`, `test_end_to_end_offline::test_alu_freeinput_width_correct` |
| **D3** | Invalid picks counted by a per-depth **integer** counter (`invalid_counts`), not a set keyed on error text, so identical failures reach the 3-strike backtrack. | `engine.py` | `test_refinement_backtrack::test_identical_invalid_picks_should_backtrack_not_spin_to_max_steps` |
| **D4** | Re-picking an already-excluded choice now also counts as a strike, so a pure-of-spec picker backtracks instead of looping to `MAX_STEPS`. | `engine.py` | covered by the backtrack suite (no separate xfail) |
| **D5** | A clocked action's non-trivial guard is woven into the next-state via `_clocked_update_exprs` (`IF <not guard> THEN var ELSE <update>`, negated-guard/ELSE-chain form so Compiler 2 renders a clean nested ternary). | `bridge.py` | `test_dff::test_counter_enable_declared_as_input` (asserts the `(en != 1) ? count : ...` gating) |
| **companion: runner** | `run_testbench` absolutises `testbench_path`/`rtl_path` before changing cwd, so the graph's relative artifact paths no longer double up and the `.vvp` builds. This (not D1 alone) is what unblocked the graph's end-to-end cocotb PASS. | `cocotb/runner.py` | `test_end_to_end_offline::test_graph_stage4_cocotb_passes` |
| **companion: generator** | The generator initialises every test-vector input to 0 before the reset pulse, so undriven X cannot poison a self-feeding register at the reset-deassert edge (exposed by D5's enable gating). Shifts the en-gated counter's reset offset by one (`[2,3,0,1]` → `[1,2,3,0]`). | `cocotb/generator.py` | `test_cocotb_roundtrip::test_generated_counter_behaves_increments` |

### Still open / deferred

- **G14** (cocotb-generator value/clock fragilities) — the 6 remaining xfails: non-int/hex/bool/`'x'` values interpolated raw, non-`clk` clock-port name, empty-`test_vectors` vacuous pass. Deferred.
- **G15** (usage-ledger tests) and **G16** (deterministic diagnoser tests) — deferred "batch 2", not yet written.
- **G08 live half** — refinement-to-convergence with the *real* `pick_rule` (gated live test) still unwritten.

> Sections 2–7 below are the **original audit** (2026-06-06), preserved as the historical record of what was found before Wave 1/2.

---

## 2. Coverage Matrix

| Subsystem | Deterministic | Live | Level | Evidence |
|---|:---:|:---:|---|---|
| schemas / envelope | ✓ | ✗ | **strong** | `tests/test_envelope.py` (7 fns) — closed-set `status`, no-write-on-invalid (BUG-13), `write_error` full-dict equality |
| compiler2 (Verilog) | ✓ | ✗ | partial | `test_compilers.py` + `test_dff.py`; **strong** verilator/iverilog `rc==0`; **weak** split/truncation/multi-driver bugs untested |
| compiler1 (TLA+) | ✓ | ✗ | partial | `test_compilers.py` (11 fns incl. 4 error-path + determinism); substring-only; multi-invariant/UNCHANGED/lowercase-ops untested |
| refinement-engine | ✓ | ✗ | partial | `test_refinement_convergence.py` (one counter path + replay-hash) + `test_dff.py`; backtrack/stall/invalid-pick uncovered |
| refinement-rules | ✓ | ✗ | partial | `test_purity_of_all_rules` (6 Tier-1, double-call identity only); no input-mutation/correctness check; Tier-2 zero |
| agent1 | ✗ | ✓ | thin | `agentic_tests/test_agent1_live.py` (5 fns, gated); mostly shape checks; determinism smoke currently failing (`fcd45f5`) |
| agent3 / pick_rule | ✗ | ✓ | thin | `agentic_tests/test_agent3_live.py` (8 fns, gated); `revise_*` assert validity not *fix*; only key-missing guard is deterministic |
| diagnoser | ✗ | ✓ | thin | `agentic_tests/test_diagnoser_live.py` (3 fns, gated); accepts either label → cannot detect misclassification; build short-circuit unverified |
| cocotb-generator | ✗ | ✗ | thin | `test_cocotb_roundtrip.py` exercises it but has **0 pytest-collectable fns** (all under `__main__`) |
| cocotb-runner | ✗ | ✗ | partial | real good/mutant/invalid discriminator in `test_cocotb_roundtrip.py` — but **not collectable** under pytest |
| refinement-templates | ✗ | ✗ | **none** | no test references `refinement_templates`/`pass*`/`_PASS_CONFIGS` |
| orchestration / graph | ✗ | ✗ | **none** | no test imports `graph.py`, `_route_after_*`, `_read_status`, `build_graph` |
| usage-ledger | ✗ | ✗ | **none** | no test imports `usage`, `check_budget`, `log_usage`, `BudgetExceeded`, `_extract_tokens` |
| **end-to-end NL→Verilog** | ✗ | ✗ | **none** | no test invokes the compiled graph; no `03_rtl_output.json`/`04_evaluation.json`/`*.v` ever persisted |

---

## 3. Latent Bugs the Trivial Fixtures Are Hiding

These are not merely missing tests — they are product defects on the medium-complexity path, each re-verified against the source.

| # | Bug | Independent verification |
|---|---|---|
| **G12** | **Alternation & SequentialComposition collapse multi-branch assignments to the same variable (first-wins).** The rules stash `branches`/`sequential_steps` on the action, but `bridge.py` emits clocked logic **only** from the flat `updates` list. | ✅ **Confirmed** — `grep branches bridge.py` = 0, `sequential_steps` = 0; `alternation.py:42` `all_vars.setdefault(upd["variable"], upd)` then `action["updates"] = list(all_vars.values())`; `sequential_composition.py:49` `if upd["variable"] not in seen`. FSM/mux/ALU next-state *is* multi-branch assign → **lint-clean RTL with wrong next-state.** |
| **G09** | **Pass templates instruct the LLM to emit unregistered rule names.** `pass2_handshake` `rule_used: <StrengthenDuring \| PipingComposition>`; `pass3_datapath` `<DataRefinement \| …>`. | ✅ **Confirmed** — registry = `{Assignment, Alternation, Iteration, SequentialComposition, IntroduceVariable, Initialization}`; none of `StrengthenDuring/PipingComposition/DataRefinement` exist. `_PASS_CONFIGS` allows `{Alternation, IntroduceVariable}` (pass2) / `{Assignment, IntroduceVariable}` (pass3). The handshake & datapath passes — the ones that distinguish a FIFO/UART from a counter — **stall on every pick** (`_validate_pick` rejects). |
| **G10** | **`pass5_mapping` & `pass6_checker` are dead code** — the only mapping-completeness and refinement-correctness critics. | ✅ **Confirmed** — 0 imports anywhere; `stage3.py:86-89` wires only pass1–4; `_PASS_CONFIGS` contains only those four. No static gate rejects a bad refinement before codegen (cocotb is the sole backstop). |
| **G04** | **Compiler-2 banlist is blind to leaked TLA+ keywords** (`IF/THEN/ELSE/IN/CASE/LET`, bare `FORMAL_ONLY`). | ✅ **Confirmed** — `_BANLIST` (compiler2.py:143–203) contains only SystemVerilog/non-synth tokens (`logic`, `always_ff`, `always_comb`, `always_latch`, `interface`, `modport`, `typedef`, `initial`, `#delay`, `$systask`); no TLA+-operator entry. The last gate before disk won't catch a leaked conditional. |
| **G05** | **Compiler-2 can emit an illegal multi-driver** (`assign` + procedural `<=` on one variable). | ⚠️ **Partial** — confirmed `_classify` (compiler2.py:393–404) has **no conflict detection**: a var in both `seq_vars` and `comb_vars` silently returns `output_reg` (seq check wins). The double-emit consequence (both `parse_combinational` and `parse_sequential` emit) is the likely result but the full emit path was not traced end to end. |
| **G13** | **Multi-pass refinement chain overwritten per pass; backtrack replays from the abstract spec, losing pass1–4.** | ⚠️ **Partial** — confirmed `_save_chain` (engine.py:137–139) opens mode `"w"` (overwrite). The "run() never `_load_chain`s; same `run_id` across 4 passes ⇒ only last pass survives; backtrack replays from abstract spec" consequence is plausible but not fully traced. |

---

## 4. What's Solid (credit where due)

- **`tests/test_envelope.py`** — the one genuinely strong file. Verifies real contracts, not shapes: the `status` `Literal` closed-set accepts the three legal values and rejects a typo (`test_status_typo_raises`); the BUG-13 **no-write-on-invalid** invariant via `not p.exists()` (`test_write_artifact_rejects_typo_no_file`), proving validation precedes disk write; `write_error` checked with full-dict equality; `validate_status` identity pinned with `is`.
- **The verilator/iverilog lint gates** (`test_compiler2_sample_lint_clean`, `test_compiler2_counter_tla_lint_clean`, `test_bug17_width2_counter_lint_clean_no_widthtrunc`, `test_dff::test_dff_elaborates_clean_under_iverilog`) shell out and require `rc==0` / no `WIDTHTRUNC` — proving emitted Verilog is real elaboratable code, not well-shaped text.
- **BUG-17 / BUG-18 regression guards are real and direct** — width carried `bridge→compiler2` as `[1:0]` (`test_bug17_width_carried_to_verilog_range`); free inputs declared via update-expr (`test_dff_data_input_declared`) and via guard (`test_counter_enable_declared_as_input`); the no-spurious-`rst` negative check. All run through the real `engine_spec_to_rtl_tla` path.
- **The banlist negative tests** (`test_banlist_*` family) confirm the gate fires *and* names the offending token, with no false-positive on banned words inside comments.
- **The cocotb good/mutant/invalid discriminator** (`test_cocotb_roundtrip.py` sub-tests 2–4) is a genuine behavioral oracle: a real Icarus sim that PASSes the correct counter, FAILs a one-character `q-1` mutant (`phase=='test'`), and FAILs malformed Verilog (`phase=='build'`). Its only flaw is that it doesn't run under pytest (G03/G14).
- **The replay-hash determinism check** (`test_convergence_counter`) and **per-rule purity check** (`test_purity_of_all_rules`) pin the engine's load-bearing backtracking invariant for the scripted case.

---

## 5. Critical & High Gaps

### G01 — No end-to-end NL→Verilog correctness test at any tier *(critical)*
**Missing:** nothing runs `build_graph().invoke` / `main.py` from an NL prompt through to a cocotb PASS. On disk, `artifacts/` holds only `00_nl_spec.json`, `refinement_chain.json`, `usage_log.jsonl` — no `01_summary.json`, `02_formal_spec.json`, `03_rtl_output.json`, `04_evaluation.json`, or any `*.v` has *ever* been persisted. The closest tests stop short: `test_stage_nodes_live.py` halts before cocotb; `test_dff.py` only `iverilog -t null` elaborates a hand-built spec; `test_cocotb_roundtrip.py` sims a hardcoded counter string, not pipeline output.
**Why it blocks the goal:** the entire claim is "NL → perfect RTL verified by cocotb." Zero evidence the chained path works even once; any inter-stage status/handoff/codegen break ships undetected.
**Add:** a deterministic end-to-end test driving the full graph on ≥1 design with a recorded/stubbed offline `pick_rule`, asserting the artifact chain completes (`01/02/03/04` all `success`), `output.v` exists, lints clean, and cocotb PASSes. Pair with a gated live variant on a medium design.

### G03 — No functional (behavioral) RTL verification in the deterministic suite *(critical)*
**Missing:** every deterministic RTL check stops at lint/elaboration. `_run_linter` runs `verilator --lint-only` / `iverilog -Wall -t null` only; `test_dff.py` never runs `vvp`. The only behavioral sim (`test_cocotb_roundtrip.py`) is not pytest-collectable and only covers a hand-written counter, never Compiler-2-generated output.
**Why it blocks the goal:** lint-clean-but-functionally-wrong RTL is the precise silent failure the goal must catch.
**Add:** rename `_run_tests` sub-tests in `test_cocotb_roundtrip.py` to `test_*` with `importorskip`/`shutil.which` guards, then add cocotb behavioral checks on **Compiler-2-generated** Verilog (DFF: q follows d, resets to 0; counter increments; ≥1 medium design), asserting signal values over time.

### G12 — Alternation/SequentialComposition collapse multi-branch assignments *(critical)*
See §3. **Add:** an Alternation and a SequentialComposition spec where two branches assign the SAME variable different expressions (`state'=S1` vs `state'=S2`); assert both survive into emitted RTL (ternary/case or `branches`-aware emission), then verify functionally via cocotb.

### G06 — LangGraph routing & write-before-return invariant entirely untested *(critical)*
**Missing:** no test imports `graph.py`, `_route_after_stage1/3/4`, `_route_after_diagnose`, `_read_status`, or `build_graph`. Unverified: the `_read_status` crash-shield (must return `'error'`, never raise, on missing/malformed/status-less artifacts); the stage1 retry off-by-one; `_route_after_stage3` advancing `'partial'` as `'success'`; cocotb retry-loop termination; write-artifact-before-return on every failure path.
**Why it blocks the goal:** routing is the control plane (converge vs halt vs advance-wrong-artifact). A `_read_status` that raises crashes the run to zero RTL.
**Add:** deterministic tests — (1) `_read_status` returns `'error'` for missing/invalid-JSON/status-less files; (2) each node writes a status-bearing artifact on every early-return (monkeypatch agent/generator/runner to raise); (3) walk stage1 retry + diagnose→revise/backtrack→stage4 for bounded termination; (4) `_route_after_stage3` returns `'advance'` for `'partial'` and `'success'`, `'halt'` otherwise.

### G02 — Zero medium-complexity design fixtures anywhere *(high)*
**Missing:** exhaustive grep for `fifo|uart|arbiter|alu|spi|i2c|debounce|memory-controller|shift-reg|sequence-detector|round-robin|traffic|datapath` across both trees returns nothing. Every fixture is a 2-bit counter, a DFF, or a 1-bit toggle. `main.py`'s default is a counter.
**Why it blocks the goal:** a green suite proves only counter/DFF capability; it cannot catch a pipeline that silently fails on every design the goal targets.
**Add:** hand-built `FormalSpec` + NL-prompt fixtures for ≥3 medium designs (sync FIFO with full/empty flags; ≥4-state UART-tx FSM + shift register; multi-op ALU with flags), routed through `engine.run` → `engine_spec_to_rtl_tla` → Compiler-2 → lint + cocotb functional checks. **Unblocks G01, G03, G08, G11.**

### G07 — 'partial' RTL status advances into cocotb, untested *(high)*
**Missing/Bug:** `graph.py:106-110` `_route_after_stage3` treats `'partial'` identically to `'success'`. When refinement throws (`stage3.py:296-299`) it sets `status='partial'` on `02_formal_spec.json` only, while `rtl_tla_source` silently falls back to the **unrefined** `tla_source` (`stage3.py:259`), so `03_rtl_output.json` is written `status='success'`. Either way, incompletely-refined RTL advances to cocotb. No test exercises the partial path.
**Add:** write a `'partial'` `03_rtl_output.json`, assert `_route_after_stage3` returns `'advance'`; then an end-to-end test that under-refined RTL does NOT vacuously PASS cocotb against a meaningful testbench.

### G13 — Multi-pass chain overwritten; backtrack loses pass1–4 *(high)*
See §3. **Add:** simulate stage3's multi-pass pattern (repeated `run()`, same `run_id`, across `allowed_rule_names` subsets), then `_replay_chain(initial_abstract_spec, on_disk_chain)` and assert it reconstructs the FINAL multi-pass spec, not just the last pass.

### G08 — No live refinement-to-convergence with the REAL pick_rule *(high)*
**Missing:** `test_refinement_convergence` uses a hardcoded stub on a fixed 3-step counter path; `test_agent3_live` only checks single-shot `pick_rule` membership. Nothing drives a full multi-step refinement to `is_rtl_style` with the real LLM, and the backtracking machinery (`_backtrack`, `MAX_BACKTRACK_DEPTH=5`, `__invalid__` 3-strikes, `excluded_at`, `RefinementStall`) has **zero** coverage even deterministically.
**Add:** (1) a gated live test running `engine.run` with the real `pick_rule` on a medium spec, asserting `is_rtl_style` without `RefinementStall`; (2) a deterministic test feeding a deliberately-stalling stub, asserting backtrack fires and `RefinementStall` raises on exhaustion.

### G04 — Compiler-2 banlist blind to leaked TLA+ keywords *(high)*
See §3. **Add:** (a) feed corrupt Verilog with word-boundary `IF/THEN/ELSE/IN/CASE/LET` and a bare `FORMAL_ONLY` to `verify_banlist`, assert `BanlistViolation`; (b) compile a nested-IF spec whose ELSE lands alone on a continuation line, assert no leaked token survives; (c) compile a comb conjunct with a formal-only RHS, assert no empty `assign` is emitted.

### G05 — Compiler-2 emits illegal multi-driver, no conflict detection *(high)*
See §3. **Add:** an RTL-style TLA+ spec assigning the same variable in `CombinationalLogic` and `UpdatePipeline`; assert Compiler-2 raises a clear error or emits a single consistent driver, and verilator/iverilog report no `MULTIDRIVEN`/`BLKANDNBLK`.

### G09 — Pass-template allowed-rule names contradict `_PASS_CONFIGS` *(high)*
See §3. **Add:** a deterministic test asserting every rule name in each pass's `SYSTEM` ALLOWED-RULES section and `rule_used` enum is a member of `{r.__class__.__name__ for r in RULE_REGISTRY}` AND equals `stage3._PASS_CONFIGS[i]['allowed']`.

### G10 — pass5_mapping & pass6_checker are dead code *(high)*
See §3. **Add:** a wiring test asserting pass5/pass6 are referenced by stage3 (or an explicit documented skip); plus a coverage test that the checker pass actually rejects a known-bad refinement.

### G11 — Reverse bridge lacks a multi-var multi-bit test *(medium — refined down)*
**Refined:** BUG-17 and BUG-18 (both variants) ARE directly regressed through the real bridge path (§4). **Residual gap:** no single test feeds ≥2 variables that BOTH have width>1 together with a free input + reset + multiple clocked actions — every bridge test uses exactly ONE engine variable, so the multi-var width-comment/comma-emission loop and per-var width correctness on the bridge text are never asserted.
**Add:** feed a refined FSM+datapath engine-spec (≥2 vars width>1, a reset action, multiple clocked actions, a guard referencing a free input) to `engine_spec_to_rtl_tla`; assert per-var `\* width: N` comments, free input → input port, IF reset=1 THEN/ELSE block well-formed; round-trip through Compiler-2 + verilator, assert `WIDTHTRUNC`/`UNDRIVEN`-clean.

---

## 6. Medium & Low Gaps

- **G14 — cocotb generator value/clk fragility *(high)*.** `generator.py` hardcodes `Clock(dut.clk)`/`RisingEdge(dut.clk)` (clock port must literally be `clk`) and interpolates test-vector values raw via f-strings (lines 71, 82–84). Since `inputs`/`expected` are `dict[str, Any]`, a string `'x'`, a bool, or hex produces broken/wrong Python. No test feeds a non-int value, non-`clk` clock name, empty `test_vectors` (vacuous PASS), active-low reset, or a combinational/multi-cycle DUT. **Add** collectable generator tests (tool-skip guarded) for each shape.
- **G15 — usage.py budget guard untested *(high)*.** No test imports `usage`/`check_budget`/`log_usage`/`BudgetExceeded`/`_extract_tokens`/`reprice`. History (`fcd45f5`, "bad model string … fixed") confirms model-id→rate already broke once. A raising `log_usage` crashes a stage before its artifact is written, crashing the router. **Add** deterministic tests against a tmp JSONL ledger: boundary (just-under returns, exactly-at raises `BudgetExceeded`); `log_usage` never raises on malformed/None/unwritable; `_extract_tokens` for both SDK shapes and `usage=None`; model-id→rate including a date-stamped opus id and an unknown id falling back to the most-expensive Claude rate.
- **G16 — deterministic diagnoser build-path untested *(high)*.** No `tests/` file imports the diagnoser; `test_diagnoser_live.py` falsely claims the build path is covered deterministically. The `phase=='build' → 'spec'` short-circuit (the only non-LLM classification, primary routing key) and `run_diagnose`'s always-write-artifact + always-set-`last_diagnosis` contract are unverified; `phase=='unknown'` silently taking the LLM `'test'` path is untested. **Add** deterministic tests: `phase=='build'` returns `'spec'` with `_get_client` monkeypatched to raise; `run_diagnose` writes a valid envelope + sets `last_diagnosis` on both success and crash; `phase=='unknown'` pinned; out-of-vocab `failure_type` coerced to `'spec'`.
- **Compiler-1 translation correctness *(low-medium)*.** `test_compiler1_action_expressions_translated` asserts `/\` appears *anywhere* (vacuous); `test_compiler1_unchanged_clause` only checks `UNCHANGED <<>>` is absent; multi-invariant `Inv0/Inv1` naming branch (compiler1.py:279), Bit-type `{0,1}` range, and the `=→==` lookbehind regex are never asserted. **Add** golden-file comparisons or targeted operator-mapping assertions.
- **Tier-2 refinement rules *(low)*.** `ParallelComposition`, `ExpandFrame`, `ContractFrame`, `WeakenPrecondition`, `StrengthenPostcondition` have zero purity or apply coverage.
- **Rule purity is shallow *(low-medium)*.** `verify_rule_purity` checks only double-call output identity; it never asserts `apply()` leaves the input spec unmutated (the load-bearing facet for replay) nor that the output is the *intended* refinement.

**Cross-cutting brittleness to fix while touching these files:** lint tests use no `shutil.which`/`importorskip` guard, so they ERROR (not skip) without verilator/iverilog; `test_cocotb_roundtrip.py` and `test_refinement_convergence.py` use `__main__` harnesses / `sys.exit(1)` instead of pytest functions; many emitter assertions pin exact whitespace; module-global `_step_index` in `engine.py` violates the project's "no global mutable state" rule.

---

## 7. Recommended Test-Plan Additions (ordered punch-list)

Items **1–6 are the goal-blocking core** — ship them before claiming the pipeline can hit NL→medium-RTL→cocotb-verified.

1. **`tests/fixtures/medium_designs.py`** — hand-built `FormalSpec` + engine-spec + NL-prompt fixtures for a sync FIFO (full/empty), a ≥4-state UART-tx FSM + shift register, and a multi-op ALU with flags. *Unblocks G02; dependency for items 2, 3, 6, 9.*
2. **`tests/test_graph_routing.py`** — `_read_status` crash-shield; every node writes status-bearing artifact on every failure path (monkeypatched to raise); stage1 retry + diagnose→revise/backtrack→stage4 bounded termination; `_route_after_stage3` partial/success → `'advance'`, else `'halt'`. *G06, G07.*
3. **`tests/test_end_to_end_offline.py`** — full `build_graph().invoke` on a medium design with a recorded/stubbed offline `pick_rule`; assert `01/02/03/04` all `success`, `output.v` exists, lints clean, cocotb PASSes. *G01.*
4. **Convert `tests/test_cocotb_roundtrip.py` to pytest** — rename `_run_tests` sub-tests to `test_*` with `importorskip`/`shutil.which` guards; add behavioral cocotb checks on **Compiler-2-generated** RTL (DFF, counter, ≥1 medium). *G03.*
5. **`tests/test_compiler2_correctness.py`** — banlist leaked-TLA+/`FORMAL_ONLY` cases (G04); multi-driver same-variable conflict (G05); nested-IF/premature-ELSE split + empty-assign corruption.
6. **`tests/test_branch_collapse.py`** — Alternation + SequentialComposition specs with two branches assigning the SAME variable; assert both survive into emitted RTL, verify via cocotb. *G12.*
7. **`tests/test_pass_templates.py`** — every pass `SYSTEM`/`rule_used` rule name ∈ `RULE_REGISTRY` and = `_PASS_CONFIGS[i]['allowed']`; pass5/pass6 wired-in-or-documented-skip + checker rejects a known-bad refinement. *G09, G10.*
8. **`tests/test_refinement_backtrack.py`** — deliberately-stalling stub asserts backtrack fires, `MAX_BACKTRACK_DEPTH`/`__invalid__` 3-strike accounting, `RefinementStall` on exhaustion; multi-pass `run()` same-`run_id` chain reconstruction. *G08 (deterministic half), G13.*
9. **`tests/test_reverse_bridge.py`** — multi-var (≥2 width>1) + free input + reset + multiple clocked actions through `engine_spec_to_rtl_tla`; assert width comments, ports, reset block; lint WIDTHTRUNC/UNDRIVEN-clean. *G11.*
10. **`tests/test_cocotb_generator.py`** — non-int/hex/bool/`'x'` values, non-`clk` clock name, empty `test_vectors` (not a vacuous PASS), active-low reset polarity ordering, combinational/multi-cycle sampling. *G14.*
11. **`tests/test_usage_ledger.py`** — budget boundary, never-raise contract, `_extract_tokens` for both SDK shapes + None, model-id→rate table (date-stamped opus + unknown-id fallback). *G15.*
12. **`tests/test_diagnoser_deterministic.py`** — `phase=='build' → 'spec'` with client monkeypatched to raise; `run_diagnose` always-write + always-set `last_diagnosis` on success and crash; `phase=='unknown'`; out-of-vocab `failure_type` coerced to `'spec'`. *G16.*
13. **Gated live additions** (`agentic_tests/`) — real `pick_rule` refinement-to-convergence on a medium design (G08 live half); a medium-design Stage1/Stage3 node run that asserts Verilog-2001 conformance (reject `logic`/`always_ff`/`always_comb`/`initial`) and lints, not just substring `'module'`.
