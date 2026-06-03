# Agentic RTL Engine

> **"bounded action space tames hallucination" (Claude-Code Opus 4.8, June 2026)**

Turn a plain-English description of a digital circuit into real, synthesizable hardware code (Verilog), with checks at every step so the output is trustworthy.

That one line at the top is the whole idea, so here it is in plain terms. Large language models are great at writing code but they also "hallucinate," meaning they confidently produce things that are subtly or completely wrong. Hardware is unforgiving: one wrong line and the chip misbehaves. So instead of asking an AI to write a big pile of hardware code in one shot and hoping it is correct, this project only ever lets the AI make **small, pre-approved moves from a short menu**. The menu is the "bounded action space." Each move on the menu is mathematically guaranteed to keep the design correct. The AI chooses *which* move to make next; it never freehand-writes the hardware. Narrow the choices, and you starve the hallucination.

---

## What it actually does

You give it a sentence like:

> "A 2-bit counter that increments every clock cycle when enabled, and resets to zero."

and it produces a verilog file that a real hardware toolchain will accept, after automatically checking that the design behaves the way you asked.

---

## The core idea, a little deeper

To get from natural language (NL) to correct hardware description, the pipeline goes through an in-between form called a **formal specification** using precise, math-backed description of how the circuit should behave, with no ambiguity. This is written in **TLA+**.

The catch is that a formal spec starts out **abstract** (it says *what* should happen) and hardware needs something **concrete** (it says *how*, in terms of clocks, registers, and wires). Closing that gap is the hard part.

The usual temptation is to ask an AI to "rewrite this abstract spec as concrete hardware." That is exactly the open-ended, hallucination-prone request we want to avoid. Instead we use **stepwise refinement**: we lower the spec toward hardware one tiny step at a time, where each step is a single rule from a small fixed library. Every rule is provably correctness-preserving, so if you only ever apply rules from the library, the end result is correct by construction. The AI's only job at each step is to pick the next rule from the handful that currently apply. That is the bounded action space in action.

A running log of every rule applied (`refinement_chain.json`) becomes a step-by-step proof trail from the abstract idea down to the hardware.

---

## How it works: four stages

The whole thing is orchestrated by **LangGraph**, which runs each stage and decides whether to move on, retry, or stop based on a `status` field each stage writes to disk. Stages hand data to each other as JSON files under `artifacts/<run_id>/`, so every run is fully inspectable after the fact.

```
        Plain-English prompt
                 │
                 ▼
   Stage 1  Understand the request
            (AI) prompt  ->  structured summary + test cases
                 │
        ┌────────┴─────────────────────────────┐
        ▼                                       ▼
   Stage 3  Build + lower the spec         Stage 2  Build the tests
   (AI + math tools)                       (no AI, pure templates)
     summary -> formal spec (TLA+)           summary -> a cocotb testbench
       -> checked by TLC
       -> refined one rule at a time
       -> compiled to Verilog
        │                                       │
        └────────┬──────────────────────────────┘
                 ▼
   Stage 4  Test the hardware
            run the Verilog against the testbench
                 │
          pass ->  done, you get verified Verilog
          fail ->  a diagnoser figures out what went wrong
                   and sends it back to be fixed
```

A quick gloss on the unfamiliar names:

- **TLA+**: a precise language for describing how a system behaves, so a tool can check it.
- **TLC**: the checker for TLA+. It explores the spec looking for ways it could break. If it finds one, the AI revises the spec and tries again.
- **cocotb**: a Python framework for testing hardware. We auto-generate a testbench (a set of inputs and expected outputs) directly from your request, then run the generated Verilog against it.
- **Refinement rules**: the short menu of correctness-preserving moves (see below).
- **Diagnoser**: when a test fails, this decides whether the *idea* was wrong (fix the spec) or a *refinement step* was wrong (back up a few steps and try different choices), and routes the fix accordingly.

Notice that Stage 2 and the compilers have **no AI in them at all**. They are plain, deterministic Python. The AI is confined to three narrow jobs: understanding the prompt, authoring the formal spec, and picking refinement rules. Everything else is mechanical and repeatable.

---

## The refinement library (the heart of the project)

These are the only moves the AI can make to turn an abstract spec into hardware. Each one maps to a real hardware concept:

| Rule | What it does in hardware terms |
|------|--------------------------------|
| **Initialization** | Give every register a defined reset value |
| **Iteration** | Make logic run once per clock tick (a clocked register) |
| **Sequential Composition** | Chain steps that happen within one clock cycle |
| **Assignment** | Update a register with a new value |
| **Alternation** | Branch (an if/else, a mux, a case statement) |
| **Introduce Variable** | Add a new register or wire |

