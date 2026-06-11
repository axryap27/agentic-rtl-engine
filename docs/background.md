# Background & Theory

Why this project routes natural language through a formal layer instead of asking an
LLM for RTL directly, and the refinement-calculus lineage of the rules.

---

## Why formal methods

LLMs can emit RTL from a prompt, but they hallucinate: plausible-looking designs with
subtle logical errors that, in hardware, mean a broken chip — and that are expensive to
catch after the fact. Hardware is especially unforgiving of one-shot generation:

- **Concurrency** — many interdependent blocks run in parallel.
- **Reactivity** — behavior is driven by events, not sequential control flow.
- **Temporality** — correctness is a property across clock cycles, not a single state.
- **Signal dependency** — non-blocking vs. blocking assignment creates ordering hazards.

TLA+ models all of these well, which is why it is the formal layer here. The pipeline
inserts that layer between the prompt and the RTL and crosses it with **correctness
guarantees at every step**.

## Stepwise refinement

A formal spec starts **abstract** — it says *what* should happen. Hardware is
**concrete** — clocks, registers, wires. Bridging that gap is the hard part, and the
naive move ("rewrite this abstract spec as RTL") is exactly the open-ended request that
invites hallucination.

Instead the engine uses **stepwise refinement**: it lowers the spec one transformation
at a time, each a single rule from a small fixed library. Every structural rule is
*refinement-preserving* by construction; `LoopIntroduction` is refinement-preserving by
**per-application mechanical proof** — its iteration-rule provisos are discharged by a
deterministic obligation kernel against the real expression semantics before the loop
is installed, and a failed discharge is a pure no-op (the engine's backtrack signal),
so an unproven derivation can never enter the chain (see
[refinement.md](refinement.md#the-obligation-kernel)). Either way, a design built only
from library rules is correct by construction. The LLM's entire job per step is to pick
the next rule from the set that currently applies — a small structured-output decision,
not freehand code.
Every applied rule is logged, giving a replayable proof trail from abstract spec to RTL.

This is the **bounded action space**: constrain the LLM's choices to a menu of
provably-correct moves and you starve the failure mode. See
[architecture.md](architecture.md#6-the-four-design-invariants) for how the codebase
enforces it and [refinement.md](refinement.md) for the engine that runs it.

---

## The refinement calculus

The rule library is drawn from **refinement calculus** (Back / von Wright / Morgan).
A specification statement is written `w : [pre, dur, post]` (frame `w`, precondition,
during-condition, postcondition); `⊑` reads "is refined by". Each rule rewrites a
statement into a more concrete one under stated provisos, and the rewrite preserves the
refinement relation.

The two tables below are the source menu. The pipeline implements **eight rules
today**: the six structural Tier-1 rules marked **✔** below, plus the
verified-derivation pair — **LoopIntroduction**, which implements the Table-1
*Iteration* lineage in full (Morgan's iteration rule / Back's do–od introduction,
with the provisos `O1: pre ⇒ inv[init]`, `O2: inv ∧ guard ⇒ inv[body]` ∧ variant
decreases, `O3: inv ∧ ¬guard ⇒ post` **mechanically discharged** per application by
the [obligation kernel](refinement.md#the-obligation-kernel) before a loop is
installed), and **ScheduleHandshakeFSM**, the deterministic FSMD scheduling of the
verified loop (no proof of its own — soundness lives in LoopIntroduction). The
remaining rows are Tier-2 / research-grade and are not implemented. When adding a
rule with `/add-refinement-rule`, record it in the
appropriate table here, and implement it per [refinement.md](refinement.md#adding-a-rule).

### Table 1 — process-level development

Shorthand `ss = w : [pre, dur, false] ‖ env`.

| Rule | Implemented | Hardware role |
|---|:---:|---|
| Weaken Environment | | relax assumptions on the surrounding environment |
| Strengthen During | | tighten the during-condition |
| Parallel Composition | Tier-2 | independent modules running concurrently |
| Piping Composition | | producer→consumer dataflow between processes |
| Bidirectional Composition | | two-way coupled processes |
| **Initialization** | **✔** | reset behavior — every register gets an initial value |
| **Iteration** | **✔** | free-running clocked logic; the loop body is the per-cycle update |

### Table 2 — control- and data-flow development

Shorthand `ss = w : [pre, dur, post]`.

| Rule | Implemented | Hardware role |
|---|:---:|---|
| Weaken Precondition | Tier-2 | widen the accepted input state space |
| Strengthen Postcondition | Tier-2 | tighten the guaranteed output state space |
| Expand Frame | Tier-2 | add a variable a step may modify |
| Contract Frame | Tier-2 | remove a variable from a step's frame |
| **Sequential Composition** | **✔** | combinational steps within one cycle |
| **Assignment** | **✔** | fundamental register update |
| Concurrent Assignment | | simultaneous independent assignments |
| Leading / Following Assignment | | reorder assignments around a step |
| Skip Statement | | a no-op step when `pre ∧ dur ⇒ post` |
| **Introduce Variable** | **✔** | add a register or wire |
| **Alternation** | **✔** | mux / case / FSM branches |
| Procedure Assignment / Specification | | factor a sub-specification into a procedure |
| Feasibility | | the statement is implementable |

The implemented eight cover reset/init, clocked iteration, assignment, branching,
sequencing, and variable introduction — enough for counters, flip-flops, FSMs, muxes,
and simple FSM+datapath designs — and, via the verified-derivation pair, refining an
abstract arithmetic specification statement (e.g. `product' = a * b`) into an
obligation-checked multi-cycle FSMD datapath behind a start/done handshake (the
sequential-multiplier class). Their exact applicability conditions and parameters
are in [refinement.md](refinement.md#the-rule-library).

---

## Research directions (not implemented)

The original framing reached past the current implementation; recording it here so the
ambition is legible without implying it exists:

- **Function-preserving rules + PPA.** Optimization rules that improve power /
  performance / area without changing behavior, evaluated with tools like Yosys or
  OpenROAD (possibly with a surrogate PPA model for intermediate versions).
- **Alternative RTL backends.** A guarded-atomic-action HDL (e.g. Bluespec) as an
  alternative to the custom TLA+ → Verilog compiler.
- **Benchmark suites.** VerilogEval, RTLLM, VeriThoughts, CVDP for systematic
  evaluation.

Today the RTL backend is the deterministic [Compiler 2](compilers.md#compiler-2), and
verification is functional (cocotb), not PPA.

---

## References

- Refinement calculus — Back & von Wright, *Refinement Calculus: A Systematic
  Introduction*; Morgan, *Programming from Specifications*.
- Refinement mappings — Hillel Wayne, ["Refinement"](https://hillelwayne.com/post/refinement/).
- TLA+ — Lamport, [TLA+ tutorial](https://lamport.azurewebsites.net/tla/tutorial/intro.html);
  [`learntla.com`](https://learntla.com).
- [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) — orchestration.
- [cocotb](https://www.cocotb.org/) — Python HDL verification.
