"""
Differential tests: the native spec-sim cycle engine vs the Python reference.

THE CONTRACT THESE TESTS PIN
----------------------------
derive_expected(backend="cpp") must return EXACTLY the rows of
derive_expected(backend="python") — same dicts, same omitted-X holes — for any
engine spec and stimulus. The composition (SpecSimulator.__init__, reusing the
bridge's compose functions) is SHARED between the backends; only the per-edge
loop differs. Row identity is load-bearing: Stage 4 writes the derived vectors
into 02_vector_check.json / 04_evaluation.json, so the artifacts must not
depend on which backend ran.

Coverage: every fixture design's real stimulus (FSM, ALU, accumulator,
register file w/ memory, FIFO w/ comb flags, both multipliers incl. the
verified-derivation one), plus a randomized-stimulus differential fuzz that
exercises X inputs, hex/string/bool coercion, input holds (missing keys),
mid-stream resets, and over-width input values.

The whole module skips when the native module is not built (core/build.sh).
"""

import random

import pytest

native = pytest.importorskip(
    "pipeline.refinement._rtlcore",
    reason="native core not built (run core/build.sh)",
)

from pipeline.refinement.bridge import formal_spec_to_engine_spec  # noqa: E402
from pipeline.refinement.engine import _replay_chain  # noqa: E402
from pipeline.cocotb.spec_sim import derive_expected, specsim_backend  # noqa: E402
from tests.fixtures.medium_designs import MEDIUM_DESIGNS  # noqa: E402


def _design(name):
    """(refined engine spec, summary) for a fixture design."""
    d = MEDIUM_DESIGNS[name]
    summary = d["summary"]()
    chain = [{"rule_name": n, "params": p} for n, p in d["picker_sequence"]()]
    refined = _replay_chain(formal_spec_to_engine_spec(d["formal_spec"]()), chain)
    return refined, summary


def _both(refined, stim, outs, summary):
    kw = dict(
        reset_port=summary.reset_port or "reset",
        reset_active_low=bool(summary.reset_active_low),
    )
    py = derive_expected(refined, stim, outs, **kw, backend="python")
    cpp = derive_expected(refined, stim, outs, **kw, backend="cpp")
    return py, cpp


# ===========================================================================
# Every fixture design, on its real (cocotb-proven) stimulus
# ===========================================================================

@pytest.mark.parametrize("name", list(MEDIUM_DESIGNS))
def test_fixture_stimulus_row_parity(name):
    refined, summary = _design(name)
    stim = [tv.inputs for tv in summary.test_vectors]
    outs = [p.name for p in summary.ports if p.direction == "output"]
    py, cpp = _both(refined, stim, outs, summary)
    assert py == cpp, f"{name}: native rows diverge from Python"
    # ... and both equal the fixture's proven trace (redundant with
    # test_spec_sim, kept as the anchor for THIS backend).
    assert cpp == [dict(tv.expected) for tv in summary.test_vectors]


# ===========================================================================
# Randomized-stimulus differential fuzz
# ===========================================================================

def _fuzz_value(rng: random.Random, width: int):
    """A stimulus value in the generator's accepted shapes: ints (sometimes
    over-width — inputs are NOT masked), bools, decimal/hex strings, and the
    4-state/don't-care strings that coerce to X."""
    r = rng.random()
    hi = (1 << width) - 1 if width else 1
    if r < 0.55:
        return rng.randrange(0, hi + 1)
    if r < 0.65:
        return rng.randrange(0, 4 * (hi + 1))  # over-width: held unmasked
    if r < 0.72:
        return bool(rng.randrange(2))
    if r < 0.80:
        return str(rng.randrange(0, hi + 1))
    if r < 0.88:
        return hex(rng.randrange(0, hi + 1))
    if r < 0.95:
        return "x"  # don't-care -> X
    return "1z"     # unparseable 4-state literal -> X


