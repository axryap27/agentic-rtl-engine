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
| Deterministic test suite | **204 passed, 6 xfailed** |

The deterministic spine is verified end to end. The full LangGraph now runs **NL → RTL →
cocotb PASS offline** on two medium designs — a traffic-light FSM and a multi-op ALU —
with every LLM boundary mocked, exercising the real engine, both compilers, and the
cocotb runner.

---

## Open issues

The six remaining `xfail`s are all **cocotb-generator fragilities (G14)** — known,
captured, not yet fixed. The generator interpolates test-vector values raw, so it
mishandles non-integer values and a few edge shapes:

- string values (`'x'`, `'1z'`, `'0xff'`) are emitted unquoted → `NameError` /
  `SyntaxError` / silent int coercion at sim time;
- boolean values are not normalized to `1`/`0`;
- the clock port is hardcoded to `dut.clk` (a summary whose clock is named otherwise is
  never clocked);
- empty `test_vectors` produces a vacuous PASS rather than a refusal.

*Fix direction:* `repr()`/quote non-int values; derive the clock-port name from the
`SpecSummary`; refuse (or fail) on empty vectors.

### Deferred test coverage

- **Usage-ledger tests** — budget boundary, never-raise contract, token extraction,
  model→rate table.
- **Deterministic diagnoser tests** — the `phase=="build" → "spec"` short-circuit and
  the always-write / always-set-`last_diagnosis` contract.
- **Live refinement convergence** — driving the engine to RTL-style with the *real*
  `pick_rule` (a gated `agentic_tests/` test), distinct from the deterministic stub
  path already covered.

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
