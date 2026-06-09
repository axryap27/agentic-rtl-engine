# Status & Known Issues

A snapshot of what works today and what's open. For the full history of resolved bugs
and the test-comprehensiveness audit, see the git log — this file tracks the *current*
state, not the archive.

_Last updated: 2026-06-09._

---

## What's built

| Component | State |
|---|---|
| Stage 1 — prompt → `SpecSummary` (Agent 1) | built |
| Stage 2 — testbench generation (deterministic) | built |
| Stage 3 — spec authoring, refinement, codegen (Agent 3 + engine + Compiler 2) | built |
| Stage 4 — cocotb simulation (deterministic) | built |
| Diagnoser — failure classification + routing | built |
| Refinement engine + six Tier-1 rules | built; converges on counter, flip-flop, FSM, ALU, accumulator; robust to a throwing/cycling live picker |
| Compiler 1 / Compiler 2 + bridge | built; Verilog-2001, width-correct, banlist-enforced |
| LangGraph orchestration + status routing | built |
| Usage ledger + Agent-3 budget guard | built |
| Deterministic test suite | **293 passed, 0 xfailed** |

The deterministic spine is verified end to end. The full LangGraph now runs **NL → RTL →
cocotb PASS offline** on three medium designs — a traffic-light FSM, a multi-op ALU, and
an 8-bit accumulator — with every LLM boundary mocked, exercising the real engine, both
compilers, and the cocotb runner.

Live-confirmed design classes: the 2-bit counter (`885b9fc0a06b`), the traffic-light FSM
(`15d3dd354b17`), the multi-op ALU, and — after a three-run debugging arc — the 8-bit
accumulator, now a **clean, genuine live pass** (`121027-760bd3`, 2026-06-09). The
accumulator's first run (`223427`) was a **false green** and its second (`114009`) halted
safely on a refinement stall; both exposed real bugs, now fixed + regression-tested (see
the accumulator sections below). The runs also **confirmed RC1's active-low reset path
live** (`if (!rst_n)`), closing the gap the FSM run left open.

---

## Open issues

The first live `main.py` run (2-bit counter, run `64b59441443e`, 2026-06-08) is done.
It **confirmed the spec-authoring path is healthy** — the hardened Agent-3 prompt
produced a clean `FormalSpec` (symbolic comparisons, `clk`/`rst` not modelled as state
variables) — and **exposed two refinement-engine robustness gaps** the deterministic
stub picker had masked. Both are now fixed and regression-tested in
`tests/test_live_counter_repro.py`:

- A **throwing `pick_rule`** used to abort the whole run. The live picker (correctly)
  returned a non-pick "blocked" report on the irrelevant handshake pass; that raised
  out of the engine → `partial` empty module. The engine now treats a picker exception
  as a strike→backtrack, so a bad/declining response degrades to a *skipped pass*.
- **`Iteration` was non-idempotent** — it re-wrapped a guard in parens on every apply,
  so a re-picked action cycled the pass to its step cap. It is now a no-op on an
  already-clocked action, and the engine rejects no-op applications instead of
  committing-and-spinning.

With the captured clean spec, the **full Stage-3 path now converges to a correct,
lint-clean, cocotb-passing 2-bit counter offline** (`test_full_stage3_converges_on_captured_counter`,
`..._passes_cocotb`).

### Resolved: catch-all is now the sole refinement driver

The five structured-pass prompts (`pipeline/refinement_templates/passN_*.py`) instructed
Agent 3 to emit a verbose pass-report object (`status`/`artifact`/`diagnostics`),
**incompatible with `pick_rule`'s `{rule_name, params}` contract** — and they assumed
every design needs every phase (a counter has no handshake/datapath/mapping phase). On
the live 2-bit counter this wasted ~62% of the LLM budget (16 of 26 `pick_rule` calls
were junk) AND produced a **non-replayable** `refinement_chain.json`: passes 3 and 5 both
committed an `IntroduceVariable` named `count_concrete` (the per-pass uniqueness check
sees only the live in-memory spec, not the cross-pass committed prefix the engine
concatenates on disk), so replaying the full chain from scratch raised
`"IntroduceVariable: variable 'count_concrete' already exists"`.

**Resolution (implemented 2026-06-08):** the structured-pass loop is gated off
(`stage3._RUN_STRUCTURED_PASSES = False`) and the catch-all (base prompt, all rules) is
the **sole** refinement driver. A single `engine.run()` makes the on-disk chain
self-contained and replayable, and a duplicate `IntroduceVariable` name can never be
committed within one run. `_PASS_CONFIGS` and the pass-template files are **retained**
(pinned by `tests/test_pass_templates.py`, kept for future re-enablement), so that suite
stays green untouched. `_CATCHALL_MAX_STEPS` was raised 12 → 16 for sole-driver headroom;
idempotency + the no-op / 3-strike→backtrack guards make a larger cap cycle-free.
Regression: `tests/test_catchall_sole_driver.py` pins that the catch-all-only chain
replays cleanly with no duplicate `IntroduceVariable` names. **Confirmed live:** run
`885b9fc0a06b` refined the counter in exactly 3 clean steps (Initialization → Iteration →
Iteration), the persisted chain replays cleanly, and cocotb PASSED — 3 `pick_rule` calls
vs the old run's 26.

