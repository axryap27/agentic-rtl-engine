# Team TODO

> **Update this file** when your focus changes so the rest of the team can see who owns what.

---

## What People are currently working on

| Person | Area |
|--------|------|
| **Satviki** | Compiler 1 (JSON(TLA) → TLA+) and Compiler 2 (RTL-style TLA+ → SystemVerilog) |
| **Terry** | cocotb harness, Agent 2 (JSON(S) → Python testbench) |
| **Mike** | Agent 1 & 3 — JSON(S) and JSON(TLA) schemas and generation |
| **Aarya** | LangGraph pipeline skeleton (see `pipeline/graph.py`, [PIPELINE.md](../PIPELINE.md)) |

---

## Open tasks

- [ ] **Harness scope** — Decide what each agent can read/write (which repo paths, artifact dirs, logs).
- [ ] **LangGraph integration** — Confirm how this team’s agents plug into the existing graph in `pipeline/` (Aarya’s work vs. new nodes).

---

## JSON schema work (Mike)

- [ ] Lock **JSON(S)** format: NL interpretation, test inputs, expected outputs.
- [ ] Lock **JSON(TLA)** format: states, transitions, invariants (Compiler 1 input).
- [ ] Document both schemas in repo (align with or extend [PIPELINE.md](../PIPELINE.md) artifacts where possible).

---

## Compilers (Satviki)

- [ ] Compiler 1: JSON(TLA) → valid TLA+ for TLC.
- [ ] Compiler 2: RTL-style TLA+ → SystemVerilog (template-driven).

---

## cocotb (Terry)

- [ ] Agent 2: JSON(S) → runnable cocotb testbench.
- [ ] Wire testbench to generated RTL module; define pass/fail reporting for Agent 3 feedback loop.

---

## Done / in progress elsewhere

| Item | Status | Where |
|------|--------|-------|
| LangGraph 4-stage skeleton | Implemented (stubs + partial real logic) | `pipeline/`, [README.md](../README.md) |
| Stage artifact contracts | Documented | [PIPELINE.md](../PIPELINE.md) |

