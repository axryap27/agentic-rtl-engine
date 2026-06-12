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

For sequential arithmetic the bound goes one step further — **verified derivation**. The LLM may author only an abstract postcondition (e.g. `product = a * b`) plus a candidate loop invariant, and a deterministic proof kernel (`pipeline/refinement/obligations.py`) discharges the Morgan/Back refinement obligations against the real expression semantics before the derived loop is ever installed: an exhaustive finite proof over the declared bit widths when the input space is ≤ 65,536, sampled falsification otherwise, and a concrete counterexample on failure. The LLM proposes; the kernel proves.

Every applied rule is logged to `refinement_chain.json`, giving a replayable proof trail from abstract spec down to RTL. Verified derivations carry their discharged proof audit (invariant, variant, guard, proof mode, cases checked) right on the chain entry — a visible certificate.

---

## Architecture: four stages

**LangGraph** orchestrates the run, advancing, retrying, or halting on the `status` field each stage writes to disk. Stages pass data as JSON in the run's artifact directory (`artifacts/<date>/<time-module-hash>/`), so every run is fully inspectable.

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
            re-derive expected outputs from the refined spec,
            run the Verilog against them (cocotb),
            then soak it with seeded random cycles vs the spec
                 │
          pass ->  verified Verilog
          fail ->  diagnoser classifies the fault and routes the fix
```

Technical stack & key features:

- **TLA+ / TLC**: the spec language, and its model checker. TLC explores the spec's state space for invariant violations; on a hit, Agent 3 revises the spec and Compiler 1 + TLC re-run. (TLC is optional — Stage 3 skips the model-checking step if `tlc` is not on PATH.)
- **cocotb**: a Python-based HDL verification framework. The testbench stimulus comes from the Stage 1 vectors, but the **expected outputs are re-derived from the refined spec** by an independent interpreter (`pipeline/cocotb/spec_sim.py` + `vector_check.py`) — a Stage 1 arithmetic slip can't fail a correct design, and Agent-1/spec disagreements are recorded and surfaced rather than silently trusted.
- **Spec-vs-RTL soak**: after a pass, the RTL is hammered with `RTL_SOAK_CYCLES` (default 2000) deterministic random cycles against the spec interpreter, seeded from the run-dir name so any divergence replays from the artifacts alone. A divergence is a deterministic codegen bug — surfaced loudly, without flipping the verdict.
- **Diagnoser**: on a simulation failure, classifies it as a spec fault (wrong behavior, revise the FormalSpec) or a refinement fault (wrong rule parameters, backtrack the chain and re-pick), then routes accordingly.
- **Native core (optional)**: `core/` holds exact C++17 mirrors of the expression evaluator, proof kernel, and spec interpreter — byte-identical verdicts, with the 8-bit exhaustive proof dropping from 23.2 s to 74 ms (~311×). Pure Python remains the reference and the fallback.

The LLM is confined to four jobs: interpreting the prompt, authoring/revising the formal spec, picking refinement rules, and classifying simulation failures. Stage 2, both compilers, the refinement engine and rules, the proof kernel, the vector checker, the soak, and the cocotb runner contain **no LLM calls**, they are deterministic Python.

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
| **Loop Introduction** | Abstract postcondition → proven multi-cycle loop datapath; fires only after the obligation kernel discharges the refinement proof (exhaustive over the declared widths when the input space is ≤ 65,536) — on failed obligations it is a pure no-op, the engine's backtrack signal |
| **Schedule Handshake FSM** | Verified loop → IDLE/BUSY/DONE control FSM with a start/done handshake (an FSMD) |

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
    obligations.py      proof kernel: discharges the Morgan/Back loop obligations
    rules/              the eight rules, one file each
    bridge.py           translates between the stages' spec formats
  refinement_templates/ groups rules into ordered passes (FSM, reset, etc.)
  cocotb/
    generator.py        summary -> cocotb testbench (deterministic)
    spec_sim.py         independent cycle-accurate interpreter of the refined spec
    vector_check.py     re-derives expected outputs from the spec, flags disagreements
    soak.py             post-pass mass random spec-vs-RTL soak
    runner.py           runs the testbench, structured pass/fail report
  nodes/                one runner per stage, plus the diagnoser node
  run_dirs.py           dated, module-named run dirs + the artifacts/latest symlink
  usage.py              Agent 3 spend ledger (prepaid-credit budget guard)
core/                   optional C++17 native kernel (CMake + pybind11): exact
                        mirrors of the evaluator, proof kernel, spec interpreter
main.py                 entry point
tests/                  deterministic test suite (no LLM calls)
```

---

## Setup

**Requirements:** Python 3.11+. To run hardware: `iverilog` and `cocotb` (`verilator` optional, for extra lint; `tlc` optional, for TLA+ model checking — Stage 3 skips the check if it is not installed).

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

### Optional: native core