The gated live-refinement-convergence test (`agentic_tests/test_refinement_convergence_live.py`)
and the deterministic usage-ledger / diagnoser coverage (`tests/test_usage_ledger.py`,
`tests/test_diagnoser_deterministic.py`) are all written.

### Deferred polish & future scope

- **Sized-counter wrap idiom.** The `count <= (count + 1) % 4` form is functionally
  correct and lints clean under iverilog, but verilator still emits a *cosmetic*
  `WIDTHTRUNC` on the `% 2^k`. Emitting an explicit wrap (`IF count = MAX THEN 0 ELSE
  count + 1`) is fully clean on both linters; the fixtures already use the explicit
  form. A refinement/`pick_rule` policy preference, not a correctness bug.
- **Tier-2 refinement rules.** `ParallelComposition`, `ExpandFrame`, `ContractFrame`,
  `WeakenPrecondition`, `StrengthenPostcondition` are designed but not implemented (see
  [background.md](background.md)) — needed for designs beyond FSM+datapath.

### Live full-pipeline run — CONFIRMED GREEN

The full pipeline (NL → Agent 1 → Agent 3 → refinement → Compiler 2 → cocotb) reaches
**cocotb PASS end to end on a real LLM** — first on the old 5-pass path (run
`3f7e08d09b4b`), then, after the sole-driver change, on the catch-all-only path (run
`885b9fc0a06b`) in **3 clean, replayable `pick_rule` calls**. The bounded-action-space
thesis is demonstrated against a live model. The per-pick decision log
(`artifacts/<run_id>/refinement_decisions.jsonl`) records the full live trajectory.

