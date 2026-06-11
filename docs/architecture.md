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
        (abstract spec statement → obligation
         kernel → LoopIntroduction →
         ScheduleHandshakeFSM)
     → Compiler 2 → Verilog-2001
   (02_formal_spec.json, 03_rtl_output.json,
    output.v, refinement_chain.json,
    refinement_decisions.jsonl)
        │                                             │
        └────────┬────────────────────────────────────┘
                 ▼
   Stage 4   pre-flight: spec-derived vectors    (02_vector_check.json,
             (independent spec interpreter)        02_testbench_specvec.py)
             cocotb runner  (Icarus Verilog)     (04_evaluation.json)
             run output.v vs spec-derived bench  (deterministic, no LLM)
             post-pass: spec-vs-RTL random soak  (04_soak.json)
                 │
        pass ──► done (verified Verilog)
        fail ──► Diagnose ──► classify fault ──► revise spec  OR  backtrack refinement
                 (04_diagnosis.json)                 [LLM, proxy]
```

Three components call an LLM — **Agent 1**, **Agent 3**, and the **Diagnoser**.
Everything else (Stage 2's generator, both compilers, the refinement engine and its
obligation kernel, the spec simulator, the vector check, the soak, the cocotb runner)
is deterministic code: pure Python, with an optional exact-mirror C++ native core
(`core/`, pybind11 → `pipeline.refinement._rtlcore`) accelerating the expression
evaluator, the obligation kernel, and the spec-sim cycle loop. Python stays the
reference semantics and the fallback when the module is not built; both backends
return identical verdicts and rows by contract, so which backend ran is invisible in
the artifacts and replay is backend-independent.

---

## 2. The artifact chain

Each stage reads its inputs and writes its outputs as JSON files under
`artifacts/<run_id>/`, where `run_id` is a date-stamped relative path —
`<YYYY-MM-DD>/<HHMMSS>-<module>-<hash>/` (the module name is spliced in after the
run; `artifacts/latest` always points at the most recent run — see
`pipeline/run_dirs.py`). **LangGraph routes solely on the `status` field of the output
JSON** — never on Python return values or exceptions. Every node must write a
status-bearing artifact before returning, even on failure, or the router has nothing
to act on.

| File | Written by | Read by | `status` values |
|------|-----------|---------|-----------------|
| `00_nl_spec.json` | user / `main.py` | Stage 1 | — (not a stage output) |
| `01_summary.json` | Stage 1 (Agent 1) | Stage 2, Stage 3, Stage 4 (vector check + soak) | `success`, `error` |
| `02_testbench.py` + `02_testbench_meta.json` | Stage 2 (generator) | Stage 4 | `success`, `error` |
| `02_formal_spec.json` | Stage 3 (Agent 3) | Stage 4, Diagnose | `success`, `error`, `partial` |
| `02_vector_check.json` + `02_testbench_specvec.py` | Stage 4 (vector-check pre-flight) | Stage 4 (cocotb runs the specvec bench), review | — (not routed on) |
| `03_rtl_output.json` (+ `output.v`) | Stage 3 (Compiler 2) | Stage 4, cocotb | `success`, `partial`, `error` |
| `04_evaluation.json` | Stage 4 (cocotb runner) | terminal, Diagnose | `success`, `error` |
| `04_soak.json` + `04_soak_testbench.py` | Stage 4 (soak post-pass) | review / debugging | — (not routed on) |
| `04_diagnosis.json` | Diagnose node | LangGraph routing | `success`, `error` |
| `refinement_chain.json` | Refinement Engine | debugging, Stage 3 (backtrack), Stage 4 (vector check + soak replay it to reconstruct the refined spec) | — (not routed on) |
| `refinement_decisions.jsonl` | Stage 3 (`pick_rule` log) | debugging | — (not routed on) |

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
   For sequential arithmetic functions its prompt steers it to author an *abstract*
   spec statement (a transition with `spec_statement: true` plus a postcondition,
   e.g. `product' = a * b`) rather than hand-writing the datapath.
