# Verification

The verification branch turns the Stage-1 test vectors into a cocotb testbench and runs
it against the generated Verilog. Both halves are **deterministic Python** — no LLM.

---

## The testbench generator

**File:** `pipeline/cocotb/generator.py` ·
`generate_testbench(summary: SpecSummary, output_path: Path) -> Path`

A pure template. `SpecSummary.test_vectors` already fully specifies the bench
(inputs to drive, outputs to assert), so no LLM is needed. For each test vector the
generator emits: a comment, one `dut.<port>.value = <v>` per input, a clock edge, a
settle `Timer`, and one assert per expected output.

Structure of a generated bench:

1. start a clock — `Clock(dut.clk, 10, unit="ns")` (cocotb 2.x uses singular `unit=`);
2. **initialise every test-vector input to 0** before the reset pulse — undriven (`X`)
   inputs at the reset-deassert edge would otherwise poison a self-feeding register
   (e.g. an enable-gated counter latches `X` and never recovers);
3. pulse `reset` (polarity from `summary.reset_active_low`), with a settle edge so the
   DUT fully exits reset before the first vector;
4. per vector: drive inputs, clock, settle past the delta cycle (a 1 ns `Timer`, so
   registered `always @(posedge clk)` outputs are visible), then assert.

The settle `Timer` is the standard cocotb 2.x idiom: `RisingEdge` wakes the coroutine
*at* the edge delta, before clocked outputs update.

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
formal model or refinement. (Stage 4 wraps this result into `04_evaluation.json`.)

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
cocotb PASS offline** on two medium designs (a traffic-light FSM and a multi-op ALU)
with every LLM boundary mocked. Lint/sim tests guard on tool availability
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
