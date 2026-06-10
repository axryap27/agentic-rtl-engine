"""
Differential tests: the native (C++) verification core vs the Python reference.

THE CONTRACT THESE TESTS PIN
----------------------------
The native core (core/ -> pipeline/refinement/_rtlcore*.so) is an EXACT mirror
of two pieces of reference Python:

  1. the expression evaluator `pipeline.cocotb.spec_sim._eval` — the semantics
     Compiler 2 emits and the obligation kernel proves against, and
  2. the obligation kernel `discharge_loop_obligations` — same enumeration
     order, same mode/cases_checked, same verdicts, byte-identical
     counterexamples.

Verdict identity is LOAD-BEARING: the refinement audit recorded on the chain
must not depend on which backend ran, or chain replay would diverge across
machines. The Python implementations remain the reference semantics; the native
core must prove it matches, never the other way around.

The whole module skips when the native module is not built (core/build.sh) —
the pipeline is pure-Python-complete without it.
"""

import random

import pytest

native = pytest.importorskip(
    "pipeline.refinement._rtlcore",
    reason="native core not built (run core/build.sh)",
)

from pipeline.cocotb.spec_sim import _eval  # noqa: E402  (reference evaluator)
from pipeline.refinement.obligations import (  # noqa: E402
    ObligationResult,
    discharge_loop_obligations,
    kernel_backend,
)


# ===========================================================================
# Evaluator parity — quirk list
# ===========================================================================

# Every expression-semantics quirk the mirror contract names, plus the real
# expression shapes the pipeline produces (handshake chains, shift-add body).
_QUIRKS = [
    # (expr, env)
    ("1 + 2", {}),
    ("count - 1", {"count": 0}),                  # U32 wraparound
    ("0 - 1 < 5", {}),                            # wrapped, NOT less-than
    ("-1", {}),
    ("65535 * 65537", {}),                        # masked product
    ("65536 * 65536", {}),                        # 2^32 -> 0
    ("7 / 2", {}),                                # truncating division
    ("5 / 0", {}),                                # div by zero -> X
    ("5 % 0", {}),
    ("7 mod 2", {}),                              # word op
    ("a AND b", {"a": 1, "b": 0}),
    ("a OR b", {"a": 1, "b": 0}),
    ("NOT a", {"a": 0}),
    ("missing + 1", {}),                          # X propagation
    ("missing \\/ TRUE", {}),                     # PESSIMISTIC X (not Verilog)
    ("missing /\\ FALSE", {}),
    ("IF missing THEN 1 ELSE 2", {}),
    ("IF TRUE THEN 1 ELSE missing", {}),          # taken branch only matters
    ("~0", {}),
    ("!5", {}),
    ("5 \\/ 0", {}),                              # nonzero truthiness
    ("1 = 1", {}),
    ("1 /= 1", {}),
    ("2 <= 2", {}),
    ("2 >= 3", {}),
    ("TRUE", {}),
    ("FALSE", {}),
    ("x", {"x": True}),                           # bool env value (int subtype)
    ("x + 1", {"x": True}),
    ("1 + 2 zzz", {}),                            # trailing tokens ignored
    ("a {,} + 1", {"a": 2}),                      # unknown chars skipped
    ("count' + 1", {"count": 4}),                 # prime dropped
    ("(1 = 2 = 3) + 1", {}),                      # tolerant-paren oddity
    ("m[0]", {}),                                 # absent array -> X
    ("m[i]", {"m": [11, None, 33], "i": 2}),      # memory read
    ("m[i]", {"m": [11, None, 33], "i": 1}),      # X cell
    ("m[i]", {"m": [11, None, 33], "i": 9}),      # out of range -> X
    ("m[i]", {"m": [11, None, 33]}),              # X index -> X
    # real pipeline shapes
    (
        "IF (state = 0 OR state = 2) AND start = 1 THEN 1 "
        "ELSE IF state = 1 AND count = 1 THEN 2 "
        "ELSE IF state = 1 THEN 1 "
        "ELSE IF state = 2 THEN 0 ELSE 0",
        {"state": 2, "start": 1, "count": 0},
    ),
    (
        "IF (mplier % 2) = 1 THEN product + mcand ELSE product",
        {"mplier": 3, "product": 10, "mcand": 7},
    ),
    ("product + mplier * mcand = a * b",
     {"product": 6, "mplier": 0, "mcand": 8, "a": 2, "b": 3}),
]


@pytest.mark.parametrize("expr,env", _QUIRKS, ids=[q[0][:40] for q in _QUIRKS])
def test_eval_quirk_parity(expr, env):
    assert native.eval_expr(expr, env) == _eval(expr, env)