2. [Compiler 1](compilers.md#compiler-1) emits abstract TLA+; **TLC** optionally
   model-checks it (skipped if TLC is not installed). On a TLC error, Agent 3 revises
   and the compile→check loop repeats (≤ 3 attempts).
3. The [Refinement Engine](refinement.md) lowers the spec to RTL-style, one rule at a
   time, in a single [catch-all pass](refinement.md#refinement-driver-the-catch-all-pass)
   (all eight rules: six Tier-1 plus the two verified-derivation rules below). Each
   step is logged to `refinement_chain.json`; each `pick_rule` decision is appended
   to `refinement_decisions.jsonl`.
4. A one-shot Agent-3 [correctness critic](refinement.md#the-correctness-critic) gates
   the refined spec before codegen.
5. [Compiler 2](compilers.md#compiler-2) emits Verilog-2001 to `output.v`.

**The verified-derivation path.** When the spec carries an abstract spec statement,
the bridge keeps the target variables abstract and `LoopIntroduction` refines the
statement into a concrete clocked loop **only after** the deterministic obligation
kernel (`pipeline/refinement/obligations.py`) discharges the Morgan/Back iteration
obligations against the real expression semantics — O1 `pre ⇒ inv[init]`, O2
invariant preservation plus strict variant decrease, O3 `inv ∧ ¬guard ⇒ post`. The
kernel is honest about proof strength: `mode="exhaustive-proof"` when the input space
is ≤ 65536 valuations (a real finite proof over the declared widths), else
`mode="sampled"` (falsification only); a failure yields a concrete counterexample,
and failed obligations make `apply()` a pure no-op — the engine's strike/backtrack
signal. The discharged audit (invariant, variant, guard, mode, cases_checked,
obligations) is recorded on the chain as a certificate. `ScheduleHandshakeFSM`
(deterministic, no proof — the soundness lives in LoopIntroduction) then schedules
the verified loop onto a hardened IDLE/BUSY/DONE start/done FSMD: a
back-to-back-safe load (start accepted in IDLE *or* DONE), body conditionals
flattened into flat else-if chains, combinational `done`. This path is what makes
the intro's "one provably-correct refinement rule at a time" literally true for
derivations.

**Stage 4 — simulate.** Deterministic, in two phases around the simulation.
**Pre-flight:** the spec-derived golden-vector cross-check
(`pipeline/cocotb/vector_check.py`) replays `refinement_chain.json` to reconstruct
the refined spec, re-derives the expected outputs from Agent 1's *input* stimulus via
an independent spec interpreter (`pipeline/cocotb/spec_sim.py`), and writes a
corrected bench (`02_testbench_specvec.py`) plus a disagreement report
(`02_vector_check.json`) — so a correct RTL is never failed by a wrong Agent-1 vector
(no false reds), and any Agent-1/spec disagreement is surfaced on
`04_evaluation.json` and the `main.py` banner instead of silently shipping green. The
[cocotb runner](verification.md#the-runner) then builds `output.v` with Icarus
Verilog and runs the spec-derived bench (the Stage-2 bench is the fail-soft
fallback), writing a structured pass/fail report to `04_evaluation.json`.
**Post-pass:** on a pass, the mass spec-vs-RTL soak (`pipeline/cocotb/soak.py`)
cross-checks the RTL against the refined spec over `RTL_SOAK_CYCLES` deterministic
random cycles (default 2000, `0` disables) — in-width stimulus, reset never re-driven
in-vector, seed = crc32 of the run-dir name so the soak replays from the artifacts
alone. A soak divergence is a deterministic codegen/composition bug: surfaced loudly
(`04_soak.json`, a `soak` block on `04_evaluation.json`, the `main.py` banner) but it
never flips `status`, because a metered Agent-3 revision cannot fix codegen
(diagnoser routing for soak divergences is planned). Both phases are fail-soft —
they skip rather than break a Stage-4 run.

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
| Refinement Engine + rules + obligation kernel | ✗ | — | `pipeline/refinement/` |
| cocotb runner | ✗ | — | `pipeline/cocotb/runner.py` |
| Spec simulator + vector check + soak | ✗ | — | `pipeline/cocotb/{spec_sim,vector_check,soak}.py` |
| Native core (optional C++17 exact mirror) | ✗ | — | `core/` → `pipeline.refinement._rtlcore` |
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
   obligation kernel, the spec simulator, the cocotb generator, and the runner
   contain no LLM calls and no nondeterminism. Rule `apply()` is pure, which is what
   makes the refinement chain replayable and backtracking sound. The optional native
   core preserves this: both backends return identical verdicts and rows by contract
   (same enumeration order, same `mode`/`cases_checked`, byte-identical
   counterexamples, the identical 65536 exhaustive threshold; dispatch via
   `OBLIGATIONS_BACKEND` / `SPECSIM_BACKEND` or `backend=` params), so replay is
   backend-independent.

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
