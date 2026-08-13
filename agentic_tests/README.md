# agentic_tests: live LLM test suite

These tests make **real API calls and cost money**. They are kept separate from
`tests/` (which is fully deterministic and free) and are **off by default**.

## What's here now

Scoped, as of 2026-06-03, to the LLM calls and the LLM-driven stage nodes:

| File | Covers | Transport / creds |
|------|--------|-------------------|
| `test_agent1_live.py` | Agent 1: prompt → SpecSummary (validity, ports, vectors, determinism) | proxy (`LLM_*`) |
| `test_agent3_live.py` | Agent 3: all four entry points; `pick_rule` bounded-action-space checks | Anthropic (`ANTHROPIC_API_KEY`) |
| `test_diagnoser_live.py` | Diagnoser: failure classification returns a legal routing signal | proxy (`LLM_*`) |
| `test_stage_nodes_live.py` | Stage 1 & Stage 3 **nodes**: artifact-write contract + envelope validity with live LLMs | proxy and/or Anthropic |

## What's deferred (on purpose)

Until the Agent 3 key is wired and scope is decided:

- **Full-pipeline tests**: `main.py` / `build_graph().invoke(...)` running NL → Verilog → cocotb end to end.
- **Live refinement-engine tests**: the engine driven by the real `pick_rule` to convergence (the central thesis test). The deterministic suite proves convergence with a scripted picker; the live version proves the LLM chooses a converging path.

## The triple gate

An agentic test runs only when **all three** hold:

1. **Marker**: every test here is auto-marked `live_llm`; `pyproject.toml` sets `addopts = -m 'not live_llm'`, so a normal run skips them.
2. **Opt-in**: even if selected, tests skip unless `RUN_LIVE_LLM=1`.
3. **Keys**: even when opted in, each test skips unless required credentials are present and non-placeholder (proxy keys for Agent 1 / diagnoser; `ANTHROPIC_API_KEY` for Agent 3; both for the Stage 3 node).

So `pytest` and `pytest tests/` never spend money or hit the network.

## Running them

```bash
# everything that can run given the keys you have configured:
RUN_LIVE_LLM=1 pytest agentic_tests -m live_llm

# just the Agent 1 (proxy) tests:
RUN_LIVE_LLM=1 pytest agentic_tests/test_agent1_live.py -m live_llm

# just Agent 3 (needs ANTHROPIC_API_KEY):
RUN_LIVE_LLM=1 pytest agentic_tests/test_agent3_live.py -m live_llm
```

Credentials are read from the process environment, or loaded from `.env` if
present (via `python-dotenv` when installed, else a minimal built-in parser).

> **Budget note:** before running the Agent 3 tests at volume, review the
> Agent 3 budget guard (see `docs/agents.md` → *Budget guard*).
> Agent 3 bills against its own Anthropic account with a configured cap. These
> tests are small but not free.

## Assertion philosophy

LLM output isn't byte-deterministic. Tests assert structure and invariants:

- Schema validity, ports/vectors referencing real signals
- `pick_rule` choosing only from the offered set
- Nodes always writing a status-valid artifact

Goal: confirm the agent produced something well-formed and contract-honoring.