def test_truncated_expression_raises_indexerror_in_both():
    with pytest.raises(IndexError):
        _eval("1 +", {})
    with pytest.raises(IndexError):
        native.eval_expr("1 +", {})


def test_scalar_index_raises_typeerror_in_both():
    with pytest.raises(TypeError):
        _eval("s[0]", {"s": 5})
    with pytest.raises(TypeError):
        native.eval_expr("s[0]", {"s": 5})


# ===========================================================================
# Evaluator parity — deterministic differential fuzz
# ===========================================================================

_VARS = ["a", "b", "count", "state", "x7", "ghost"]  # ghost is never bound
_LITS = [0, 1, 2, 3, 5, 7, 8, 255, 256, 65535, 4294967295]
_BIN = ["+", "-", "*", "/", "%", "mod", "/\\", "\\/", "AND", "OR"]
_CMP = ["=", "/=", "<", "<=", ">", ">="]


def _gen_expr(rng: random.Random, depth: int) -> str:
    """Random expression over the engine-spec grammar. Stays inside the mirror
    contract's shared domain: ASCII, literals < 2^32, no scalar indexing."""
    if depth == 0 or rng.random() < 0.25:
        kind = rng.random()
        if kind < 0.45:
            return str(rng.choice(_LITS))
        if kind < 0.85:
            return rng.choice(_VARS)
        return rng.choice(["TRUE", "FALSE"])
    kind = rng.random()
    if kind < 0.15:
        return (
            f"IF {_gen_expr(rng, depth - 1)} THEN {_gen_expr(rng, depth - 1)} "
            f"ELSE {_gen_expr(rng, depth - 1)}"
        )
    if kind < 0.30:
        op = rng.choice(["~", "!", "NOT ", "-"])
        return f"{op}{_gen_expr(rng, depth - 1)}"
    if kind < 0.45:
        return f"({_gen_expr(rng, depth - 1)})"
    if kind < 0.60:
        return (
            f"{_gen_expr(rng, depth - 1)} {rng.choice(_CMP)} "
            f"{_gen_expr(rng, depth - 1)}"
        )
    return (
        f"{_gen_expr(rng, depth - 1)} {rng.choice(_BIN)} "
        f"{_gen_expr(rng, depth - 1)}"
    )


def _gen_envs(rng: random.Random):
    full = {v: rng.randrange(0, 1 << 32) for v in _VARS if v != "ghost"}
    partial = {v: n for v, n in full.items() if rng.random() < 0.6}
    zeros = {v: 0 for v in full}
    maxed = {v: (1 << 32) - 1 for v in full}
    small = {v: rng.randrange(0, 4) for v in full}
    return [full, partial, zeros, maxed, small, {}]


def test_eval_fuzz_parity():
    """2,000 deterministic random expressions x 6 envs each: the native
    evaluator must agree with the Python reference EXACTLY (X = None
    included)."""
    rng = random.Random(0xC0FFEE)
    checked = 0
    for _ in range(2000):
        expr = _gen_expr(rng, rng.randrange(1, 5))
        for env in _gen_envs(rng):
            assert native.eval_expr(expr, env) == _eval(expr, env), (
                f"divergence on {expr!r} with env {env!r}"
            )
            checked += 1
    assert checked == 12000


# ===========================================================================
# Kernel parity — full ObligationResult equality, both backends
# ===========================================================================

def _mul_params(width: int, count: str = "8") -> dict:
    return dict(
        post="product = a * b",
        invariant="product + mplier * mcand = a * b",
        variant="count",
        guard="count > 0",
        init={"product": "0", "mcand": "a", "mplier": "b", "count": count},
        body={
            "product": "IF (mplier % 2) = 1 THEN product + mcand ELSE product",
            "mcand": "mcand * 2",
            "mplier": "mplier / 2",
            "count": "count - 1",
        },
        mapping={"product": "product"},
        input_widths={"a": width, "b": width},
    )


def _both(params: dict) -> tuple[ObligationResult, ObligationResult]:
    py = discharge_loop_obligations(**params, backend="python")
    cpp = discharge_loop_obligations(**params, backend="cpp")
    return py, cpp


def test_kernel_parity_multiplier_exhaustive():
    py, cpp = _both(_mul_params(6))
    assert py == cpp  # dataclass equality: every field, counterexample included
    assert cpp.ok and cpp.mode == "exhaustive-proof" and cpp.cases_checked == 4096


