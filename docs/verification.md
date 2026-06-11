# Verification

The verification branch turns the Stage-1 test vectors into a cocotb testbench and runs
it against the generated Verilog. Four pieces, all **deterministic Python** — no LLM:
the Stage-2 testbench generator, the Stage-4 **spec-derived golden-vector pre-flight**
(cocotb runs against expecteds re-derived from the refined spec on Agent 1's input
stimulus, not Agent 1's hand arithmetic), the cocotb runner, and the post-pass
**spec-vs-RTL soak** over thousands of deterministic random cycles.

---

## The testbench generator

**File:** `pipeline/cocotb/generator.py` ·
`generate_testbench(summary: SpecSummary, output_path: Path) -> Path`

A pure template. `SpecSummary.test_vectors` already fully specifies the bench
(inputs to drive, outputs to assert), so no LLM is needed. For each test vector the
generator emits: a comment, one `dut.<port>.value = <v>` per input — except the clock
port, which is deliberately skipped (FIX RC2: the free-running `Clock` owns it, so the
1-tick-per-vector contract holds even if Agent 1 emits `clk` in a vector) — a clock
edge, a settle `Timer`, and one assert per expected output.

Structure of a generated bench:

1. start a clock — `Clock(dut.<clock_port>, 10, unit="ns")` (cocotb 2.x uses singular
   `unit=`). The clock-port name is derived by `_clock_port()`: an exact `clk`/`clock`
   input port, else any input whose name contains `clk`/`clock`, else the default
   `"clk"`;
2. **initialise every test-vector input to 0** before the reset pulse — undriven (`X`)
   inputs at the reset-deassert edge would otherwise poison a self-feeding register
   (e.g. an enable-gated counter latches `X` and never recovers);
3. pulse the reset — `dut.<summary.reset_port>`, polarity from
   `summary.reset_active_low` (skipped entirely when `reset_port` is `None`), with a
   settle edge so the DUT fully exits reset before the first vector;
4. per vector: drive inputs, clock, settle past the delta cycle (a 1 ns `Timer`, so
   registered `always @(posedge clk)` outputs are visible), then assert.

The settle `Timer` is the standard cocotb 2.x idiom: `RisingEdge` wakes the coroutine
*at* the edge delta, before clocked outputs update.

---

## Spec-derived golden vectors (Stage-4 pre-flight)

**Files:** `pipeline/cocotb/vector_check.py` ·
`apply_spec_derived_vectors(artifact_dir)` and `pipeline/cocotb/spec_sim.py` ·
`derive_expected(...)` / `SpecSimulator` — invoked from `pipeline/nodes/stage4.py`
before the runner.

Agent 1 hand-computes the golden vectors from the NL prompt, and on deep sequential
designs its arithmetic is fragile: the live FIFO run `181016` failed a **correct** RTL
because one of 19 vectors miscounted occupancy at the drain-to-empty boundary — a
*false red*. The pre-flight removes that failure class:

1. **Reconstruct the refined spec** by replaying `refinement_chain.json` from the
   `02_formal_spec.json` engine spec (the engine's replay invariant — exactly the spec
   Compiler 2 compiled). No replayable chain → skip.
2. **Simulate it on Agent 1's INPUT stimulus.** `SpecSimulator` is a cycle-accurate
   interpreter matching the generated Verilog + harness semantics: a reset pulse then
   exactly one rising edge per vector, nonblocking read-before-write register commits,
   continuous (fixpoint-settled) combinational outputs, `X` for an unwritten memory
   cell, and a 32-bit-unsigned arithmetic context (underflow wraps like Verilog) with
   per-signal width masking on commit. An `X` output is omitted from the expecteds —
   a cocotb don't-assert.
3. **Regenerate the bench** as `02_testbench_specvec.py` against the spec-derived
   expecteds; cocotb runs that bench, so a correct RTL is never failed by a wrong
   Agent-1 vector (**no false red**).
4. **Surface every disagreement** between Agent 1's expecteds and the spec's in
   `02_vector_check.json` — either an Agent-1 arithmetic slip (a false red avoided) or
   a genuine spec/intent bug, recorded for review, never silently masked (**no silent
   false green**). On a pass with disagreements, Stage 4 copies them onto
   `04_evaluation.json` as `vector_disagreement` + `vector_check_note`, and `main.py`
   prints a loud `PASSED WITH UNRESOLVED AGENT-1/SPEC DISAGREEMENT` banner.

**Degenerate-reference guard:** if any declared output port is never asserted by the
spec-derived reference across all vectors (the spec never drives it, or everything
degrades to `X`), the reference is refused — that port would get zero assertions and
any RTL would pass it silently.

**Fail-soft:** any failure (missing artifact, unevaluable expression, the guard)
returns `None` and Stage 4 runs Agent 1's original testbench unchanged — the
pre-flight never breaks a run.

**Independence is scoped to the expression-evaluation leaf.** The simulator's
recursive-descent evaluator is wholly separate from Compiler 2's `translate_expr` +
iverilog, so agreement cross-validates Compiler 2's translation/emission. It is *not*
independent of the upstream composition: the simulator reuses the bridge's
`_compose_clocked_actions` / `_action_update_exprs` — the same functions that feed
Compiler 2 — so a composition bug would corrupt both identically. Composition
correctness is pinned instead by the fixture-trace tests in `tests/test_spec_sim.py`:
run on each design class's input stimulus, the simulator must reproduce that fixture's
hand-derived, real-cocotb-proven trace exactly.

### The native cycle engine

`derive_expected(..., backend="auto"|"python"|"cpp")` dispatches the per-edge cycle
loop to the optional C++ core (`pipeline.refinement._rtlcore`, built by
`core/build.sh`) when present; the `SPECSIM_BACKEND` env var overrides the choice and
`specsim_backend()` reports what `"auto"` will use. Composition stays in Python
(`SpecSimulator.__init__`) either way, and both backends return **identical rows by
contract** — pinned by `tests/test_native_specsim.py` (every fixture trace, a
randomized-stimulus differential fuzz, and a 2,000-cycle FIFO parity run) — so Stage-4
artifacts are backend-independent. Pure-Python fallback when the core is not built
(`backend="cpp"` raises instead). Measured ~59× on an M-series Mac: a 20,000-cycle
FIFO spec-sim drops 1.47 s → 25 ms (~0.8M edges/s) — what makes the 2000-cycle soak
below nearly free.

---

## The runner

**File:** `pipeline/cocotb/runner.py` ·
`run_testbench(testbench_path, rtl_path, module_name) -> dict`

A deterministic subprocess wrapper around cocotb's Icarus Verilog flow:

1. **Build** — `iverilog -g2001 -o sim_build/<module>.vvp <rtl>` (Verilog-2001 mode).
2. **Test** — `vvp` with cocotb's VPI library loaded
   (`COCOTB_TOPLEVEL`, `COCOTB_TEST_MODULES`, `PYGPI_PYTHON_BIN`,
   `COCOTB_RESULTS_FILE`). `vvp` exits 0 even on test failure, so pass/fail is read
   from the JUnit XML cocotb writes, not the exit code.

Inputs are resolved to **absolute paths** up front: `vvp` runs with `cwd` set to the
testbench directory so cocotb can import the bench module, and a relative `.vvp` path
would otherwise double-resolve against that cwd. This is what lets the runner work when
the caller (the graph) passes relative `artifacts/<run_id>/...` paths.

### Structured failure report

The result is designed for the [Diagnoser](agents.md#the-diagnoser) to consume:

```jsonc
// pass
{"status": "pass"}

// build failure — RTL did not compile (suspect Compiler 2 / codegen)
{"status": "fail", "phase": "build", "error": "<iverilog first line>",
 "raw": "<full stdout+stderr>", "failed_vectors": []}

// test failure — sim ran, assertions failed (suspect spec / refinement)
{"status": "fail", "phase": "test", "error": "<N tests failed>",
 "raw": "<full output>",
 "failed_vectors": [{"test": "...", "error_type": "AssertionError", "error_msg": "..."}]}
```

`phase` is the primary routing key: `build` → suspect codegen; `test` → suspect the
formal model or refinement. (Stage 4 wraps this result into `04_evaluation.json` —
no longer just the bare runner result: a passing evaluation may also carry
`vector_disagreement` / `vector_check_note` and a `soak` block. `status` stays
`success` so LangGraph routing is unchanged; those signals ride the artifact and the
`main.py` banners.)

---

## The spec-vs-RTL soak (Stage-4 post-pass)

**File:** `pipeline/cocotb/soak.py` ·
`run_soak(artifact_dir, verilog_path, module_name)`

The directed bench checks ~20 vectors. The refined spec is an executable model and
(natively) nearly free to simulate, so after the directed bench **passes**, Stage 4
soaks the same RTL against the spec on thousands of random cycles — a divergence here
is a genuine spec-vs-RTL bug the directed vectors missed.

- **Deterministic and replayable from the artifacts alone:** the stimulus seed is
  `crc32` of the run-dir name; `RTL_SOAK_CYCLES` sets the length (default 2000, `0`
  disables; `tests/conftest.py` disables it suite-wide — dedicated soak tests pass
  `n_cycles` explicitly).
- **In-width stimulus:** every free input gets a random value within its declared
  width (the spec sim does not mask inputs while the RTL port would truncate them — an
  over-width value would be a false divergence, not a finding). The reset port is
  never re-driven in-vector (the bench's reset pulse handles reset) and `clk` is
  excluded. Expecteds come from the same `derive_expected`; the bench is regenerated
  as `04_soak_testbench.py`; the same degenerate-reference guard as the pre-flight
  applies.
- **A divergence is a deterministic pipeline bug** — Compiler-2 codegen, composition,
  or simulator-semantics drift, *not* an agent error. A soak failure is therefore
  surfaced loudly (full detail in `04_soak.json`, a `soak` block on
  `04_evaluation.json`, a `SOAK FAILURE` banner in `main.py`) but does **not** flip
  the evaluation status: the design met its directed acceptance bench, and a metered
  Agent-3 revision retry cannot fix codegen. (Routing soak failures to the diagnoser
  is the planned upgrade.)
- **Fail-soft:** any infrastructure problem (no replayable chain, runner unavailable)
  yields `status: "skipped"` with a reason in `04_soak.json` and never breaks Stage 4.
  A cocotb failure is a *result* (`"failed"`), never swallowed. Skipped soaks stay off
  `04_evaluation.json` so the envelope stays minimal.

---

## The test suite

Two trees, with opposite cost profiles.

### `tests/` — deterministic, free, default

No LLM calls and no network: hand-built specs plus a scripted `pick_rule` stub stand in
for the LLM, so the entire mechanical spine (bridge, engine, both compilers, cocotb) is
exercised end to end for free. Run anytime:

```bash
python3.11 -m pytest tests/ -q
```

Headline coverage: the refinement loop converges to RTL on a counter and a D
flip-flop; emitted Verilog is lint-clean and elaborates under Icarus; bit widths
survive and free input ports are declared and correctly sized; the LangGraph routing
table and the status envelope are typo-proof; and the full graph runs **NL → RTL →
cocotb PASS offline** on six designs in `tests/test_end_to_end_offline.py` (the
traffic-light FSM, multi-op ALU, 8-bit accumulator, 8×8 register file, 4-deep FIFO,
and the 8×8 FSMD shift-add multiplier — each with chain-completes / lints-clean /
real-cocotb tests) with every LLM boundary mocked. With the 2-bit counter that makes
**seven design classes** proven offline NL → RTL → real-cocotb PASS. Lint/sim tests
guard on tool availability
(`shutil.which` / `importorskip`) and skip — rather than error — when `iverilog` /
`verilator` / `cocotb` are absent.

The D flip-flop integration test also runs standalone, as documented in `CLAUDE.md`:

```bash
python3.11 tests/test_dff.py
```

See [status.md](status.md) for the current tally and the remaining `xfail`s.

### `agentic_tests/` — live, metered, opt-in

Tests that hit the real models live here. They are **off by default** (gated behind an
explicit opt-in flag and the Agent-3 budget guard) so a normal run never spends money
or touches the network.
