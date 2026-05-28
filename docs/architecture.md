# Agentic RTL Engine — Architecture

**Input:** Natural language hardware description  
**Output:** Synthesizable SystemVerilog RTL, verified against a cocotb testbench

---

## Overview

The pipeline turns a user prompt into RTL through two strictly formatted JSON artifacts. One branch builds and checks formal models (TLA+); the other builds executable tests (cocotb). RTL is only accepted when it passes the testbench; failures feed back into the formal branch.

The formal branch uses **stepwise refinement** to bridge the gap between verified abstract TLA+ and RTL-style TLA+. The refinement step is driven by a finite library of **refinement calculus rules** (see [TLA_specs.png](./TLA_specs.png)). At each refinement step the LLM picks one rule from the applicable set; the engine applies it mechanically. This bounded action space is the project's primary defense against LLM hallucination — refinement correctness is guaranteed by construction because every rule in the library is provably refinement-preserving.

**Orchestration:** [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) coordinates agents, compilers, and retry loops.

For the four-stage artifact contract in `pipeline/` today, see [PIPELINE.md](../PIPELINE.md). For research framing, see [docs.md](./docs.md). This document is the implementation target for the 2-week / 5-person build.

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
 Compiler 1 ──► TLA+ ──► TLC           │
        │ (retry on TLC errors)        │
        ▼                              │
 Refinement Engine                     │
   ┌──────────────────────────────┐    │
   │ while not is_rtl_style:      │    │
   │   applicable = rules.filter()│    │  ← bounded LLM action space:
   │   rule,params = LLM picks    │    │    pick from Tier-1 calculus rules
   │   spec = rule.apply(...)     │    │
   └──────────────────────────────┘    │
        │                              │
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
| **refinement_chain.json** | Ordered list of `(rule_name, params)` applied during refinement — serves as the proof trace | Produced by Refinement Engine |
| **RTL-style TLA+** | Bridge to hardware | Result of applying refinement calculus rules to the verified TLA+ |
| **SystemVerilog** | Final RTL | Produced by Compiler 2 from RTL-style TLA+ |
| **cocotb script** | Simulation testbench | Generated from JSON(S); drives RTL directly |

---

## Branch 1 — Formal path (TLA+ → RTL)

1. **Agent 3** builds or updates **JSON(TLA)** from **JSON(S)** (and from cocotb/RTL debug logs on failure).
2. **Compiler 1** turns JSON(TLA) into **TLA+**.
3. **TLC** model-checks TLA+. On failure, errors are fed back into JSON(TLA) and the compile → TLC loop repeats.
4. **Refinement Engine** runs a rule-application loop until the spec is RTL-style:
   - The engine filters the rule library to those applicable to the current spec
   - The LLM picks one rule and its parameters from the applicable set
   - The engine applies the rule, producing a new (more concrete) spec
   - The choice is appended to `refinement_chain.json`
5. **Compiler 2** emits **SystemVerilog** from RTL-style TLA+.

The refinement loop replaces freeform "rewrite TLA+ as RTL-style TLA+" with a sequence of bounded, verifiable rule applications.

---

## Branch 2 — Verification path (cocotb)

1. **Agent 2** generates a **cocotb** Python testbench from **JSON(S)**.
2. The testbench is applied to the generated RTL module.
3. **Pass** → pipeline returns RTL + artifacts.
4. **Fail** → trust the testbench (see assumptions below); **Agent 3** adjusts JSON(TLA) and the formal branch runs again.

---

## Refinement calculus rules

The Refinement Engine's rule library is drawn from refinement calculus (Back / von Wright / Morgan style; see [TLA_specs.png](./TLA_specs.png)). Each rule is a Python class with three responsibilities:

1. **Applicability check** — given a spec, can this rule fire?
2. **Transformation** — produce the refined spec from the input + parameters
3. **Verification** — the rule's correctness preserves the refinement relation by construction

### Tier-1 rules (must have)

| Rule | Source | Hardware role |
|---|---|---|
| **Initialization** | Table 1 | Reset behavior — every register has an initial value |
| **Iteration** | Table 1 | Free-running clocked logic; the loop body is the per-cycle update |
| **Sequential Composition** | Table 2 | Combinational paths within one cycle |
| **Assignment** | Table 2 | Fundamental register update |
| **Alternation** | Table 2 | Mux / case / FSM branches |
| **Introduce Variable** | Table 2 | Add a register or wire |

### Tier-2 rules (stretch goals)