def test_kernel_parity_wrong_invariant():
    params = _mul_params(6)
    params["invariant"] = "product + mplier * mcand = a + b"
    py, cpp = _both(params)
    assert py == cpp
    assert not cpp.ok
    assert cpp.obligations == {"O1": False, "O2": False, "O3": False}
    assert cpp.counterexample["obligation"] == "O1"


def test_kernel_parity_dropped_shift_counterexample():
    params = _mul_params(6)
    params["body"] = dict(params["body"], mcand="mcand")  # dropped shift
    py, cpp = _both(params)
    assert py == cpp  # incl. the exact counterexample dict
    assert not cpp.ok
    assert cpp.obligations == {"O1": True, "O2": False, "O3": False}
    assert cpp.counterexample["detail"] == "body does not maintain the invariant"
    # the counterexample pins the same first failing input + pre-state
    assert py.counterexample == cpp.counterexample


def test_kernel_parity_sampled_mode():
    # 16-bit operands: 2^32 inputs >> threshold -> sampled battery. With only 8
    # iterations the loop CANNOT multiply 16-bit operands; the battery must
    # falsify O3 — identically in both backends (same battery, same first cex).
    py, cpp = _both(_mul_params(16))
    assert py == cpp
    assert not cpp.ok and cpp.mode == "sampled"
    assert cpp.obligations == {"O1": True, "O2": True, "O3": False}
    # ... and with 16 iterations it verifies (sampled can only falsify; this
    # asserts the battery agrees, not a proof).
    py16, cpp16 = _both(_mul_params(16, count="16"))
    assert py16 == cpp16
    assert cpp16.ok and cpp16.mode == "sampled"
    assert cpp16.cases_checked == py16.cases_checked


def test_kernel_parity_termination_failure():
    params = _mul_params(4, count="100")
    params["invariant"] = "TRUE"
    params["post"] = "TRUE"
    py, cpp = _both(params)
    assert py == cpp
    assert not cpp.ok
    assert cpp.counterexample["detail"] == "guard still holds after max_iters=64"


def test_kernel_parity_threshold_and_max_iters_plumb():
    params = _mul_params(6)
    params["exhaustive_threshold"] = 100  # force sampled at 6-bit
    py, cpp = _both(params)
    assert py == cpp
    assert cpp.mode == "sampled"
    params = _mul_params(6)
    params["max_iters"] = 4  # 8-step loop trips the termination guard
    py, cpp = _both(params)
    assert py == cpp
    assert not cpp.ok
    assert cpp.counterexample["detail"] == "guard still holds after max_iters=4"


def test_kernel_parity_empty_inputs():
    params = dict(
        post="x = 8",
        invariant="x + count = 8",
        variant="count",
        guard="count > 0",
        init={"x": "0", "count": "8"},
        body={"x": "x + 1", "count": "count - 1"},
        mapping={"x": "x"},
        input_widths={},
    )
    py, cpp = _both(params)
    assert py == cpp
    assert cpp.ok and cpp.cases_checked == 1


# ===========================================================================
# Backend dispatch
# ===========================================================================

def test_backend_auto_resolves_native_when_built():
    assert kernel_backend() == "cpp"


def test_backend_env_override_forces_python(monkeypatch):
    monkeypatch.setenv("OBLIGATIONS_BACKEND", "python")
    assert kernel_backend() == "python"
    # auto-dispatch honours the override (still returns the same verdicts)
    r = discharge_loop_obligations(**_mul_params(4))
    assert r.ok and r.mode == "exhaustive-proof" and r.cases_checked == 256


def test_backend_param_overrides_are_explicit():
    r_py = discharge_loop_obligations(**_mul_params(4), backend="python")
    r_cpp = discharge_loop_obligations(**_mul_params(4), backend="cpp")
    assert r_py == r_cpp
    with pytest.raises(ValueError):
        discharge_loop_obligations(**_mul_params(4), backend="fortran")


# ===========================================================================
# Speed smoke — the reason the native core exists
# ===========================================================================

def test_native_8bit_exhaustive_is_fast():
    """The 8-bit exhaustive proof (65,536 cases x up to 8 iterations) is the
    live-loop workload — it runs on EVERY LoopIntroduction proposal. Native it
    must be interactive-speed (it measures ~tens of ms; the bound is generous
    for CI noise). The Python reference takes tens of seconds on this input —
    which is the point of the native core — so it is not timed here."""
    import time

    t0 = time.perf_counter()
    r = discharge_loop_obligations(**_mul_params(8), backend="cpp")
    elapsed = time.perf_counter() - t0
    assert r.ok and r.mode == "exhaustive-proof" and r.cases_checked == 65536
    assert elapsed < 10.0, f"native 8-bit exhaustive took {elapsed:.2f}s"