The proof kernel and the spec interpreter have exact C++17 mirrors under `core/` — same enumeration order, byte-identical verdicts and counterexamples, ~205–311× faster proofs:

```bash
./core/build.sh    # needs CMake >= 3.18, a C++17 compiler, pip-installed pybind11
```

Pure Python stays the reference semantics and the automatic fallback when the module isn't built. `OBLIGATIONS_BACKEND` and `SPECSIM_BACKEND` (`auto` | `python` | `cpp`) pin a backend explicitly.

### Operational knobs

- `AGENT3_BUDGET_USD` / `AGENT3_BUDGET_RESERVE_USD` (defaults `100.0` / `0.50`): hard USD cap on Agent 3 spend. Pre-flight check before every call; once cumulative spend plus the reserve reaches the cap, further calls are refused and the stage writes `status: error`.
- `RTL_SOAK_CYCLES` (default `2000`, `0` disables): length of the post-pass spec-vs-RTL soak.

---

## Running

```bash
python3.11 main.py
```

Runs the full pipeline on the default 2-bit counter. Output lands in a dated, module-named run dir — `artifacts/<date>/<time-module-hash>/`, with `artifacts/latest` pointing at the newest run — every intermediate artifact on disk for inspection. Prune old runs with `python3.11 main.py --clean-artifacts [N]`.

---

## Tests

The suite is **fully deterministic with no LLM calls** — 469 tests, ~30 s — so it is fast, free, and safe to run anytime:

```bash
python3.11 -m pytest tests/ -q
```

It exercises the mechanical path end to end (engine, bridge, both compilers, cocotb) with hand-built specs and a scripted rule-picker standing in for the LLM. Headline coverage:

- the refinement loop converges to RTL on all seven design classes, counter through the FSMD multiplier, ending in a real cocotb PASS,
- verified derivation: the obligation kernel's exhaustive proofs, and exact counterexamples on failure,
- spec-derived golden vectors and the random soak,
- native/Python parity: byte-identical kernel verdicts under a 12,000-case differential expression fuzz, plus spec-sim trace and FIFO parity,
- emitted Verilog is lint-clean and elaborates under Icarus Verilog,
- bit widths survive, free input ports are declared, and status routing is typo-proof (validated `status` envelope).

The D flip-flop test also runs standalone:

```bash
python3.11 tests/test_dff.py
```

Tests that hit the real models live separately (22 opt-in tests under `agentic_tests/`) and are off by default, so a normal run never spends money or touches the network.

---

## Status

| Component | State |
|-----------|-------|
| Stage 1 (prompt interpretation) | Built |
| Stage 2 (testbench generation) | Built, deterministic |
| Stage 3 (spec authoring, verified refinement, codegen) | Built; verified live (Anthropic key required — see Setup) |
| Stage 4 (simulation + spec-derived vectors + soak) | Built |
| Diagnoser (failure routing) | Built |
| Refinement engine + eight rules | Built; six Tier-1 rules plus the verified-derivation pair (Loop Introduction, Schedule Handshake FSM); converges on all seven design classes |
| Native core (optional C++) | Built; byte-identical to the Python reference, ~311× on the 8-bit exhaustive proof |
| Deterministic test suite | 469 passing, ~30 s (plus 22 opt-in live-LLM tests, off by default) |
| Offline end-to-end (mocked LLM) | Verified — NL → RTL → cocotb PASS on seven design classes: counter, traffic-light FSM, multi-op ALU, accumulator, register file (memory arrays), FIFO (combinational outputs), FSMD shift-add multiplier |
| Live end-to-end runs | Verified across all seven design classes |
| Live verified derivation | Run 102611 (2026-06-10): Agent 3 authored only the abstract spec `product = a * b` and proposed the textbook invariant first try; the native kernel proved all 65,536 inputs in-loop; 3 picks, 0 strikes; cocotb PASS; clean 2,000-cycle soak |

The pipeline runs live end to end on all seven design classes. The headline result is the live verified derivation: Agent 3 proposes the abstract spec and the loop invariant, the obligation kernel proves the derived shift-add loop over every one of the 65,536 input pairs before installing it, the scheduler lowers it onto a start/done handshake FSM — and the result passes cocotb and a clean random soak. The bounded action space holds at its hardest test yet: the LLM proposes, the kernel proves.

The artifacts from run `102611` are committed verbatim as an inspectable exhibit — **[`docs/exhibits/102611/`](docs/exhibits/102611/README.md)** — including the hand-authored abstract spec (`02_formal_spec.json`) and the derivation certificate (`refinement_chain.json`) with its discharged invariant and variant. The same mechanism is reproducible offline with no credentials via `pytest tests/test_verified_derivation.py`.

---

Deeper design docs live in [`docs/`](docs/README.md) — start with [`docs/architecture.md`](docs/architecture.md) (full system) and [`docs/refinement.md`](docs/refinement.md) (the engine and rules). `CLAUDE.md` is the contributor contract.
