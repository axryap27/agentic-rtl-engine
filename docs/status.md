# Status & Known Issues

A snapshot of what works today and what's open. For the full history of resolved bugs
and the test-comprehensiveness audit, see the git log — this file tracks the *current*
state, not the archive.

_Last updated: 2026-06-07._

---

## What's built

| Component | State |
|---|---|
| Stage 1 — prompt → `SpecSummary` (Agent 1) | built |
| Stage 2 — testbench generation (deterministic) | built |
| Stage 3 — spec authoring, refinement, codegen (Agent 3 + engine + Compiler 2) | built |
| Stage 4 — cocotb simulation (deterministic) | built |
| Diagnoser — failure classification + routing | built |
| Refinement engine + six Tier-1 rules | built; converges on counter, flip-flop, FSM, ALU |
| Compiler 1 / Compiler 2 + bridge | built; Verilog-2001, width-correct, banlist-enforced |
| LangGraph orchestration + status routing | built |
| Usage ledger + Agent-3 budget guard | built |
| Deterministic test suite | **246 passed, 0 xfailed** |

The deterministic spine is verified end to end. The full LangGraph now runs **NL → RTL →
cocotb PASS offline** on two medium designs — a traffic-light FSM and a multi-op ALU —
with every LLM boundary mocked, exercising the real engine, both compilers, and the
cocotb runner.

---

## Open issues

The cocotb-generator fragilities (G14) that were the suite's only remaining `xfail`s are
**fixed** — the deterministic suite is fully green. Values are quoted/normalized via the
generator's `_fmt_value` (strings through `json.dumps`, bools → `1`/`0`), the clock port
is derived from the `SpecSummary` (defaulting to `clk`), and an empty `test_vectors`
emits a failing assert instead of a vacuous pass. The remaining open items below are
deferred test coverage and the first live run — neither is a code defect.

### Deferred test coverage

- **Live refinement convergence** — driving the engine to RTL-style with the *real*
  `pick_rule` (a gated `agentic_tests/` test), distinct from the deterministic stub
  path already covered.

  The deterministic usage-ledger and diagnoser coverage previously listed here is done
  — see `tests/test_usage_ledger.py` (budget boundary, never-raise, token extraction,
  model→rate table) and `tests/test_diagnoser_deterministic.py` (the `phase=="build" →
  "spec"` short-circuit and the always-write / always-set-`last_diagnosis` contract).

### Deferred polish & future scope

- **Sized-counter wrap idiom.** The `count <= (count + 1) % 4` form is functionally
  correct and lints clean under iverilog, but verilator still emits a *cosmetic*
  `WIDTHTRUNC` on the `% 2^k`. Emitting an explicit wrap (`IF count = MAX THEN 0 ELSE
  count + 1`) is fully clean on both linters; the fixtures already use the explicit
  form. A refinement/`pick_rule` policy preference, not a correctness bug.
- **Tier-2 refinement rules.** `ParallelComposition`, `ExpandFrame`, `ContractFrame`,
  `WeakenPrecondition`, `StrengthenPostcondition` are designed but not implemented (see
  [background.md](background.md)) — needed for designs beyond FSM+datapath.

### Live full-pipeline run

The deterministic suite proves the mechanical path; a **live** end-to-end run (real
Agent 1 / Agent 3 / Diagnoser) requires the two credential sets in
[running.md](running.md#credentials). The Agent-3 Anthropic key is metered and guarded
by [the budget cap](agents.md#budget-guard). This is the next milestone where the
bounded-action-space thesis meets a real LLM driving refinement.

---

## How issues are tracked

This file lists *current* open items only. Resolved work — the BUG-* fix sweep, the
G01–G16 test-comprehensiveness audit, and the D1–D5 medium-design fixes — lives in the
git history (`git log`) and in the tests that pin each fix. When an item here is
resolved, remove it and let the test that guards it stand as the record.
