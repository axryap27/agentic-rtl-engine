# Architecture

**Input:** a natural-language description of a digital circuit.
**Output:** a synthesizable Verilog-2001 module, verified against a cocotb testbench.

The pipeline never asks an LLM to write hardware in one shot. It routes the prompt
through a **formal specification** (TLA+) and lowers that spec toward hardware
**one provably-correct refinement rule at a time**. The LLM's job is confined to
interpreting the prompt, authoring/revising the formal spec, and *choosing* which
rule to apply next from a filtered menu — never emitting RTL freehand. That bounded
action space is the project's central defense against hallucination.

---

## 1. Pipeline at a glance

```
        Natural-language prompt  (00_nl_spec.json)
                 │
                 ▼
   Stage 1   Agent 1  ──►  SpecSummary           (01_summary.json)
             prompt → ports + behavior + test vectors        [LLM, proxy]
                 │
        ┌────────┴───────────────────────────────────┐
        ▼                                             ▼
   Stage 3   Agent 3 + deterministic tools       Stage 2   cocotb generator
   summary → FormalSpec (TLA+)                    summary → testbench .py
     → TLC model-check (optional)                 (deterministic, no LLM)
     → Refinement Engine lowers it,               (02_testbench.py,
        one rule at a time                          02_testbench_meta.json)
     → Compiler 2 → Verilog-2001
   (02_formal_spec.json, 03_rtl_output.json,
    output.v, refinement_chain.json)
        │                                             │
        └────────┬────────────────────────────────────┘
                 ▼
   Stage 4   cocotb runner  (Icarus Verilog)     (04_evaluation.json)
             run output.v against the testbench  (deterministic, no LLM)
                 │
        pass ──► done (verified Verilog)
        fail ──► Diagnose ──► classify fault ──► revise spec  OR  backtrack refinement
                 (04_diagnosis.json)                 [LLM, proxy]
```

Three components call an LLM — **Agent 1**, **Agent 3**, and the **Diagnoser**.
Everything else (Stage 2's generator, both compilers, the refinement engine, the
cocotb runner) is deterministic Python.

---

## 2. The artifact chain

Each stage reads its inputs and writes its outputs as JSON files under
`artifacts/<run_id>/`. **LangGraph routes solely on the `status` field of the output
JSON** — never on Python return values or exceptions. Every node must write a
status-bearing artifact before returning, even on failure, or the router has nothing
to act on.

| File | Written by | Read by | `status` values |
|------|-----------|---------|-----------------|
| `00_nl_spec.json` | user / `main.py` | Stage 1 | — (not a stage output) |
| `01_summary.json` | Stage 1 (Agent 1) | Stage 2, Stage 3 | `success`, `error` |
| `02_testbench.py` + `02_testbench_meta.json` | Stage 2 (generator) | Stage 4 | `success`, `error` |
| `02_formal_spec.json` | Stage 3 (Agent 3) | Stage 4, Diagnose | `success`, `error`, `partial` |
| `03_rtl_output.json` (+ `output.v`) | Stage 3 (Compiler 2) | Stage 4, cocotb | `success`, `partial`, `error` |
| `04_evaluation.json` | Stage 4 (cocotb runner) | terminal, Diagnose | `success`, `error` |
| `04_diagnosis.json` | Diagnose node | LangGraph routing | `success`, `error` |
| `refinement_chain.json` | Refinement Engine | debugging, Stage 3 (backtrack) | — (not routed on) |

> The `02_` prefix on `02_formal_spec.json` reflects on-disk ordering, **not** which
> stage produces it — Stage 3 writes it. The 3-agent design produces **no**
> `02_pluscal_impl.json`. Filenames are kept exactly as the code uses them.

A schema backs every artifact (`pipeline/schemas/`); the `ArtifactEnvelope` model
validates the `status` field at write time, so a typo like `"sucess"` raises a
`ValidationError` instead of silently misrouting the run.

---

## 3. The control plane (LangGraph)

`pipeline/graph.py` wires seven nodes. The entry point is `stage1`; terminal is `END`.

| Node | Runner | On exit, routes via |
|---|---|---|
| `stage1` | `run_stage1` (Agent 1) | `_route_after_stage1` |
| `stage2` | `run_stage2` (generator) | unconditional → `stage3` |
| `stage3` | `run_stage3` (Agent 3 + engine + Compiler 2) | `_route_after_stage3` |
| `stage4` | `run_stage4` (cocotb runner) | `_route_after_stage4` |
| `diagnose` | `run_diagnose` (Diagnoser) | `_route_after_diagnose` |
| `stage3_revise_cocotb` | `run_stage3_revise_cocotb` | `_route_after_stage3` |
| `stage3_backtrack_refinement` | `run_stage3_backtrack_refinement` | `_route_after_stage3` |

**Edges and routing logic:**

- `stage1` → `advance`:`stage2` · `retry`:`stage1` · `halt`:`END`
  Retries on a non-`success` summary up to `_MAX_STAGE1_RETRIES = 1`, then halts.
- `stage2` → always `stage3`.
- `stage3` → `advance`:`stage4` · `halt`:`END`
  **Only `success` advances. `partial` halts immediately (G07):** a `partial` RTL
  artifact means the Verilog was built from the *unrefined* spec (the engine was
  unavailable or threw), so it must not reach cocotb where it could vacuously pass.
