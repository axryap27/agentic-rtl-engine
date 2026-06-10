# rtlcore — native verification core

C++17 mirror of the pipeline's verification hot path, exposed to Python as the
optional module `pipeline.refinement._rtlcore`. It exists for ONE reason: the
obligation kernel runs on **every** `LoopIntroduction` proposal — including the
failed proposals burned during 3-strike backtracking — and the pure-Python
kernel re-parses every expression on every `_eval` call. The native core
compiles each expression once and enumerates in compiled C++:

| workload | python | native | speedup |
|---|---|---|---|
| 6-bit exhaustive proof (4,096 cases) | ~1.4 s | ~7 ms | ~205× |
| 8-bit exhaustive proof (65,536 cases) | ~23 s | ~74 ms | ~311× |
| spec-sim, 20,000-cycle FIFO run | ~1.5 s | ~25 ms | ~59× (~0.8M edges/s) |

(M-series Mac, Release build. Proof rows measured on the multiplier
derivation; today's Stage 4 derivation is ~20 vectors and was never slow — the
spec-sim engine exists to make MASS stimulus cross-checks affordable.)

## What it mirrors — and the contract

Three reference implementations stay authoritative **in Python**:

1. `pipeline.cocotb.spec_sim._eval` — the expression semantics Compiler 2
   emits (U32 wraparound masking, pessimistic X-propagation, div/mod-by-zero →
   X, word-operator fallbacks, the tolerant parser).
2. `pipeline.refinement.obligations.discharge_loop_obligations` — the
   O1/O2/O3 sweeps, domain enumeration order, sampled battery, `mode`,
   `cases_checked`, result envelope, counterexample dicts (byte-identical
   detail strings).
3. `pipeline.cocotb.spec_sim.SpecSimulator`'s **cycle loop** — reset pulse,
   input drive/hold, combinational bounded fixpoint, reset-branch vs
   read-before-write clocked commits, memory writes (X index skipped,
   out-of-range dropped, X-until-written), width-masked commits, X-omitting
   output rows. The COMPOSITION stays in `SpecSimulator.__init__` (it reuses
   the bridge functions that feed Compiler 2) for BOTH backends — the native
   engine replaces only the per-edge loop, fed pre-digested updates.

The native core must MATCH them; it never redefines them. Verdict/row identity
is load-bearing: the refinement audit recorded on the chain and the derived
golden vectors written into Stage-4 artifacts must not depend on which backend
ran, or chain replay / artifact comparison would diverge across machines. The
contract is pinned by `tests/test_native_kernel.py` (a 12,000-case
differential expression fuzz plus full `ObligationResult` equality,
counterexamples included, on the multiplier derivation and every negative
control) and `tests/test_native_specsim.py` (exact row equality on every
fixture design's proven stimulus plus a randomized-stimulus fuzz: X inputs,
hex/string/bool coercion, input holds, mid-stream resets, over-width values,
and a 2,000-cycle FIFO run). For the same reason the exhaustive threshold
default (65,536) is identical in both backends — raising it is a deliberate
committed change, not a build-dependent one.

Documented divergences (outside the pipeline's expression domain, see
`include/rtlcore/expr.hpp`): ASCII-only digits/identifiers, 64-bit saturating
literals, exact integer division for operands ≥ 2^53, lazy IF branches, and
TypeError-kind parity (not message parity) on indexing a bound scalar.

## Build

```bash
python3.11 -m pip install pybind11   # once
./core/build.sh                      # cmake + build + ctest + install module
```

`build.sh` configures CMake against the interpreter (`PYTHON=... ./core/build.sh`
to override), pins `CMAKE_OSX_ARCHITECTURES` to the interpreter's architecture
(an Intel-brew cmake under Rosetta would otherwise emit x86_64), runs the C++
unit tests, and copies `_rtlcore.cpython-*.so` into `pipeline/refinement/`.
The `.so` and `build/` are gitignored — build artifacts, not sources.

## Backend selection

Both dispatchers follow the same pattern — a `backend` parameter
(`"auto"` default | `"python"` | `"cpp"`, explicit `"cpp"` raises if not
built), an env var to force a choice globally, and a reporter:

- obligations: `discharge_loop_obligations(..., backend=)`,
  `OBLIGATIONS_BACKEND`, `kernel_backend()`.
- spec-sim: `derive_expected(..., backend=)`, `SPECSIM_BACKEND`,
  `specsim_backend()`. One domain note: a NEGATIVE stimulus int is outside the
  native unsigned domain — `auto` silently falls back to Python (legacy
  behavior preserved); an explicit `"cpp"` refuses loudly.

Nothing else in the pipeline knows the native core exists. Without the module
(no compiler, fresh clone) everything runs pure-Python with identical results,
just slower; the differential test modules skip themselves.

## Layout

```
core/
  include/rtlcore/expr.hpp         compiled evaluator (Value/X, SymTab, AST)
  include/rtlcore/obligations.hpp  kernel result/params types
  include/rtlcore/spec_sim.hpp     cycle-engine spec/edge/row types
  src/expr.cpp                     tokenizer + parser + evaluator
  src/obligations.cpp              domain enumeration + O1/O2/O3 walker
  src/spec_sim.cpp                 per-edge cycle loop (comb fixpoint, commits)
  bindings/module.cpp              pybind11 boundary (eval_expr, discharge, run_spec_sim)
  tests/test_core.cpp              zero-dependency C++ unit tests (ctest)
  build.sh                         configure + build + test + install
```

## Roadmap

- **Mass stimulus cross-check** — the native cycle engine makes a
  thousands-of-cycles random soak (spec vs RTL) affordable per run
  (~0.8M edges/s); the Stage-4 consumer for it is not wired yet.
- **Z3 symbolic mode** — the same compiled AST translated to Z3 bit-vector
  terms, upgrading `mode` from "exhaustive over the declared widths" to a
  width-generic symbolic proof (and making O2 the full Hoare obligation over
  all invariant-satisfying states, strictly stronger than the reachable-state
  walk). Kept as a separate, honestly-labelled mode; brute-force enumeration
  remains the fallback when the solver returns *unknown* (nonlinear integer
  arithmetic is undecidable in general).
- **Threshold raise** — at native speed a 2^20 domain costs ~1 s; raising the
  default threshold becomes a deliberate, committed, backend-independent change.
