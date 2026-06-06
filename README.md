# Agentic RTL Engine

> **"bounded action space tames hallucination" (Claude-Code Opus 4.8, June 2026)**

Turns a natural-language description of a digital circuit into synthesizable Verilog, with a formal-verification check at every step.

The tagline is the design thesis. An LLM asked to emit RTL in one shot will hallucinate: confidently wrong code that, in hardware, means a broken chip. So the LLM never freehand-writes hardware here. At each step it picks one move from a short, fixed menu of correctness-preserving transformations. The menu is the bounded action space. Constrain the choices and you starve the failure mode.

---

## What it does

Given a prompt like:

> "A 2-bit counter that increments every clock cycle when enabled, and resets to zero."

it emits a synthesizable `.v` file and confirms, automatically, that the design matches the requested behavior.

---

## The core idea

The path from prompt to hardware runs through a **formal specification**: an unambiguous, machine-checkable description of the circuit's behavior, written in **TLA+** (a specification language for describing and model-checking systems).

A formal spec begins **abstract** (it says *what* should happen) while hardware is **concrete** (clocks, registers, wires). Bridging that gap is the hard problem, and the naive approach, asking an LLM to "rewrite this abstract spec as RTL," is exactly the open-ended request that invites hallucination.

Instead the engine uses **stepwise refinement**: it lowers the spec toward hardware one transformation at a time, each one a single rule from a small fixed library. Every rule is provably refinement-preserving, so a design built only from library rules is correct by construction. The LLM's entire job per step is to choose the next rule from the set that currently applies. It returns a `(rule, parameters)` choice and nothing else.

Every applied rule is logged to `refinement_chain.json`, giving a replayable proof trail from abstract spec down to RTL.

---

## Architecture: four stages

**LangGraph** orchestrates the run, advancing, retrying, or halting on the `status` field each stage writes to disk. Stages pass data as JSON under `artifacts/<run_id>/`, so every run is fully inspectable.

```
        Natural-language prompt
                 │
                 ▼
   Stage 1  Interpret the request
            (LLM)  prompt  ->  structured summary + test vectors
                 │
        ┌────────┴─────────────────────────────┐
        ▼                                       ▼
   Stage 3  Author + lower the spec        Stage 2  Generate the testbench
   (LLM + deterministic tools)             (deterministic, no LLM)
     summary -> formal spec (TLA+)           summary -> cocotb testbench
       -> model-checked by TLC
       -> refined one rule at a time
       -> compiled to Verilog
        │                                       │
        └────────┬──────────────────────────────┘
                 ▼
   Stage 4  Simulate
            run the Verilog against the testbench
                 │
          pass ->  verified Verilog
          fail ->  diagnoser classifies the fault and routes the fix
```

Technical stack & key features:

- **TLA+ / TLC**: the spec language, and its model checker. TLC explores the spec's state space for invariant violations; on a hit, Agent 3 revises the spec and Compiler 1 + TLC re-run.
- **cocotb**: a Python-based HDL verification framework. The testbench is generated mechanically from the Stage 1 test vectors and run against the emitted RTL.
- **Diagnoser**: on a simulation failure, classifies it as a spec fault (wrong behavior, revise the FormalSpec) or a refinement fault (wrong rule parameters, backtrack the chain and re-pick), then routes accordingly.

The LLM is confined to three jobs: interpreting the prompt, authoring/revising the formal spec, and picking refinement rules. Stage 2, both compilers, and the cocotb runner contain **no LLM calls**, they are deterministic Python.

---

## The refinement library

The complete set of moves the LLM can make to lower an abstract spec to RTL. Each maps to a hardware construct:

| Rule | Hardware meaning |
|------|------------------|
| **Initialization** | Reset value for every register |
| **Iteration** | Clocked, per-cycle update (a register) |
| **Sequential Composition** | Combinational steps within one cycle |
| **Assignment** | Register update |
| **Alternation** | Branch (if/else, mux, case) |
| **Introduce Variable** | New register or wire |

The engine filters the library to the rules currently applicable, hands that set to the LLM, applies the chosen rule deterministically, and appends it to the chain. The LLM never emits TLA+ or Verilog during refinement.