- `stage4` → `done`:`END` · `diagnose`:`diagnose` · `halt`:`END`
  On a cocotb failure, routes to `diagnose` while
  `retry_counts["stage4_cocotb"] < _MAX_COCOTB_RETRIES (= 2)`, else halts.
- `diagnose` → `revise_spec`:`stage3_revise_cocotb` · `backtrack`:`stage3_backtrack_refinement`
  Forks on `state["last_diagnosis"]`: `"refinement"` → backtrack, otherwise → revise.
- both Stage-3 recovery nodes re-enter `stage4` via `_route_after_stage3`.

**Happy path:** `stage1 → stage2 → stage3 → stage4 → END`.

The thin inter-stage state (`pipeline/state.py`) carries only what routing needs:

```python
class PipelineState(TypedDict):
    run_id: str
    retry_counts: dict[str, int]
    halt: bool
    last_diagnosis: str | None   # "spec" | "refinement" | None
```

---

## 4. Stages in detail

**Stage 1 — interpret the prompt.** [Agent 1](agents.md#agent-1) turns the NL prompt
into a `SpecSummary`: module name, description, typed ports, and golden
`test_vectors`. Written to `01_summary.json`.

**Stage 2 — generate the testbench.** The deterministic
[cocotb generator](verification.md) turns `SpecSummary.test_vectors` into a cocotb
`.py` testbench. No LLM — the test vectors already fully specify the bench. (This was
originally specced as "Agent 2"; the implementation simplified to pure templating.)

**Stage 3 — author and lower the spec.** The heart of the pipeline:

1. [Agent 3](agents.md#agent-3) authors a `FormalSpec` (JSON(TLA)) from the summary.
2. [Compiler 1](compilers.md#compiler-1) emits abstract TLA+; **TLC** optionally
   model-checks it (skipped if TLC is not installed). On a TLC error, Agent 3 revises
   and the compile→check loop repeats (≤ 3 attempts).
3. The [Refinement Engine](refinement.md) lowers the spec to RTL-style, one rule at a
   time, over a fixed schedule of [passes](refinement.md#multi-pass-schedule). Each
   step is logged to `refinement_chain.json`.
4. A one-shot Agent-3 [correctness critic](refinement.md#the-correctness-critic) gates
   the refined spec before codegen.
5. [Compiler 2](compilers.md#compiler-2) emits Verilog-2001 to `output.v`.

**Stage 4 — simulate.** The deterministic [cocotb runner](verification.md#the-runner)
builds `output.v` with Icarus Verilog and runs the Stage-2 testbench, writing a
structured pass/fail report to `04_evaluation.json`.

**Diagnose — route the fix.** On a Stage-4 failure, the
[Diagnoser](agents.md#the-diagnoser) classifies the fault as a **spec** fault (wrong
behavior → Agent 3 revises the FormalSpec) or a **refinement** fault (right behavior,
wrong rule parameters → backtrack the chain and re-pick). A `build`-phase failure is
classified `spec` with **no** LLM call.

---

## 5. Runtime agents vs. deterministic core

| Component | LLM? | Transport | File |
|---|:---:|---|---|
| Agent 1 | ✓ | OpenAI-compatible proxy | `pipeline/agents/agent1.py` |
| Agent 3 | ✓ | Anthropic SDK (direct) | `pipeline/agents/agent3.py` |
| Diagnoser | ✓ | OpenAI-compatible proxy | `pipeline/agents/agent_diagnoser.py` |
| Stage 2 generator | ✗ | — | `pipeline/cocotb/generator.py` |
| Compiler 1 / Compiler 2 | ✗ | — | `pipeline/compilers/` |
| Refinement Engine + rules | ✗ | — | `pipeline/refinement/` |
| cocotb runner | ✗ | — | `pipeline/cocotb/runner.py` |
| LangGraph orchestration | ✗ | — | `pipeline/graph.py` |

The two transports are a deliberate split — see [agents.md](agents.md#two-transports).

---

## 6. The four design invariants

Everything above exists to protect four properties. Treat them as non-negotiable.

1. **Bounded action space.** During refinement the LLM only ever returns a
   `(rule_name, params)` choice from the engine's filtered applicable set — it never
   writes TLA+ or Verilog. `Agent3.pick_rule` is a one-shot structured-output call
   with **no tools and no internal loop**. (Agent 3's spec-authoring calls *are*
   tool-using; the invariant applies to rule selection.)

2. **Deterministic core.** The compilers, the refinement engine and its rules, the
   cocotb generator, and the runner contain no LLM calls and no nondeterminism. Rule
   `apply()` is pure, which is what makes the refinement chain replayable and
   backtracking sound.

3. **Verilog-2001 only.** Compiler 2 emits a strict Verilog-2001 subset (no `logic`,
   `always_ff`, `always_comb`, or `initial` in synthesizable modules). This is
   enforced at codegen by a [banlist verifier](compilers.md#the-banlist), not by
   prompt retries.

4. **Status-routed artifact contract.** Every node writes a `status`-bearing JSON
   artifact before returning, even on failure; LangGraph routes only on that status.
   A validated envelope makes an invalid status a write-time error.

---

## 7. Where to go next

- The agents and their call surface: [agents.md](agents.md)
- The refinement engine, rules, and passes: [refinement.md](refinement.md)
- Both compilers and the bridge: [compilers.md](compilers.md)
- The cocotb generator/runner and tests: [verification.md](verification.md)
- Theory and the refinement-calculus tables: [background.md](background.md)
- Current status and open issues: [status.md](status.md)