Live runs are metered on the Agent-3 Anthropic key ([budget cap](agents.md#budget-guard))
and need the two credential sets in [running.md](running.md#credentials).

### FSM breadth — live-confirmed green (run `15d3dd354b17`, 2026-06-08)

The traffic-light FSM now passes **NL → Verilog → cocotb PASS live**, the second design
class after the counter. The first FSM run (`9a77ce279bfb`) died `partial` and exposed
three independent, previously-masked bugs — all fixed deterministically, regression-tested
in `tests/test_fsm_reset_clock_repro.py`, and confirmed on the re-run:

- **RC1 — active-low reset polarity dropped in codegen.** `SpecSummary.reset_active_low`
  was read at Stage 1 but never threaded into the reverse bridge or Compiler 2, which
  hardcoded active-high (`IF reset = 1` / `if (reset)`). Now threaded (default active-high)
  so an active-low `rst_n` emits `if (!rst_n)`. *Proven offline + unit-tested; the live
  re-run happened to choose active-high `rst`, so RC1's active-low branch is not yet
  live-exercised.*
- **RC2 — Agent 1 modelled `clk` as a toggling vector input.** The cocotb generator owns
  the clock and ticks once per vector, so toggled-`clk` vectors assumed half-rate
  advancement the harness never produces. Agent 1's prompt now carries a one-tick-per-vector
  clock contract; the generator no longer drives `clk` per-vector. *Confirmed live: the
  re-run emitted clean `clk:1`-constant immediate-advance vectors.*
- **RC3 — the refinement critic false-rejected the `Initialization` reset action.** A
  synchronous reset forcing state to its declared init values is a sanctioned refinement;
  `pass6_checker` now carves it out while keeping every genuine check. *Confirmed live: the
  critic ACCEPTED and the run reached `success`.*

The re-run refined in **4 clean, replayable steps** (`Initialization` + `Iteration×3`,
contiguous hashes, zero junk `IntroduceVariable`s) — the bounded-action-space thesis holds
on a third NL prompt.

Remaining live scope: all four proven classes have now run live — counter, FSM, ALU, and
accumulator. RC1's active-low path is **confirmed live** (the accumulator's `rst_n`).

### Accumulator — live run exposed a false green (now fixed) (run `223427-d7a921`, 2026-06-09)

The 8-bit accumulator (active-low `rst_n`, enable-gated `acc <= acc + din`) was the 4th
live design. It reached cocotb PASS — but as a **false green**: Agent 3 modelled the data
input `din` and the enable `en` as STATE VARIABLES, so the reverse bridge emitted them as
`output reg`. The design could never receive `din`, yet cocotb — which force-drives the
mis-declared output nets — passed anyway. Agent 1's port directions were correct; the
fault was Agent 3's spec authoring. Three fixes, all offline-proven, verified by an
adversarial multi-agent review (parser-robustness + replay-contract lenses) plus 8
regression tests in `tests/test_input_modeling_regression.py`:

- **RC4 — deterministic port-direction gate.** After Compiler 2, the emitted module's port
  directions are checked against the SpecSummary; a summary `input` emitted as `output`
  downgrades the RTL artifact to `partial` (→ halt) with `port_direction_errors`, instead
  of shipping a structurally-wrong interface. Fails LOUD if it cannot parse the header
  (never silently certifies). `pipeline/nodes/stage3.py`.
- **RC5 — Agent 3 prompt (root cause).** Now forbids modelling data/control inputs as
  `variables`, with the `x -> x`-in-every-action litmus the bug exhibited.
  `pipeline/agents/agent3.py`. **Confirmed live** by run #2 below.
- **RC6 — revise-replay.** See "Resolved — refinement-chain replay" below; it fired on
  this run.

### Accumulator run #2 — RC5/RC4 confirmed live; identity-hold stall + `mod` fixed (run `114009-883d7e`, 2026-06-09)

The same accumulator prompt re-run live did **not** false-green: **RC5 held** (Agent 3
modelled only `acc` as a variable — `din`/`en` correctly free inputs) and **RC4 held** (it
caught a degenerate module and halted at `partial` — no false green). The run instead
exposed two NEW deterministic defects, diagnosed via an ultracode workflow (3 investigators
+ an adjudicator that corrected all three on the necessity of the *pair* fix) and fixed
offline (suite 289 → 293, `tests/test_identity_hold_and_mod_regression.py`):

- **RC7 — refinement stall on a pure register-hold (the trigger).** Agent 3 authored a
  dedicated `Hold` action (`acc' = acc`). `is_rtl_style` required *every* non-reset action
  to be clocked, but the live picker never iterated `Hold`, so the engine backtracked to
  empty → stalled → fell back to abstract Compiler-1 TLA+ → Compiler 2 degenerated `acc`
  into a bare `input` with an empty body. Fixed as a verified **pair** (each alone fails):
  (1) `engine.is_rtl_style` skips identity-only holds (no longer requires them clocked);
  (2) `bridge.engine_spec_to_rtl_tla` drops identity-only actions from CombinationalLogic
  (else the un-iterated hold double-drives the register → `MultiDriverError`). Convergence
  no longer depends on the picker iterating a redundant Hold.
- **RC8 — `mod` word operator (latent, masked by the stall).** Agent 3 wrote
  `(acc + din) mod 256`; `mod` is not valid TLA+ and was not translated, leaking a phantom
  `input mod` port and invalid Verilog. Fixed by folding `mod` → `%` at the same
  word-boundary as AND/OR/NOT (`bridge._translate_bool_words` + a defensive copy in
  `compiler2._translate_basic`), plus an Agent 3 prompt nudge to use `%`.

With RC7+RC8 the captured run-#2 spec compiles to a correct, iverilog-clean accumulator
offline (`acc` as `output reg`, `din`/`en` inputs, `(acc + din) % 256`, enable-gated hold).

### Accumulator run #3 — CLEAN live pass; RC5/RC7 confirmed live (run `121027-760bd3`, 2026-06-09)

The same prompt, re-run live against the committed RC7+RC8 fixes, reaches **NL → cocotb
PASS end to end** — a genuine success, neither a false green nor a safe halt:

- **RC7 confirmed live.** Agent 3 again authored a dedicated `Hold` action (`acc' = acc`);
  only `Accumulate` was clocked, yet the engine converged in **2 clean replayable steps**
  (`Initialization` + `Iteration`, just 2 pick attempts, no cycling — contrast run #2's 11)
  with no stall and no `MultiDriverError`. The identity-hold relaxation took the Rule Picker
  off the convergence-critical path.
- **RC5 held** (`variables: ['acc']` only); **RC8 held** (Agent 3 wrote `% 256` — the prompt
  nudge; the deterministic translation stayed the unused backstop); **RC4 passed**
  (`03_rtl_output: success`, not `partial` → interface verified, not a false green); **RC1**
  emitted `if (!rst_n)`.
- cocotb passed **13 real `acc` assertions** (`0, 10, 30, 60, 60(hold)…`); the emitted module
  is a correct 8-bit accumulator (`acc` an `output reg [7:0]`, `din`/`en` inputs).

The accumulator is now a clean, genuine live design class — the 4th (counter, FSM, ALU,
accumulator). The full RC1–RC8 arc is validated against a live model.

### Resolved — refinement-chain replay on the cocotb-revise path

The cocotb-revise path used to append a fresh chain onto the stale prefix (instead of
truncating like backtrack), yielding a non-replayable `refinement_chain.json`. This
**fired live** on the accumulator run (`223427-d7a921`): a syntax-error revise re-entry
left a hash discontinuity at the prefix→suffix seam. **Fixed** (2026-06-09): the revise
path now clears `refinement_chain.json` (preserving it as
`refinement_chain_pre_revise_<n>.json`, suffixed per attempt) before re-running, so the
re-authored spec gets a self-contained, replayable chain. Backtrack's golden replay is
untouched — it truncates a PARTIAL prefix on an UNCHANGED spec, whereas a revise discards
the WHOLE prefix because the spec changed. Regression: `tests/test_input_modeling_regression.py`.

---

## How issues are tracked

This file lists *current* open items only. Resolved work — the BUG-* fix sweep, the
G01–G16 test-comprehensiveness audit, and the D1–D5 medium-design fixes — lives in the
git history (`git log`) and in the tests that pin each fix. When an item here is
resolved, remove it and let the test that guards it stand as the record.
