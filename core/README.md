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

(M-series Mac, Release build. Measured on the multiplier derivation.)

## What it mirrors — and the contract

Two reference implementations stay authoritative **in Python**:

1. `pipeline.cocotb.spec_sim._eval` — the expression semantics Compiler 2
   emits (U32 wraparound masking, pessimistic X-propagation, div/mod-by-zero →
   X, word-operator fallbacks, the tolerant parser).
2. `pipeline.refinement.obligations.discharge_loop_obligations` — the
   O1/O2/O3 sweeps, domain enumeration order, sampled battery, `mode`,
   `cases_checked`, result envelope, counterexample dicts (byte-identical
   detail strings).

The native core must MATCH them; it never redefines them. Verdict identity is
load-bearing: the refinement audit recorded on the chain must not depend on
which backend ran, or chain replay would diverge across machines. The contract
is pinned by `tests/test_native_kernel.py`: a 12,000-case differential
expression fuzz plus full `ObligationResult` equality (counterexamples
included) on the multiplier derivation and every negative control. For the same
reason the exhaustive threshold default (65,536) is identical in both backends —
raising it is a deliberate committed change, not a build-dependent one.

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

`pipeline/refinement/obligations.py` dispatches per call:

- `discharge_loop_obligations(..., backend="auto")` (the default) — native iff
  built, else pure Python. The `OBLIGATIONS_BACKEND` env var (`python` | `cpp` |
  `auto`) can force a choice globally without code changes.
- `backend="python"` / `backend="cpp"` — explicit; `"cpp"` raises if not built.
- `kernel_backend()` reports what `"auto"` resolves to.

Nothing else in the pipeline knows the native core exists. Without the module
(no compiler, fresh clone) everything runs pure-Python with identical results,
just slower; `tests/test_native_kernel.py` skips itself.

## Layout

```
core/
  include/rtlcore/expr.hpp         compiled evaluator (Value/X, SymTab, AST)
  include/rtlcore/obligations.hpp  kernel result/params types
  src/expr.cpp                     tokenizer + parser + evaluator
  src/obligations.cpp              domain enumeration + O1/O2/O3 walker
  bindings/module.cpp              pybind11 boundary (eval_expr, discharge)
  tests/test_core.cpp              zero-dependency C++ unit tests (ctest)
  build.sh                         configure + build + test + install
```

## Roadmap

- **Z3 symbolic mode** — the same compiled AST translated to Z3 bit-vector
  terms, upgrading `mode` from "exhaustive over the declared widths" to a
  width-generic symbolic proof (and making O2 the full Hoare obligation over
  all invariant-satisfying states, strictly stronger than the reachable-state
  walk). Kept as a separate, honestly-labelled mode; brute-force enumeration
  remains the fallback when the solver returns *unknown* (nonlinear integer
  arithmetic is undecidable in general).
- **Threshold raise** — at native speed a 2^20 domain costs ~1 s; raising the
  default threshold becomes a deliberate, committed, backend-independent change.