| Rule | Source | Hardware role |
|---|---|---|
| **Parallel Composition** | Table 1 | Multiple modules running concurrently |
| **Expand / Contract Frame** | Table 2 | Manage which variables a step modifies |
| **Weaken Precondition / Strengthen Postcondition** | Table 2 | Tighten state space bounds |

### Skip (out of scope for 2-week MVP)

Piping / Bidirectional Composition, Procedure rules, full Feasibility — research-grade, not demo-critical.

### LLM action space at each refinement step

| Decision | Determined by |
|---|---|
| What rules exist | Library (fixed, ~6 rules in v1) |
| How a rule transforms a spec | Rule code (deterministic function) |
| Which rule to apply next | **LLM** |
| What parameters to use | **LLM** |

The LLM never writes TLA+ during refinement. It receives the current spec plus the filtered list of applicable rules, and returns a single structured choice `(rule_name, parameters)`. The engine applies it.

---

## Design assumptions

| Assumption | Rationale |
|------------|-----------|
| **JSON(S) is correct for one prompt round** | We cannot know if we misread the user without follow-up questions |
| **Testbench failures imply formal/RTL issues, not bad tests** | cocotb generation from JSON(S) is a short, reliable path vs. the full TLA+ → RTL chain |
| **Tier-1 rules are sufficient for demo designs** | The 6 rules cover Init/Reset, clocked iteration, assignment, branching, sequencing, and variable introduction — enough for counters, FFs, FSMs, muxes, simple datapaths. |
| **LLM can reliably pick rules from a filtered list** | Each refinement step is a small structured-output decision; far smaller error surface than freeform TLA+ generation. |
| **User re-check of expected I/O** | Optional stopgap if we doubt JSON(S) test vectors (backup only) |

---

## Components

| Component | Responsibility |
|-----------|----------------|
| **Agent 1** | NL prompt → JSON(S) |
| **Agent 2** | JSON(S) → cocotb testbench + harness setup |
| **Agent 3** | JSON(S) → JSON(TLA); revise JSON(TLA) from TLC or testbench failures |
| **Compiler 1** | JSON(TLA) → TLA+ |
| **Refinement Engine** | Drives the rule-application loop. Filters applicable rules, applies the LLM's choice, accumulates the refinement chain, supports backtracking. |
| **Rule Picker (LLM)** | At each refinement step, picks `(rule, params)` from the applicable set. Structured JSON output. |
| **Rule Library** | `pipeline/refinement/rules/*.py` — one file per rule, each a subclass of `RefinementRule` |
| **Compiler 2** | RTL-style TLA+ → SystemVerilog |
| **Pipeline coordination** | LangGraph graph, routing, retries, artifact paths |

---

## Retry behavior

| Failure point | Action |
|---------------|--------|
| TLC rejects TLA+ | Fix JSON(TLA) → Compiler 1 → TLC again |
| LLM picks an inapplicable refinement rule | Engine rejects the choice and re-prompts with the filtered applicable list |
| Refinement chain stalls (no applicable rule reaches RTL-style) | Roll back N steps, ask the rule-picker for a different choice at the rollback point (tree search) |
| cocotb fails on RTL | Fix JSON(TLA) → re-run formal branch through RTL regen → re-test |

Rule application itself never "fails" in the retry sense — if a rule is applicable, applying it produces a valid refinement by construction. Only LLM rule *selection* and downstream cocotb simulation can fail.

---

## Scope and team (2 weeks, 5 people)

~60% of person-time goes to the refinement library — the project's load-bearing contribution.

| Person | Focus | Weeks |
|---|---|---|
| **A** | `RefinementRule` ABC + Refinement Engine (applicability filtering, rule application, chain serialization, backtracking) | 2 |
| **B** | Tier-1 rules 1-3 (Initialization, Iteration, Sequential Composition) — encode + unit-test each | 2 |
| **C** | Tier-1 rules 4-6 (Assignment, Alternation, Introduce Variable) — encode + unit-test each | 2 |
| **D** | Agent 1 + Agent 3 + Compiler 1 + Rule Picker LLM + LangGraph orchestration | 2 |
| **E** | Agent 2 (cocotb testbench generator) + Compiler 2 + cocotb runner + integration | 2 |

---

## Related documentation

| Document | Contents |
|----------|----------|
| [PIPELINE.md](../PIPELINE.md) | LangGraph stages, JSON schemas, milestones |
| [README.md](../README.md) | Setup, run instructions, current implementation status |
| [docs.md](./docs.md) | Formal-methods background and refinement concepts |
| [TLA_specs.png](./TLA_specs.png) | The refinement calculus rules that define the rule-picker's action space |
| [system_architecture.png](./system_architecture.png) | Reference visual style for diagrams |
