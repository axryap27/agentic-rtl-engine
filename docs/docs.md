# Stepwise RTL Refinement

An agentic LLM pipeline that generates verified RTL from natural language specifications via stepwise formal refinement.

---

## Overview

LLMs can generate RTL code directly from natural language, but they hallucinate — producing plausible-looking designs with subtle logical errors that are hard to catch after the fact. This project attacks that problem by inserting a **formal intermediate layer** between the natural language spec and the final RTL, and using **stepwise refinement** to traverse it with correctness guarantees at every step.

The pipeline: NL spec → TLA+ formal spec → formally refined implementation → RTL (via BlueSpec BSV or a custom compiler).

---

## Architecture

```
Design Specification (NL)
        │
        ▼
 [Auto-formalization]
        │
        ▼
 Formal Specification (TLA+)
        │
        ▼
 [Stepwise Refinement]  ◄──── Refinement Rules / Function-Preserving Rules
        │
        ▼
 Formal Implementation (PlusCal / TLA+)
        │
        ▼
   [RTL Generation]
        │
        ▼
 RTL Implementation (Verilog)
```

---

## Stages

### 1. Auto-Formalization

Translates a natural language hardware description into a formal TLA+ specification.

**Agents:**
- **Translation node** — LLM generates initial TLA+ spec from NL
- **Syntax checking node** — verifies the spec compiles via the TLA+ CLI
- **Syntax correction node** — iteratively fixes compilation errors
- **Semantics judge node** — verifies functional requirements are correctly captured

Also extracts non-functional requirements (timing, area, power constraints) for downstream stages.

**Resources:** [TLA+ CLI](https://learntla.com/topics/cli.html)

---

### 2. Stepwise Refinement

Progressively refines the TLA+ spec toward a concrete implementation through a sequence of verified steps. Each intermediate version is a mixture of specification and implementation. 

**Key concepts:**

- **Refinement rules** — pre-defined templates, each encoding a design decision, a transformation, and a proof of correctness. The next version and refinement mapping are derived from the template.
- **Refinement mapping** — describes the correspondence between state variables of adjacent versions. Used to check that each new version correctly implements the previous one.
- **Refinement tree** — the search space. Nodes are program versions, edges are refinement rules. Infeasible nodes trigger backtracking.
- **Function-preserving rules** — optimization-focused rules that improve PPA (power, performance, area) without changing behavior.

**Implementation:** Tree search agent selects, applies, and verifies refinement rules. LangGraph is the recommended orchestration framework.

**Resources:**
- [Refinement mapping explainer](https://hillelwayne.com/post/refinement/)
- [Refinement calculus](https://dl.acm.org/doi/pdf/10.1007/s001650200032)
- [TLA+ tutorial](https://lamport.azurewebsites.net/tla/tutorial/intro.html)
- [PlusCal cheatsheet](https://github.com/tlaplus/PlusCalCheatSheet/blob/main/pluscal.pdf)

---

### 3. RTL Generation

Compiles the final PlusCal/TLA+ formal implementation to synthesizable RTL.

**Recommended path:** [BlueSpec BSV](https://web.ece.ucsb.edu/its/bluespec/) — a hardware description language based on guarded atomic transactions that compiles automatically to Verilog. Alternatively, implement a custom PlusCal → Verilog compiler.

After RTL generation, run simulation on benchmark testbenches to verify functional correctness.

**PPA evaluation:** Use [Yosys](https://github.com/yosyshq/yosys), [Genus](https://docs.amd.com/r/en-US/ug1399-vitis-hls/HLS-Pragmas), or [OpenROAD](https://github.com/the-openroad-project) to measure power, performance, and area. A surrogate model can also be trained to predict PPA for intermediate versions.

---

## Benchmarks

| Benchmark | Link |
|-----------|------|
| VerilogEval | https://github.com/NVlabs/verilog-eval |
| RTLLM | https://github.com/hkust-zhiyao/RTLLM |
| VeriThoughts | https://github.com/wilyub/VeriThoughts |
| CVDP | https://github.com/NVlabs/cvdp_benchmark |

---

## Why Formal Methods?

Hardware design has properties that make LLM-only generation especially fragile:

- **Concurrency** — multiple interdependent modules running in parallel
- **Reactivity** — behavior driven by events, not sequential control flow
- **Temporality** — correctness requires reasoning across time steps
- **Signal dependency** — non-blocking assignments create subtle ordering issues

TLA+ is well-suited to model all of these. The temporal variable `T` (initialized to 0 at reset, incremented each process iteration) provides a uniform way to reason about time across the formal spec and implementation.

---

## Tooling

| Tool | Purpose |
|------|---------|
| [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) | Agentic workflow orchestration |
| [TLA+ / PlusCal](https://lamport.azurewebsites.net/tla/tutorial/intro.html) | Formal specification and implementation |
| [BlueSpec BSV](https://web.ece.ucsb.edu/its/bluespec/) | PlusCal → Verilog compilation |
| [Yosys](https://github.com/yosyshq/yosys) / [OpenROAD](https://github.com/the-openroad-project) | PPA evaluation |
| Claude Code / Cursor | LLM-assisted development |

---

## Project Groups

| Group | Responsibility |
|-------|---------------|
| 1 | Auto-formalization (NL → TLA+, syntax checking pipeline) |
| 2 | Refinement rules + refinement agent (tree search) |
| 3 | Function-preserving rules + PPA evaluation |
| 4 | RTL generation + benchmark testing |