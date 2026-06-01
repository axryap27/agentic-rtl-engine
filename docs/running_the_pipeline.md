# Running the Pipeline

## Prerequisites

- Python 3.11+
- API keys in `.env` (see below)
- For end-to-end simulation: `cocotb`, `iverilog`, and `vvp` on PATH

```bash
pip install -r requirements.txt cocotb
# macOS: brew install icarus-verilog
```

TLC (TLA+ model checker) is optional — Stage 3 skips it if not installed.

---

## Setup

```bash
cp .env.example .env
```

Fill in:

| Variable | Used by |
|----------|---------|
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | Agent 1 (NL → summary) |
| `ANTHROPIC_API_KEY` | Agent 3 (formal spec, refinement, revisions) |

Both keys are required for a full run through RTL generation.

---

## Run

From the repo root:

```bash
python3.11 main.py
```

Default spec is a 2-bit counter. Pass a custom prompt as arguments:

```bash
python3.11 main.py "Design a synchronous D flip-flop with active-high reset."
```

Each run creates a unique folder: `artifacts/<run_id>/`.

---

## Output artifacts

| File | Stage | Contents |
|------|-------|----------|
| `00_nl_spec.json` | entry | Your prompt |
| `01_summary.json` | 1 | Ports, behavior, test vectors |
| `02_testbench.py` | 2 | cocotb testbench |
| `02_formal_spec.json` | 3 | Formal spec (JSON TLA) |
| `03_rtl_output.json` | 3 | Verilog metadata |
| `output.v` | 3 | Generated Verilog |
| `04_evaluation.json` | 4 | cocotb pass/fail |
| `refinement_chain.json` | 3 | Refinement rule trace |

Success: `04_evaluation.json` has `"status": "success"`.

---

## Subsystem tests (no full LLM run)

```bash
python3.11 tests/test_refinement_convergence.py
python3.11 tests/test_compilers.py
python3.11 tests/test_cocotb_roundtrip.py
```
