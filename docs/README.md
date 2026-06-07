# Documentation

Design and reference documentation for the **Agentic RTL Engine** — a pipeline that
turns a natural-language description of a digital circuit into synthesizable
Verilog-2001, formally lowered through TLA+ and verified with cocotb.

For a one-page project introduction and setup, start with the
[root README](../README.md). For the contributor contract (artifact map, code
style, retry protocol), see [`CLAUDE.md`](../CLAUDE.md).

---

## Start here

| If you want to… | Read |
|---|---|
| Understand the whole system end to end | [architecture.md](architecture.md) |
| Run the pipeline | [running.md](running.md) |
| Know what works today and what's open | [status.md](status.md) |

## Reference by subsystem

| Document | Covers |
|---|---|
| [architecture.md](architecture.md) | Pipeline stages, the artifact chain, the LangGraph control plane, the runtime-agent / deterministic split, and the four design invariants. |
| [agents.md](agents.md) | The three runtime LLM agents — Agent 1, Agent 3 (five call types, tool use, budget guard), and the Diagnoser — plus the two LLM transports. |
| [refinement.md](refinement.md) | The refinement engine, the six Tier-1 rules, the multi-pass template schedule, the correctness critic, backtracking, and the replayable refinement chain. |
| [compilers.md](compilers.md) | Compiler 1 (FormalSpec → TLA+), Compiler 2 (RTL-style TLA+ → Verilog-2001), the bridge between them, port/width inference, and the Verilog-2001 banlist. |
| [verification.md](verification.md) | The deterministic cocotb testbench generator and runner, and the test suite (deterministic vs. live). |
| [background.md](background.md) | Why formal methods, the stepwise-refinement idea, the refinement-calculus rule tables, and references. |

## Conventions

- Code is the source of truth. These docs describe what the pipeline **does today**;
  where a doc and the code disagree, the code wins — please fix the doc.
- "Agent" always means a **runtime** LLM call inside the pipeline (Agent 1, Agent 3,
  Diagnoser). Everything else under `pipeline/` is deterministic Python.
- Verilog output is **Verilog-2001 only** (see the banlist in [compilers.md](compilers.md)).