At each step the engine figures out which of these *can* legally apply, hands that short list to the AI, the AI picks one, and the engine applies it mechanically. The AI never writes Verilog or TLA+ directly during refinement; it only ever returns a choice like `(rule, parameters)`.

---

## Project layout

```
pipeline/
  graph.py              LangGraph wiring: runs stages, routes on status
  state.py              the small bit of state carried between stages
  schemas/              data shapes for every artifact (Pydantic models)
  agents/
    agent1.py           Stage 1: prompt -> structured summary (AI)
    agent3.py           Stage 3: authors + revises the formal spec, picks rules (AI)
    agent_diagnoser.py  on a test failure, decides how to route the fix (AI)
  compilers/
    compiler1.py        structured spec -> TLA+ text (no AI)
    compiler2.py        refined spec -> synthesizable Verilog (no AI)
  refinement/
    engine.py           the loop: filter applicable rules, apply the choice, log it
    rules/              the six rules above, one file each
    bridge.py           translates between the spec formats the stages use
  refinement_templates/ groups the rules into ordered passes (FSM, reset, etc.)
  cocotb/
    generator.py        summary -> a cocotb testbench (no AI)
    runner.py           runs the testbench, reports pass/fail with detail
  nodes/                one runner per stage, plus the diagnoser node
main.py                 entry point
tests/                  deterministic test suite (no AI calls, see below)
```

---

## Setup

**Requirements:** Python 3.11+, and for actually running hardware: `iverilog` (Icarus Verilog) and `cocotb`. `verilator` is handy for extra lint checks.

```bash
pip install -r requirements.txt
```

### Two separate AI accounts

This project talks to AI in two different ways, so it needs two sets of credentials. Copy `.env.example` to `.env` and fill them in:

1. **Stages 1 and the diagnoser** go through an OpenAI-compatible proxy:
   ```
   LLM_BASE_URL=...     # the proxy URL
   LLM_API_KEY=...      # the proxy key
   LLM_MODEL=...        # which model the proxy should use
   ```
2. **Agent 3** (the spec author and rule picker) talks to Anthropic directly and needs its **own** key:
   ```
   ANTHROPIC_API_KEY=...   # a real Anthropic API key
   AGENT3_MODEL=...        # optional; which model Agent 3 uses
   ```

Why two? Agent 3 is deliberately built as a distinct, tool-using Anthropic agent, so it has its own credential. Until that key is set, Stages 1 and 2 still run, but Stage 3 stops with a clear "key not configured" message. Note that this Anthropic key is billed by usage and is separate from any Claude subscription (a subscription does not cover direct API calls).

---

## Running the pipeline

```bash
python3.11 main.py
```

This runs the full pipeline on the default 2-bit counter. Results land in `artifacts/<run_id>/`, with a fresh ID per run so nothing gets overwritten. You can open any of the JSON files to see exactly what each stage produced.

---

## Running tests

The test suite is **fully deterministic and makes no AI calls**, so it is fast, free, and safe to run anytime:

```bash
python3.11 -m pytest tests/ -q
```

It exercises the mechanical parts end to end (the refinement engine, the bridge, both compilers, and the cocotb run) using hand-built specs and a scripted rule-picker that stands in for the AI. The headline checks:

- the refinement loop actually converges to hardware on a counter and a D flip-flop,
- the generated Verilog is lint-clean and elaborates under Icarus Verilog,
- multi-bit signals keep their width, input ports are declared, and the status routing is typo-proof.

The D flip-flop test also runs standalone, matching the dev guide:

```bash
python3.11 tests/test_dff.py
```

Tests that call the real AI models will live separately and stay off by default, so a normal test run never spends money or hits the network.

---

## Current status

| Piece | Status |
|-------|--------|
| Stage 1 (understand the prompt) | Built |
| Stage 2 (generate the testbench) | Built, deterministic, no AI |
| Stage 3 (author spec, refine, compile to Verilog) | Built; needs the Agent 3 key to run live |
| Stage 4 (run the testbench) | Built |
| Diagnoser (route failures) | Built |
| Refinement engine + six rules | Built; converges on counter and flip-flop |
| Deterministic test suite | 58 passing |
| Live end-to-end run with real AI | Pending the Agent 3 key setup |

The whole machine is wired and the mechanical spine is proven. The next milestone is the first live run, where the real AI drives the refinement loop, which is the moment the central "bounded action space" idea gets tested for real.

---

For the deeper design docs see `docs/architecture.md` (full system), `docs/layers_of_templates.md` (the refinement pass structure), and `CLAUDE.md` (the working contract for contributors).
