# Status & Known Issues

A snapshot of what works today and what's open. For the full history of resolved bugs
and the test-comprehensiveness audit, see the git log — this file tracks the *current*
state, not the archive.

_Last updated: 2026-06-08._

---

## What's built

| Component | State |
|---|---|
| Stage 1 — prompt → `SpecSummary` (Agent 1) | built |
| Stage 2 — testbench generation (deterministic) | built |
| Stage 3 — spec authoring, refinement, codegen (Agent 3 + engine + Compiler 2) | built |
| Stage 4 — cocotb simulation (deterministic) | built |
| Diagnoser — failure classification + routing | built |
| Refinement engine + six Tier-1 rules | built; converges on counter, flip-flop, FSM, ALU; robust to a throwing/cycling live picker |
| Compiler 1 / Compiler 2 + bridge | built; Verilog-2001, width-correct, banlist-enforced |
| LangGraph orchestration + status routing | built |
| Usage ledger + Agent-3 budget guard | built |
| Deterministic test suite | **257 passed, 0 xfailed** |

The deterministic spine is verified end to end. The full LangGraph now runs **NL → RTL →
cocotb PASS offline** on two medium designs — a traffic-light FSM and a multi-op ALU —
with every LLM boundary mocked, exercising the real engine, both compilers, and the
cocotb runner.

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

With the captured clean spec, the **full Stage-3 path (five structured passes +
catch-all) now converges to a correct, lint-clean, cocotb-passing 2-bit counter
offline** (`test_full_stage3_converges_on_captured_counter`, `..._passes_cocotb`).

### Known: structured-pass prompt/contract mismatch

The five structured-pass prompts (`pipeline/refinement_templates/passN_*.py`) instruct
Agent 3 to emit a verbose pass-report object (`status`/`artifact`/`diagnostics`), which
is **incompatible with `pick_rule`'s `{rule_name, params}` contract** — and they assume
every design needs every phase (a counter has no handshake/datapath/mapping phase).
Today this is *tolerated*: the engine robustness above skips passes where Agent 3
declines, and the catch-all (base prompt, all rules) carries convergence. The clean fix
— reconcile the templates to the pick contract, or make the catch-all the sole driver —
is a **cost optimization** (it would cut the wasted per-pass Agent-3 calls) and an
architecture decision, not a correctness blocker. `tests/test_pass_templates.py` pins
the 5-pass shape, so that change must update it too.

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

### Live full-pipeline run — one confirming run pending

The mechanical path **and** Stage-3 convergence are now proven offline on the *real*
captured Agent-3 spec. The one remaining live unknown is narrow: that the live picker,
in the catch-all pass, drives a from-scratch clean spec all the way to `is_rtl_style`
end to end (the convergence test already shows the live picker makes good
Init/Assignment/Iteration choices). A single confirming `python3.11 main.py` is the
final check — it needs the two credential sets in
[running.md](running.md#credentials), is metered on the Agent-3 Anthropic key
([budget cap](agents.md#budget-guard)), and is now far cheaper: the step caps plus the
engine robustness bound a run to a handful of `pick_rule` calls. The per-pick decision
log (`artifacts/<run_id>/refinement_decisions.jsonl`) records the full live trajectory
for triage without a re-run.

---

## How issues are tracked

This file lists *current* open items only. Resolved work — the BUG-* fix sweep, the
G01–G16 test-comprehensiveness audit, and the D1–D5 medium-design fixes — lives in the
git history (`git log`) and in the tests that pin each fix. When an item here is
resolved, remove it and let the test that guards it stand as the record.