---

## Project layout

```
pipeline/
  graph.py              LangGraph wiring: runs stages, routes on status
  state.py              thin inter-stage state (run_id, retry_counts, halt)
  schemas/              Pydantic models for every artifact
  agents/
    agent1.py           Stage 1: prompt -> structured summary (LLM)
    agent3.py           Stage 3: authors/revises the spec, picks rules (LLM)
    agent_diagnoser.py  classifies + routes simulation failures (LLM)
  compilers/
    compiler1.py        structured spec -> TLA+ text (deterministic)
    compiler2.py        refined spec -> synthesizable Verilog (deterministic)
  refinement/
    engine.py           the loop: filter applicable rules, apply choice, log
    rules/              the six rules, one file each
    bridge.py           translates between the stages' spec formats
  refinement_templates/ groups rules into ordered passes (FSM, reset, etc.)
  cocotb/
    generator.py        summary -> cocotb testbench (deterministic)
    runner.py           runs the testbench, structured pass/fail report
  nodes/                one runner per stage, plus the diagnoser node
main.py                 entry point
tests/                  deterministic test suite (no LLM calls)
```

---

## Setup

**Requirements:** Python 3.11+. To run hardware: `iverilog` and `cocotb` (`verilator` optional, for extra lint).

```bash
pip install -r requirements.txt
```

### Two credential sets

The pipeline uses two LLM transports, so it needs two sets of keys. Copy `.env.example` to `.env`:

1. **Stage 1 and the diagnoser** use an OpenAI-compatible proxy:
   ```
   LLM_BASE_URL=...     # proxy URL
   LLM_API_KEY=...      # proxy key
   LLM_MODEL=...        # model the proxy serves
   ```
2. **Agent 3** talks to Anthropic directly with its own key:
   ```
   ANTHROPIC_API_KEY=...   # Anthropic API key
   AGENT3_MODEL=...        # optional; Agent 3's model
   ```

Agent 3 is intentionally a distinct, tool-using Anthropic agent with its own credential. Until that key is set, Stages 1 and 2 run but Stage 3 halts with a clear "key not configured" error. This Anthropic key is billed per-token and is separate from any Claude subscription (a subscription does not cover direct API usage).

---

## Running

```bash
python3.11 main.py
```

Runs the full pipeline on the default 2-bit counter. Output lands in `artifacts/<run_id>/`, fresh ID per run, every intermediate artifact on disk for inspection.

---

## Tests

The suite is **fully deterministic with no LLM calls**, so it is fast, free, and safe to run anytime:

```bash
python3.11 -m pytest tests/ -q
```

It exercises the mechanical path end to end (engine, bridge, both compilers, cocotb) with hand-built specs and a scripted rule-picker standing in for the LLM. Headline coverage:

- the refinement loop converges to RTL on a counter and a D flip-flop,
- emitted Verilog is lint-clean and elaborates under Icarus Verilog,
- bit widths survive, free input ports are declared, and status routing is typo-proof (validated `status` envelope).

The D flip-flop test also runs standalone:

```bash
python3.11 tests/test_dff.py
```

Tests that hit the real models live separately and are off by default, so a normal run never spends money or touches the network.

---

## Status

| Component | State |
|-----------|-------|
| Stage 1 (prompt interpretation) | Built |
| Stage 2 (testbench generation) | Built, deterministic |
| Stage 3 (spec authoring, refinement, codegen) | Built; needs the Agent 3 key for live runs |
| Stage 4 (simulation) | Built |
| Diagnoser (failure routing) | Built |
| Refinement engine + six rules | Built; converges on counter and flip-flop |
| Deterministic test suite | 58 passing |
| Live end-to-end run | Pending Agent 3 key setup |

The full pipeline is wired and the deterministic spine is verified. The next milestone is the first live run, where the LLM drives refinement and the bounded-action-space thesis gets its real test.

---

Deeper design docs: `docs/architecture.md` (full system), `docs/layers_of_templates.md` (refinement passes), `CLAUDE.md` (contributor contract).
