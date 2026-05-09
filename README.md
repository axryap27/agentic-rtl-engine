# Agentic RTL Engine

Generates synthesizable Verilog from a plain-English hardware description using a 4-stage LangGraph pipeline with formal verification at each step.

---

## What it does

You give it a natural language spec like:

> "A positive-edge-triggered D flip-flop with synchronous active-low reset."

It runs through four stages and produces a verified, synthesizable `.v` file:

```
Natural language description
        │
        ▼
[Stage 1: Formalization]   Claude → TLA+ formal spec (.tla + .cfg)
        │
        ▼
[Stage 2: Refinement]      Applies rules → PlusCal implementation with BSV mappings
        │
        ▼
[Stage 3: Codegen]         Claude → synthesizable Verilog-2001, lint-checked
        │
        ▼
[Stage 4: Evaluation]      iverilog / Yosys / VerilogEval benchmarks + PPA report
```

---

## How it works

**LangGraph** is the orchestrator. Each stage is a Python function `(state) -> state` registered as a node in a `StateGraph`. After each node, a router checks whether to retry, advance, or halt — based on the `status` field in the stage's output artifact.

**State is intentionally thin** — it only carries `run_id`, `retry_counts`, and `halt`. The actual design data (TLA+ specs, PlusCal, Verilog, evaluation results) flows between stages as JSON files on disk under `artifacts/<run_id>/`.

**Retry logic** is built into every stage. If Claude produces invalid TLA+ or Verilog that fails lint, the error is injected back into the next prompt and the stage retries automatically (up to the per-stage limit).

---

## Project layout

```
pipeline/
  graph.py          # LangGraph StateGraph with conditional retry/halt edges
  state.py          # PipelineState TypedDict
  schemas.py        # Pydantic v2 schemas for all 4 stage artifacts
  llm.py            # Shared Anthropic client with prompt caching
  nodes/
    stage1.py       # Formalization: NL → TLA+ (Claude)
    stage2.py       # Refinement: TLA+ → PlusCal (stub)
    stage3.py       # Codegen: PlusCal → Verilog (Claude)
    stage4.py       # Evaluation: Verilog → benchmarks + PPA (stub)
tests/
  test_dff.py       # Integration test: generates a D flip-flop end-to-end
main.py             # Entry point
PIPELINE.md         # Full stage interface contracts and milestone schedule
```

---

## Setup

**Requirements:** Python 3.11+

```bash
pip install -r requirements.txt
```

**Get an API key** from [console.anthropic.com](https://console.anthropic.com) → API Keys. It starts with `sk-ant-`.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

This makes the key available to the pipeline for the current terminal session.

---

## Running the pipeline

The default `main.py` runs the full pipeline on a hardcoded 2-bit counter spec (the integration baseline):

```bash
python3.11 main.py
```

Artifacts are written to `artifacts/<run_id>/`. Each run gets a fresh UUID so runs never overwrite each other.

---

## Running tests

### D flip-flop integration test

Exercises Stage 1 (Claude → TLA+) and Stage 3 (Claude → Verilog) on a simple D flip-flop. Stage 2 is bypassed with a handcrafted artifact so the test runs with two Claude calls and no other dependencies.

```bash
python3.11 tests/test_dff.py
```

What it checks:
- Stage 1 produces a valid `01_formal_spec.json` with `status: success`
- Stage 3 produces a `03_rtl_output.json` with `status: success` or `partial`
- The generated `.v` file exists, contains a `dff` module, and uses `posedge clk`
- Lint passes (via `verilator` or `iverilog` if either is on PATH)

The generated Verilog is printed to stdout at the end so you can inspect it directly.

Artifacts are saved to `artifacts/<run_id>/` for debugging.

---

## Current status

| Stage | Status | Notes |
|-------|--------|-------|
| Stage 1 | Real | Claude generates TLA+ from NL |
| Stage 2 | Stub | Hardcoded counter; real refinement rules in progress |
| Stage 3 | Real | Claude generates Verilog, lint-checked |
| Stage 4 | Stub | Hardcoded dummy scores; Yosys/benchmark runners in progress |