def _fuzz_stimulus(rng: random.Random, summary, n_vectors: int) -> list[dict]:
    in_ports = [
        p for p in summary.ports
        if p.direction == "input" and p.name not in ("clk",)
    ]
    reset_port = summary.reset_port or "reset"
    asserted = 0 if summary.reset_active_low else 1
    deasserted = 1 - asserted
    stim = []
    for _ in range(n_vectors):
        vec = {}
        for p in in_ports:
            if p.name == reset_port:
                continue
            if rng.random() < 0.15:
                continue  # missing key: input HOLDS its previous value
            vec[p.name] = _fuzz_value(rng, int(p.width or 1))
        # occasional mid-stream reset (and its string/bool spellings)
        r = rng.random()
        if r < 0.08:
            vec[reset_port] = rng.choice([asserted, str(asserted), bool(asserted)])
        elif r < 0.20:
            vec[reset_port] = rng.choice([deasserted, str(deasserted)])
        stim.append(vec)
    return stim


@pytest.mark.parametrize(
    "name", ["traffic_light", "alu", "accumulator", "register_file", "fifo",
             "multiplier"]
)
def test_random_stimulus_row_parity(name):
    """150 randomized vectors per design x exact row equality. Exercises X
    propagation through state, memory writes with fuzzed addresses, comb
    fixpoints under X, coercion shapes, holds, and mid-stream resets."""
    refined, summary = _design(name)
    outs = [p.name for p in summary.ports if p.direction == "output"]
    rng = random.Random(0xD1FF + len(name))
    stim = _fuzz_stimulus(rng, summary, 150)
    py, cpp = _both(refined, stim, outs, summary)
    assert py == cpp, f"{name}: native rows diverge under fuzzed stimulus"


def test_long_run_parity_fifo():
    """A 2,000-cycle FIFO run (memory + comb flags + back-pressure churn):
    exact row equality at a length where the Python loop is already slow —
    the scale the native engine exists for."""
    refined, summary = _design("fifo")
    outs = [p.name for p in summary.ports if p.direction == "output"]
    rng = random.Random(0xF1F0)
    stim = _fuzz_stimulus(rng, summary, 2000)
    py, cpp = _both(refined, stim, outs, summary)
    assert py == cpp


def test_native_long_run_is_fast():
    """50,000 cycles natively in interactive time — the mass-cross-check
    capability (the Python reference is ~minutes at this length and is not
    timed here)."""
    import time

    refined, summary = _design("fifo")
    outs = [p.name for p in summary.ports if p.direction == "output"]
    rng = random.Random(0xBEEF)
    stim = _fuzz_stimulus(rng, summary, 50_000)
    t0 = time.perf_counter()
    rows = derive_expected(
        refined, stim, outs,
        reset_port=summary.reset_port or "reset",
        reset_active_low=bool(summary.reset_active_low),
        backend="cpp",
    )
    elapsed = time.perf_counter() - t0
    assert len(rows) == 50_000
    assert elapsed < 10.0, f"native 50k-cycle run took {elapsed:.2f}s"


# ===========================================================================
# Backend dispatch
# ===========================================================================

def test_backend_auto_resolves_native_when_built():
    assert specsim_backend() == "cpp"


def test_backend_env_override_forces_python(monkeypatch):
    monkeypatch.setenv("SPECSIM_BACKEND", "python")
    assert specsim_backend() == "python"
    refined, summary = _design("accumulator")
    stim = [tv.inputs for tv in summary.test_vectors]
    rows = derive_expected(refined, stim, ["acc"],
                           reset_port=summary.reset_port or "reset",
                           reset_active_low=bool(summary.reset_active_low))
    assert rows == [dict(tv.expected) for tv in summary.test_vectors]


def test_backend_unknown_raises():
    refined, summary = _design("accumulator")
    with pytest.raises(ValueError):
        derive_expected(refined, [], ["acc"], backend="fortran")


def test_negative_stimulus_falls_back_to_python():
    """A negative int is outside the native unsigned domain: auto silently
    falls back to the Python path (legacy behavior preserved); an EXPLICIT
    cpp request refuses loudly instead of silently switching."""
    refined, summary = _design("accumulator")
    stim = [{"en": 1, "din": -1}, {"en": 1, "din": 2}]
    kw = dict(reset_port=summary.reset_port or "reset",
              reset_active_low=bool(summary.reset_active_low))
    auto_rows = derive_expected(refined, stim, ["acc"], **kw)  # auto
    py_rows = derive_expected(refined, stim, ["acc"], **kw, backend="python")
    assert auto_rows == py_rows
    with pytest.raises(ValueError):
        derive_expected(refined, stim, ["acc"], **kw, backend="cpp")
