# Agentic RTL Engine — Architecture

**Input:** Natural language hardware description  
**Output:** Synthesizable SystemVerilog RTL, verified against a cocotb testbench

---

## Overview

The pipeline turns a user prompt into RTL through two strictly formatted JSON artifacts. One branch builds and checks formal models (TLA+); the other builds executable tests (cocotb). RTL is only accepted when it passes the testbench; failures feed back into the formal branch.

**Orchestration:** [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) coordinates agents, compilers, and retry loops.

For the four-stage artifact contract used in `pipeline/` today, see [PIPELINE.md](../PIPELINE.md). This document describes the target end-to-end design the team is building toward.

---

## High-level flow

```
Natural language prompt
        │
        ▼
   Agent 1 ──► JSON(S)          ← summary + golden test vectors
        │
        ├──────────────────────────────┐
        │                              │
        ▼                              ▼
   Agent 3 ──► JSON(TLA)          Agent 2 ──► cocotb testbench (.py)
        │                              │
        ▼                              │
 Compiler 1 ──► TLA+ ──► TLC          │
        │ (retry on TLC errors)        │
        ▼                              │
 RTL-style TLA+ (templates)            │
        │                              │
        ▼                              │
 Compiler 2 ──► SystemVerilog RTL ─────┘
        │
        ▼
   Run cocotb against RTL
        │
   pass ──► done
   fail ──► Agent 3 revises JSON(TLA); re-run formal branch (and downstream)
```

---

## Artifacts

| Artifact | Role | Requirements |
|----------|------|----------------|
| **JSON(S)** | Canonical interpretation of the user spec | Problem statement, test inputs, expected outputs; strict schema |
| **JSON(TLA)** | Formal design plan before TLA+ is emitted | States, transitions, invariants; strict schema |
| **TLA+** | Model checked by TLC | Produced by Compiler 1 from JSON(TLA) |
| **RTL-style TLA+** | Bridge to hardware | From verified TLA+; team-provided templates |
| **SystemVerilog** | Final RTL | Produced by Compiler 2 from RTL-style TLA+ |
| **cocotb script** | Simulation testbench | Generated from JSON(S); drives RTL directly |

---

## Branch 1 — Formal path (TLA+ → RTL)

1. **Agent 3** builds or updates **JSON(TLA)** from **JSON(S)** (and from cocotb/RTL debug logs on failure).
2. **Compiler 1** turns JSON(TLA) into **TLA+**.
3. **TLC** model-checks TLA+. On failure, errors are fed back into JSON(TLA) and the compile → TLC loop repeats.
4. After TLC passes, the spec is rewritten as **RTL-style TLA+** (template-guided).
5. **Compiler 2** emits **SystemVerilog** from RTL-style TLA+.

---

## Branch 2 — Verification path (cocotb)

1. **Agent 2** generates a **cocotb** Python testbench from **JSON(S)**.
2. The testbench is applied to the generated RTL module.
3. **Pass** → pipeline returns RTL + artifacts.
4. **Fail** → trust the testbench (see assumptions below); **Agent 3** adjusts JSON(TLA) and the formal branch runs again.

---

## Design assumptions

| Assumption | Rationale |
|------------|-----------|
| **JSON(S) is correct for one prompt round** | We cannot know if we misread the user without follow-up questions |
| **Testbench failures imply formal/RTL issues, not bad tests** | cocotb generation from JSON(S) is a short, reliable path vs. the full TLA+ → RTL chain |
| **User re-check of expected I/O** | Optional stopgap if we doubt JSON(S) test vectors (backup only) |

---

## Components

| Component | Responsibility |
|-----------|----------------|
| **Agent 1** | NL prompt → JSON(S) |
| **Agent 2** | JSON(S) → cocotb testbench + harness setup |
| **Agent 3** | JSON(S) → JSON(TLA); revise JSON(TLA) from TLC or testbench failures |
| **Compiler 1** | JSON(TLA) → TLA+ |
| **Compiler 2** | RTL-style TLA+ → SystemVerilog |
| **Pipeline coordination** | LangGraph graph, routing, retries, artifact paths |

---

## Retry behavior (summary)

| Failure point | Action |
|---------------|--------|
| TLC rejects TLA+ | Fix JSON(TLA) → Compiler 1 → TLC again |
| cocotb fails on RTL | Fix JSON(TLA) → re-run formal branch through RTL regen → re-test |

---

## Related documentation

| Document | Contents |
|----------|----------|
| [PIPELINE.md](../PIPELINE.md) | LangGraph stages, JSON schemas, milestones |
| [README.md](../README.md) | Setup, run instructions, current implementation status |
| [docs.md](./docs.md) | Formal-methods background and refinement concepts |
