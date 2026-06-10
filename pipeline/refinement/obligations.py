"""
Deterministic obligation kernel for verified loop-introduction refinement.

WHAT THIS IS
------------
This is the part of the refinement engine that makes the "refinement calculus"
REAL rather than a rubber stamp. When an abstract specification statement (e.g. a
multiplier's `product' = a * b`) is to be refined into a concrete clocked loop
(e.g. shift-add), a proposer supplies a candidate derivation:

    - post:      the abstract postcondition the loop must establish
    - invariant: a loop invariant relating the concrete loop variables
    - variant:   a strictly-decreasing measure (loop termination)
    - guard:     the loop-continuation condition
    - init:      {var: expr} establishing the loop variables (must establish inv)
    - body:      {var: expr} one simultaneous loop step (read-before-write)
    - mapping:   {abstract_var: concrete_expr} data refinement at loop exit

This module DISCHARGES the three Morgan/Back iteration-rule proof obligations
against the REAL expression semantics — soundness comes from the CHECK, not from
trusting the proposer:

    O1   pre  =>  inv[init]                              (init establishes inv)
    O2   inv /\ guard  =>  inv[body] /\ variant'< variant  (body maintains inv,
                                                            variant decreases)
    O3   inv /\ ~guard  =>  post                          (exit establishes post)

PURITY / NO LLM
---------------
Pure, deterministic Python. No openai/anthropic/LLM imports, no I/O, no global
mutable state. The evaluator is `pipeline.cocotb.spec_sim._eval` — the EXACT
expression semantics Compiler 2 emits — so a derivation that discharges here is
checked against the same arithmetic the generated Verilog will run.

PROOF vs FALSIFICATION (be honest in `mode`)
--------------------------------------------
The obligations are quantified over the input space (e.g. all (a, b)). When the
product of the input ranges is small enough we enumerate it EXHAUSTIVELY: every
fixed-width input valuation is checked, which — because the loop is finite-state
per input and we walk its reachable states to a fixpoint — is a genuine finite
PROOF for that input space (`mode="exhaustive-proof"`). When the space is too
large we run a SAMPLED battery (edges 0 / max / 1 plus a deterministic pseudo-
random spread); that can only FALSIFY, never prove (`mode="sampled"`).

REACHABLE-STATE LIMITATION (O2)
-------------------------------
O2 ("inv /\ guard => inv[body]") is, in the general calculus, quantified over ALL
states satisfying the invariant. We do not have a symbolic prover here; instead we
walk the REACHABLE loop states for each enumerated input (the states the loop
actually visits from `init`). This is sound for the derivation as executed (the
loop only ever occupies reachable states), but it is NOT a symbolic proof over the
whole invariant-satisfying state set. In exhaustive-proof mode over the full input
domain this is exactly "the loop is correct for every input", which is what we want
for a fixed-width hardware block; it is weaker than a Hoare-logic proof of the body
in isolation. Documented limitation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Reuse the pipeline's REAL evaluator: the exact semantics Compiler 2 emits.
# (spec_sim imports only pipeline.refinement.bridge -> pipeline.schemas; no cycle
# back into the engine or the rules registry, so importing it here is safe.)
from pipeline.cocotb.spec_sim import _eval


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ObligationResult:
    """Outcome of discharging the loop-introduction obligations.

    ok:             True iff O1, O2 and O3 all held over the checked domain.
    mode:           "exhaustive-proof" (every input valuation checked -> a real
                    finite proof over the input space) or "sampled" (edges +
                    pseudo-random battery -> falsification only).
    cases_checked:  number of input valuations checked.
    obligations:    {"O1": bool, "O2": bool, "O3": bool}.
    counterexample: None if ok, else {"obligation", "inputs", "state", "detail"}
                    pinning the first failing input, the offending state, and what
                    broke (e.g. "variant did not decrease").
    """

    ok: bool
    mode: str
    cases_checked: int
    obligations: dict = field(default_factory=dict)
    counterexample: dict | None = None


# ---------------------------------------------------------------------------
# Domain enumeration
# ---------------------------------------------------------------------------

def _input_domain(input_widths: dict, exhaustive_threshold: int):
    """Yield (valuations, mode) for the given fixed-width inputs.

    Each width w contributes the range 0 .. 2**w - 1. If the product of all input
    ranges is <= exhaustive_threshold we enumerate the full cartesian product
    (a real proof over the input space); otherwise we return a sampled battery
    (deterministic: edges 0/1/max per input plus a fixed pseudo-random spread).
    """
    names = sorted(input_widths)  # deterministic order
    sizes = [1 << int(input_widths[n]) for n in names]  # 2**width valuations each

    total = 1
    for s in sizes:
        total *= s

    if total <= exhaustive_threshold:
        # Exhaustive cartesian product -> a finite-state PROOF over the inputs.
        valuations = [dict(zip(names, combo)) for combo in _product(sizes)]
        return valuations, "exhaustive-proof"

    # Sampled battery: edges (0, 1, max) per input as the full cross-product of
    # edges (small, deterministic), plus a deterministic pseudo-random spread.
    edge_sets = []
    for s in sizes:
        mx = s - 1
        edge_sets.append(sorted({0, 1, mx} & set(range(s)) or {0}))
    sampled: list[dict] = [dict(zip(names, combo)) for combo in _product_lists(edge_sets)]

    # Deterministic pseudo-random spread (no `random` import -> reproducible).
    SAMPLES = 256
    for i in range(SAMPLES):
        val = {}
        for j, n in enumerate(names):
            # Distinct LCG-ish mix per input index so values are not correlated.
            val[n] = ((i * (37 + 7 * j) + 11 + 3 * j) * (1 + j)) % sizes[j]
        sampled.append(val)

    # De-duplicate while keeping order deterministic.
    seen = set()
    uniq = []
    for v in sampled:
        key = tuple(v[n] for n in names)
        if key not in seen:
            seen.add(key)
            uniq.append(v)
    return uniq, "sampled"


def _product(sizes):
    """Cartesian product of range(0, size) for each size (no itertools dep needed,
    but kept explicit and deterministic)."""
    return _product_lists([list(range(s)) for s in sizes])


def _product_lists(lists):
    """Cartesian product of a list of value-lists, deterministic order."""
    result = [[]]
    for lst in lists:
        result = [prev + [x] for prev in result for x in lst]
    return [tuple(r) for r in result]


# ---------------------------------------------------------------------------
# Loop execution (read-before-write / nonblocking, like the RTL)
# ---------------------------------------------------------------------------

def _apply_init(inputs: dict, init: dict) -> dict:
    """Establish the loop variables from the inputs via `init` (sequential within
    init is fine: init is the reset/setup expression set, evaluated in order)."""
    env = dict(inputs)
    for var, expr in init.items():
        env[var] = _eval(expr, env)
    return env


def _step_body(env: dict, body: dict) -> dict:
    """One simultaneous (nonblocking) loop step: every RHS reads the PRE-state,
    all commits land together — exactly like nonblocking RTL register updates."""
    nxt = dict(env)
    for var, expr in body.items():
        nxt[var] = _eval(expr, env)  # read-before-write off the pre-state
    return nxt


# ---------------------------------------------------------------------------
# The three obligations
# ---------------------------------------------------------------------------

def _check_O1(valuations, invariant, init):
    """O1: pre => inv[init]. After init, the invariant holds for every input."""
    for inputs in valuations:
        env = _apply_init(inputs, init)
        if _eval(invariant, env) != 1:
            return False, {
                "obligation": "O1",
                "inputs": dict(inputs),
                "state": _portable(env),
                "detail": "invariant does not hold after init",
            }
    return True, None


def _check_O2(valuations, invariant, guard, variant, init, body, max_iters):
    """O2: inv /\ guard => inv[body] /\ variant strictly decreases.

    Walk the reachable loop states for each input: invariant holds at entry, is
    preserved by the body, and the variant strictly decreases while the guard
    holds (termination). See the module docstring's reachable-state limitation.
    """
    for inputs in valuations:
        env = _apply_init(inputs, init)
        if _eval(invariant, env) != 1:
            return False, {
                "obligation": "O2",
                "inputs": dict(inputs),
                "state": _portable(env),
                "detail": "invariant fails at loop entry",
            }
        iters = 0
        while _eval(guard, env) == 1 and iters < max_iters:
            variant_before = _eval(variant, env)
            nxt = _step_body(env, body)
            if _eval(invariant, nxt) != 1:
                return False, {
                    "obligation": "O2",
                    "inputs": dict(inputs),
                    "state": _portable(env),
                    "detail": "body does not maintain the invariant",
                }
            variant_after = _eval(variant, nxt)
            if variant_before is None or variant_after is None or \
                    not (variant_after < variant_before):
                return False, {
                    "obligation": "O2",
                    "inputs": dict(inputs),
                    "state": _portable(env),
                    "detail": (
                        f"variant did not strictly decrease "
                        f"({variant_before} -> {variant_after})"
                    ),
                }
            env = nxt
            iters += 1
        else:
            if iters >= max_iters and _eval(guard, env) == 1:
                # Guard still holds after max_iters -> loop did not terminate.
                return False, {
                    "obligation": "O2",
                    "inputs": dict(inputs),
                    "state": _portable(env),
                    "detail": f"guard still holds after max_iters={max_iters}",
                }
    return True, None


def _check_O3(valuations, post, guard, invariant, init, body, mapping, max_iters):
    """O3: inv /\ ~guard => post. Run to loop exit, bind each abstract variable via
    the data-refinement mapping (e.g. product := acc), then check the post."""
    for inputs in valuations:
        env = _apply_init(inputs, init)
        iters = 0
        while _eval(guard, env) == 1 and iters < max_iters:
            env = _step_body(env, body)
            iters += 1
        # At exit: bind abstract variables via the mapping, then assert post.
        post_env = dict(env)
        for abstract_var, concrete_expr in mapping.items():
            post_env[abstract_var] = _eval(concrete_expr, env)
        if _eval(post, post_env) != 1:
            return False, {
                "obligation": "O3",
                "inputs": dict(inputs),
                "state": _portable(post_env),
                "detail": "postcondition does not hold at loop exit",
            }
    return True, None


def _portable(env: dict) -> dict:
    """A JSON-friendly snapshot of a loop state (drop any non-int/None values)."""
    out = {}
    for k, v in env.items():
        if v is None or isinstance(v, int):
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def discharge_loop_obligations(
    *,
    post: str,
    invariant: str,
    variant: str,
    guard: str,
    init: dict,
    body: dict,
    mapping: dict,
    input_widths: dict,
    exhaustive_threshold: int = 65536,
    max_iters: int = 64,
) -> ObligationResult:
    """Discharge the three loop-introduction obligations for a candidate
    derivation, over the input domain implied by `input_widths`.

    All expression arguments are strings in the engine-spec expression language
    (the same `_eval` grammar Compiler 2 emits). `init`, `body` and `mapping` are
    {name: expr-str} dicts. Returns an ObligationResult; `ok` is True iff all three
    obligations held over every checked input valuation. The result is honest about
    `mode`: "exhaustive-proof" means every fixed-width input valuation was checked
    (a real proof over the input space); "sampled" means only a battery was checked
    (falsification only).
    """
    valuations, mode = _input_domain(input_widths, exhaustive_threshold)

    o1, cex = _check_O1(valuations, invariant, init)
    if not o1:
        return ObligationResult(
            ok=False, mode=mode, cases_checked=len(valuations),
            obligations={"O1": False, "O2": False, "O3": False},
            counterexample=cex,
        )

    o2, cex = _check_O2(
        valuations, invariant, guard, variant, init, body, max_iters
    )
    if not o2:
        return ObligationResult(
            ok=False, mode=mode, cases_checked=len(valuations),
            obligations={"O1": True, "O2": False, "O3": False},
            counterexample=cex,
        )

    o3, cex = _check_O3(
        valuations, post, guard, invariant, init, body, mapping, max_iters
    )
    if not o3:
        return ObligationResult(
            ok=False, mode=mode, cases_checked=len(valuations),
            obligations={"O1": True, "O2": True, "O3": False},
            counterexample=cex,
        )

    return ObligationResult(
        ok=True, mode=mode, cases_checked=len(valuations),
        obligations={"O1": True, "O2": True, "O3": True},
        counterexample=None,
    )
