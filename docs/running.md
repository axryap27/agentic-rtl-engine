# Running the Pipeline

## Prerequisites

- **Python 3.11+**
- For end-to-end simulation: `iverilog` + `vvp` (Icarus Verilog) and `cocotb` on PATH.
  `verilator` is optional (extra lint). TLC (the TLA+ model checker) is optional — Stage
  3 skips model checking if it is not installed.
- *Optional* — the **native verification core** (`core/`): `cmake` ≥ 3.18, a C++17
  compiler, and `pip install pybind11`. `./core/build.sh` configures, builds, runs the
  C++ tests, and installs the `_rtlcore` module into `pipeline/refinement/`, where the
  obligation kernel and spec simulator auto-detect it (~205–311× faster exhaustive
  proofs, ~59× faster spec-sim). Everything works without it — both fall back to pure
  Python automatically, with identical results. See [core/README.md](../core/README.md).

```bash
pip install -r requirements.txt
# macOS: brew install icarus-verilog

# optional native core:
pip install pybind11 && ./core/build.sh
```

## Credentials

The pipeline uses two LLM transports, so it needs two credential sets. Copy the
template and fill it in:

```bash
cp .env.example .env
```

| Variable | Used by |
|---|---|
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | Agent 1 and the Diagnoser (OpenAI-compatible proxy) |
| `ANTHROPIC_API_KEY` | Agent 3 (Anthropic SDK, direct) |
| `AGENT3_MODEL` *(optional)* | Agent 3's model (default `claude-opus-4-5`) |
| `AGENT3_BUDGET_USD`, `AGENT3_BUDGET_RESERVE_USD` *(optional)* | Agent 3 spend cap (defaults `100.0` / `0.50`) |

Both transports are required for a full run through RTL generation. Until
`ANTHROPIC_API_KEY` is a real key, Stages 1–2 run but Stage 3 halts with a clear "key
not configured" error. The Anthropic key is billed per token and is **separate from any
Claude subscription** — a subscription does not cover direct API usage. See
[agents.md](agents.md#two-transports) for why the transports are split.

Three optional runtime knobs (no credentials involved):

| Variable | Effect |
|---|---|
| `OBLIGATIONS_BACKEND` | obligation-kernel backend: `auto` (default — native core iff built), `python`, or `cpp` (errors if not built) |
| `SPECSIM_BACKEND` | the same three choices for the spec-simulator cycle engine |
| `RTL_SOAK_CYCLES` | length of Stage 4's post-pass spec-vs-RTL soak (default `2000`; `0` disables) |

The backend knobs select an *implementation*, never a verdict — both backends produce
identical results, so artifacts and chain replay do not depend on which one ran.

## Run

```bash
python3.11 main.py                  # default: a synchronous 2-bit up-counter
python3.11 main.py "Design a synchronous D flip-flop with active-high reset."
python3.11 main.py --clean-artifacts [N]   # prune all but the N newest runs (default 10)
```

`main.py` creates a fresh `run_id`, writes your prompt to
`artifacts/<run_id>/00_nl_spec.json`, invokes the LangGraph pipeline, and reports the
terminal result. Every intermediate artifact is left on disk for inspection.

Run directories are date-prefixed and human-readable —
`artifacts/<YYYY-MM-DD>/<HHMMSS>-<module>-<hash>/` (the module name is spliced into the
leaf once the run completes) — and `artifacts/latest` is a symlink refreshed to point at
the newest run. `--clean-artifacts` keeps the tree from growing without bound.

## Output artifacts

Everything lands under `artifacts/<run_id>/`:

| File | Stage | Contents |
|---|---|---|
| `00_nl_spec.json` | entry | your prompt |
| `01_summary.json` | 1 | ports, behavior, golden test vectors |
| `02_testbench.py` + `02_testbench_meta.json` | 2 | cocotb testbench + status |
| `02_formal_spec.json` | 3 | the formal spec (JSON(TLA)) |
| `03_rtl_output.json` + `output.v` | 3 | Verilog metadata + the generated module |
| `refinement_chain.json` | 3 | the ordered rule-application trace |
| `refinement_decisions.jsonl` | 3 | per-`pick_rule` decision log |
| `02_vector_check.json` + `02_testbench_specvec.py` | 4 (pre-flight) | spec-derived golden vectors + Agent-1/spec disagreement report |
| `04_evaluation.json` | 4 | cocotb pass/fail (structured) |
| `04_soak.json` + `04_soak_testbench.py` | 4 (post-pass) | mass spec-vs-RTL soak over `RTL_SOAK_CYCLES` random cycles, seeded from the run-dir name |
| `04_diagnosis.json` | diagnose | fault classification (only on a Stage-4 failure) |

A run **succeeds** when `04_evaluation.json` has `"status": "success"`. A passing run
can still print a loud banner: Agent-1 vector disagreements (`02_vector_check.json`)
and soak divergences (`04_soak.json`) are recorded on `04_evaluation.json` and surfaced
by `main.py`, but never flip the run status. See
[architecture.md](architecture.md#2-the-artifact-chain) for the full contract and which
stage reads/writes each file.

## Tests

```bash
python3.11 -m pytest tests/ -q     # deterministic, free, no LLM / network
python3.11 tests/test_dff.py       # the D flip-flop integration test, standalone
```

See [verification.md](verification.md#the-test-suite) for what the suite covers and how
the live (`agentic_tests/`) tests are gated.

## Custom commands

| Command | What it does |
|---|---|
| `/add-refinement-rule <Name>` | scaffold a new rule, register it, update docs |
| `/validate-artifacts <run_id>` | validate every artifact JSON against its schema |
| `/check-tla <run_id>` | run TLC on the generated TLA+ |
| `/lint-rtl <run_id>` | lint the generated Verilog |
| `/trace-refinement <run_id>` | pretty-print the refinement chain step by step |
