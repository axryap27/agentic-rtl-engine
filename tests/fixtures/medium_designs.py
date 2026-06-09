"""
Hand-built MEDIUM-complexity design fixtures for the deterministic test suite.

WHY THIS FILE EXISTS (G02)
--------------------------
Before this file, every fixture in the suite was a 2-bit counter, a D flip-flop,
or a 1-bit toggle — designs the project goal explicitly excludes as too easy. A
green suite over those fixtures proves only counter/DFF capability; it cannot
catch a pipeline that silently fails on the medium tier the goal actually
targets (FSM+datapath, multi-op ALU, FIFO/UART/arbiter). These fixtures are the
medium tier: each is genuinely multi-state / multi-branch / multi-variable.

WHAT'S HERE
-----------
Two MEDIUM designs, each provided in two forms so tests can drive either the
full LLM-mocked LangGraph (FormalSpec form) or the bare bridge→engine→Compiler-2
path (engine-spec / picker / cocotb-vector helpers):

  1. traffic_light  — a 3-state FSM (GREEN/YELLOW/RED) with a countdown-timer
                      datapath. SELF-CONTAINED: no free inputs; next-state and
                      next-timer depend only on the two registers. This makes it
                      robust under the cocotb generator's reset sequence (see the
                      "RESET-DEASSERT EDGE" note below) and is the project's first
                      end-to-end-green medium design.

  2. alu           — a multi-op ALU (ADD/SUB/AND/OR selected by a 2-bit `op`)
                      with a `zero` flag. Has multi-bit FREE INPUTS (`op`, `a`,
                      `b`). Genuinely medium (4-way datapath mux + flag), and it
                      surfaces a real pipeline limitation (free-input width
                      truncation, see tests/test_end_to_end_offline.py).

ENGINE-SPEC SHAPE (confirmed against pipeline/refinement/rules/base.py)
-----------------------------------------------------------------------
    {
      "variables": [{"name","type","width","abstract","reset_value","clocked"}],
      "actions":   [{"name","guard","clocked","is_rtl_style",
                     "updates":[{"variable","expression"}]
                     | "branches":[{"guard","updates":[...]}]            (Alternation)
                     | "sequential_steps":[{"name","guard","updates":[...]}] (SeqComp)
                    }],
      "reset_action": str | None,
      "init":         str,
      "invariants":   [str],
      "abstraction_mapping": {str: str},
      "properties":   [str],
    }

The FormalSpec form (pipeline/schemas/tla_schema.py) is the LLM-facing schema
that Agent 3 returns; the bridge (formal_spec_to_engine_spec) converts it to the
engine-spec above. We give FormalSpec fixtures because the realistic pipeline
path is: Agent 3 returns a FormalSpec whose transition `updates` already carry
the concrete next-state logic, and the refinement engine only adds reset
(Initialization) and clocking (Iteration). See the EXPRESSION DIALECT note.

EXPRESSION DIALECT — TLA+ OPERATORS, NOT WORD OPERATORS  (load-bearing!)
-----------------------------------------------------------------------
On the bridge → Compiler 2 path, expressions are fed to Compiler 2's
translate_expr WITHOUT going through Compiler 1. Compiler 2 translates the
TLA+-form boolean operators (`/\` -> `&&`, `\/` -> `||`) but does NOT translate
the English word operators (`AND`/`OR`/`NOT`) — those leak verbatim and produce
invalid Verilog. So every guard/expression in these fixtures uses `/\` and `\/`,
NOT `AND`/`OR`. (This is the opposite of the FormalSpec → Compiler 1 path, where
AND/OR/NOT is correct. The schema docstring's "AND/OR/NOT" advice applies only
to the Compiler-1 branch.)

NESTED CONDITIONALS — ELSE-IF CHAINS ONLY  (load-bearing!)
----------------------------------------------------------
Compiler 2's IF-THEN-ELSE splitter recurses into the ELSE branch but NOT into
the THEN branch. So `IF g1 THEN e1 ELSE IF g2 THEN e2 ELSE e3` (a chain nested
in the ELSE) translates into nested ternaries correctly, but
`IF g1 THEN (IF g2 THEN x ELSE y) ELSE z` (a conditional nested in the THEN)
leaks the inner IF/THEN/ELSE untranslated. Both fixtures therefore express
multi-way next-state as a flat ELSE-IF CHAIN with compound `/\` guards, never as
a conditional nested inside a THEN.

RESET-DEASSERT EDGE — cocotb expected-vector offset  (load-bearing!)
--------------------------------------------------------------------
pipeline/cocotb/generator.py's reset block issues TWO clock edges before the
first test vector: one with reset asserted (registers take their reset value)
and one with reset de-asserted (registers take ONE next-state step). So the
state observed at test-vector 0 is the next-state AFTER that extra de-assert
edge, not the reset value. The cocotb_vectors() helpers below already account
for this one-cycle offset. A design whose next-state reads a FREE INPUT will see
that input as X on the de-assert edge (inputs aren't driven yet) — which is why
traffic_light (no free inputs) is the clean cocotb fixture and alu is not.
"""

from __future__ import annotations

from pipeline.schemas.summary_schema import SpecSummary
from pipeline.schemas.tla_schema import FormalSpec


# ===========================================================================
# Fixture 1 — Traffic-light FSM with a countdown-timer datapath
# ===========================================================================
#
# Three states encoded in a 2-bit register:
#   state 0 = GREEN  (held for 3 cycles)
#   state 1 = YELLOW (held for 1 cycle)
#   state 2 = RED    (held for 2 cycles)
#
# A 2-bit `timer` counts down within a state. When timer reaches 0 the FSM
# advances GREEN -> YELLOW -> RED -> GREEN and reloads `timer` with the new
# state's (duration - 1). This is a real FSM + datapath: two registers whose
# next values are interdependent multi-branch expressions. No external inputs.
#
# Next-state (flat ELSE-IF chain, TLA+ operators):
_TRAFFIC_STATE_NEXT = (
    r"IF timer > 0 THEN state "
    r"ELSE IF state = 0 THEN 1 "
    r"ELSE IF state = 1 THEN 2 "
    r"ELSE 0"
)
# Next-timer: count down while running; on transition load the entered state's
# (duration - 1):  entering YELLOW -> 0, entering RED -> 1, entering GREEN -> 2.
_TRAFFIC_TIMER_NEXT = (
    r"IF timer > 0 THEN timer - 1 "
    r"ELSE IF state = 0 THEN 0 "
    r"ELSE IF state = 1 THEN 1 "
    r"ELSE 2"
)


def traffic_light_formal_spec() -> FormalSpec:
    """FormalSpec for the traffic-light FSM (the LLM-facing form, no free inputs).

    The transition's `updates` already carry the concrete next-state logic, as a
    realistic Agent-3 output would: refinement only needs to add reset
    (Initialization) and clocking (Iteration).
    """
    return FormalSpec(
        module_name="traffic_light",
        description=(
            "Traffic-light controller FSM. A 2-bit state register cycles "
            "GREEN(0)->YELLOW(1)->RED(2)->GREEN with per-state durations of "
            "3/1/2 cycles, sequenced by a 2-bit countdown timer. Synchronous "
            "reset returns to GREEN with the timer reloaded."
        ),
        variables={
            "state": {"type": "Nat", "width": 2},
            "timer": {"type": "Nat", "width": 2},
        },
        initial={"state": "0", "timer": "2"},
        transitions=[
            {
                "label": "Tick",
                "condition": "TRUE",
                "updates": {
                    "state": _TRAFFIC_STATE_NEXT,
                    "timer": _TRAFFIC_TIMER_NEXT,
                },
            },
        ],
        invariants=["state \\in 0..2", "timer \\in 0..2"],
    )


def traffic_light_summary() -> SpecSummary:
    """SpecSummary (Stage-1 / Agent-1 output form) for the traffic-light FSM.

    The cocotb test vectors are pre-offset for the reset-deassert edge (see the
    module docstring). They drive no inputs (the design is self-contained) and
    assert both registers over eight cycles, walking a full GREEN->YELLOW->RED
    period and into the next one.
    """
    return SpecSummary(
        module_name="traffic_light",
        description="Traffic-light FSM with countdown timer; sync reset to GREEN.",
        ports=[
            {"name": "clk", "direction": "input", "width": 1},
            {"name": "reset", "direction": "input", "width": 1},
            {"name": "state", "direction": "output", "width": 2},
            {"name": "timer", "direction": "output", "width": 2},
        ],
        test_vectors=[
            {"inputs": {}, "expected": {"state": s, "timer": t}}
            for s, t in traffic_light_cocotb_trace()
        ],
        reset_port="reset",
        reset_active_low=False,
    )


def traffic_light_picker_sequence() -> list[tuple[str, dict]]:
    """Applicability-driven rule sequence that drives the FSM to RTL-style.

    Returns (rule_name, params) pairs. Use with an idempotent picker (pick the
    first entry whose rule is in the current applicable set) — NOT a monotonic
    counter; stage3 runs several engine passes that share one picker.
    Initialization fires once (adds reset + reset_values), then Iteration once
    (marks the Tick action clocked). The concrete `updates` already exist on the
    transition, so neither Assignment nor any branch rule is needed.
    """
    return [
        (
            "Initialization",
            {
                "reset_values": {"state": "0", "timer": "2"},
                "reset_action_name": "Reset",
            },
        ),
        ("Iteration", {"action_name": "Tick"}),
    ]


def _traffic_next(state: int, timer: int) -> tuple[int, int]:
    """Reference model of the traffic-light next-state (matches the RTL)."""
    if timer > 0:
        ns = state
    elif state == 0:
        ns = 1
    elif state == 1:
        ns = 2
    else:
        ns = 0
    if timer > 0:
        nt = timer - 1
    elif state == 0:
        nt = 0
    elif state == 1:
        nt = 1
    else:
        nt = 2
    return ns, nt


def traffic_light_cocotb_trace(cycles: int = 8) -> list[tuple[int, int]]:
    """(state, timer) observed at each cocotb test vector, reset-offset applied.

    The generator clocks one reset-deassert edge before vector 0, so vector i
    observes the (i + 1)-th next-state step from (state=0, timer=2).
    """
    state, timer = 0, 2
    state, timer = _traffic_next(state, timer)  # reset-deassert edge
    out: list[tuple[int, int]] = []
    for _ in range(cycles):
        state, timer = _traffic_next(state, timer)
        out.append((state, timer))
    return out


# ===========================================================================
# Fixture 2 — Multi-op ALU with a zero flag
# ===========================================================================
#
# A registered ALU: on each clock edge `result` is updated to an op-selected
# function of the operands, and `zero` is set when an ADD produces 0. Four ops
# selected by a 2-bit `op` input:
#   op 0 = ADD (a + b)   op 1 = SUB (a - b)   op 2 = AND (a /\ b)   op 3 = OR (a \/ b)
#
# `op`, `a`, `b` are FREE INPUTS (referenced in the update but not declared
# variables). This is genuinely medium — a 4-way datapath mux plus a flag — and
# it deliberately carries a multi-bit free input (`op` needs 2 bits), which the
# pipeline's free-input width inference does not yet handle (see the e2e test).
#
# Next-result (flat ELSE-IF chain, TLA+ operators):
_ALU_RESULT_NEXT = (
    r"IF op = 0 THEN a + b "
    r"ELSE IF op = 1 THEN a - b "
    r"ELSE IF op = 2 THEN a /\ b "
    r"ELSE a \/ b"
)
# zero flag: set on an ADD whose sum is 0.
_ALU_ZERO_NEXT = r"IF op = 0 /\ a + b = 0 THEN 1 ELSE 0"


def alu_formal_spec() -> FormalSpec:
    """FormalSpec for the multi-op ALU (the LLM-facing form, with free inputs)."""
    return FormalSpec(
        module_name="alu",
        description=(
            "Multi-operation ALU. A 2-bit `op` selects ADD/SUB/AND/OR over "
            "operands `a` and `b`, registering the result into a 4-bit `result` "
            "register and setting a `zero` flag when an ADD produces 0. "
            "Synchronous reset clears result to 0 and sets zero high."
        ),
        variables={
            "result": {"type": "Nat", "width": 4},
            "zero": {"type": "Bit", "width": 1},
        },
        initial={"result": "0", "zero": "1"},
        transitions=[
            {
                "label": "Compute",
                "condition": "TRUE",
                "updates": {
                    "result": _ALU_RESULT_NEXT,
                    "zero": _ALU_ZERO_NEXT,
                },
            },
        ],
        invariants=["result \\in 0..15"],
    )


def alu_summary() -> SpecSummary:
    """SpecSummary (Stage-1 form) for the multi-op ALU.

    `op` is declared 2-bit here (the design's truth), so the generated cocotb
    drives op in 0..3. The cocotb vectors are reset-offset; the e2e test
    documents why the *raw* pipeline RTL cannot pass them (free-input width
    truncation makes Compiler 2 size `op` as 1 bit).
    """
    return SpecSummary(
        module_name="alu",
        description="Multi-op ALU (ADD/SUB/AND/OR) with a zero flag; sync reset.",
        ports=[
            {"name": "clk", "direction": "input", "width": 1},
            {"name": "reset", "direction": "input", "width": 1},
            {"name": "op", "direction": "input", "width": 2},
            {"name": "a", "direction": "input", "width": 1},
            {"name": "b", "direction": "input", "width": 1},
            {"name": "result", "direction": "output", "width": 4},
            {"name": "zero", "direction": "output", "width": 1},
        ],
        test_vectors=[
            {
                "inputs": {"op": op, "a": a, "b": b},
                "expected": {"result": r, "zero": z},
            }
            for (op, a, b), (r, z) in alu_cocotb_trace()
        ],
        reset_port="reset",
        reset_active_low=False,
    )


def alu_picker_sequence() -> list[tuple[str, dict]]:
    """Applicability-driven rule sequence that drives the ALU to RTL-style.

    Initialization (reset + reset_values) then Iteration (clock the Compute
    action). The concrete `updates` already exist on the transition.
    """
    return [
        (
            "Initialization",
            {
                "reset_values": {"result": "0", "zero": "1"},
                "reset_action_name": "Reset",
            },
        ),
        ("Iteration", {"action_name": "Compute"}),
    ]


def _alu_model(op: int, a: int, b: int) -> tuple[int, int]:
    """Reference model of the registered ALU (matches the RTL semantics).

    a/b are single-bit; `/\\` and `\\/` compile to logical && / || (0/1).
    """
    if op == 0:
        r = (a + b) & 0xF
    elif op == 1:
        r = (a - b) & 0xF
    elif op == 2:
        r = 1 if (a and b) else 0
    else:
        r = 1 if (a or b) else 0
    z = 1 if (op == 0 and (a + b) == 0) else 0
    return r, z


# Input stimulus exercising all four ops, including a zero-producing ADD.
_ALU_STIMULUS: list[tuple[int, int, int]] = [
    (0, 1, 1),  # ADD 1+1 = 2
    (1, 1, 0),  # SUB 1-0 = 1
    (2, 1, 1),  # AND 1&1 = 1
    (3, 0, 1),  # OR  0|1 = 1
    (0, 0, 0),  # ADD 0+0 = 0  -> zero flag high
]


def alu_cocotb_trace() -> list[tuple[tuple[int, int, int], tuple[int, int]]]:
    """[((op,a,b), (result,zero)), ...] for each cocotb vector.

    Because the ALU is registered and combinational over its inputs, vector i
    drives (op,a,b) then observes (result,zero) computed from THOSE inputs after
    the edge. The reset-deassert edge reads X inputs but its result is not
    sampled (vector 0 overwrites it), so no offset of the result is needed.
    """
    return [(stim, _alu_model(*stim)) for stim in _ALU_STIMULUS]


# ===========================================================================
# Fixture 3 — 8-bit enable-gated accumulator (active-low reset)
# ===========================================================================
#
# A single 8-bit register `acc` whose next value is its OWN current value plus a
# free 8-bit data input `din`, gated by a 1-bit enable `en`, with mod-256
# wraparound. This is the first fixture whose next-state references BOTH its own
# register AND a free data input (the counter references only itself; the ALU
# references only inputs) — the union a real datapath register needs.
#
# It is also the first fixture with an ACTIVE-LOW reset (`rst_n`), so it
# exercises the RC1 polarity path end to end: the reverse bridge and Compiler 2
# must emit `if (!rst_n)`. (The live FSM run happened to pick active-high `rst`,
# so this is the offline proof of RC1's active-low branch.)
#
# Enable-gated next-state as a flat ELSE-IF chain (TLA+ operators; the THEN
# branch is a flat expression, never a nested conditional). After Initialization
# wraps it in the reset guard the full next-state is
#   IF rst_n = 0 THEN 0 ELSE IF en = 1 THEN acc + din ELSE acc
# — a flat ELSE-IF chain (nesting only in the ELSE branch), so it stays clear of
# the THEN-nesting splitter limitation documented above.
_ACC_NEXT = r"IF en = 1 THEN acc + din ELSE acc"


def accumulator_formal_spec() -> FormalSpec:
    """FormalSpec for the 8-bit enable-gated accumulator (the LLM-facing form).

    The transition's `updates` already carry the concrete enable-gated add, as a
    realistic Agent-3 output would; refinement only adds reset (Initialization,
    active-low) and clocking (Iteration).
    """
    return FormalSpec(
        module_name="accumulator",
        description=(
            "8-bit accumulator. A 1-bit enable `en` gates adding an 8-bit data "
            "input `din` into an 8-bit `acc` register (mod-256 wraparound); when "
            "`en` is low `acc` holds. Synchronous active-low reset clears acc to 0."
        ),
        variables={
            "acc": {"type": "Nat", "width": 8},
        },
        initial={"acc": "0"},
        transitions=[
            {
                "label": "Accumulate",
                "condition": "TRUE",
                "updates": {"acc": _ACC_NEXT},
            },
        ],
        invariants=["acc \\in 0..255"],
    )


def accumulator_summary() -> SpecSummary:
    """SpecSummary (Stage-1 form) for the accumulator, with active-low reset.

    `din` is declared 8-bit and `en` 1-bit (the design's truth); the bridge sizes
    these free inputs from these port widths (the D2 fix), so the generated
    `acc + din` is a full 8-bit add rather than a truncated one. `reset_active_low`
    drives the RC1 codegen path so the bridge + Compiler 2 emit `if (!rst_n)`.
    """
    return SpecSummary(
        module_name="accumulator",
        description="8-bit enable-gated accumulator; sync active-low reset.",
        ports=[
            {"name": "clk", "direction": "input", "width": 1},
            {"name": "rst_n", "direction": "input", "width": 1},
            {"name": "en", "direction": "input", "width": 1},
            {"name": "din", "direction": "input", "width": 8},
            {"name": "acc", "direction": "output", "width": 8},
        ],
        test_vectors=[
            {
                "inputs": {"en": en, "din": din},
                "expected": {"acc": acc},
            }
            for (en, din), acc in accumulator_cocotb_trace()
        ],
        reset_port="rst_n",
        reset_active_low=True,
    )


def accumulator_picker_sequence() -> list[tuple[str, dict]]:
    """Applicability-driven rule sequence that drives the accumulator to RTL-style.

    Initialization (active-low reset clearing acc to 0) then Iteration (clock the
    enable-gated Accumulate action). The concrete enable-gated `updates` already
    exist on the transition, so no Alternation is needed — the `en` guard rides
    inside the update expression, exactly as the ALU's op-mux does.
    """
    return [
        (
            "Initialization",
            {
                "reset_values": {"acc": "0"},
                "reset_action_name": "Reset",
            },
        ),
        ("Iteration", {"action_name": "Accumulate"}),
    ]


def _accumulator_model(stimulus: list[tuple[int, int]]) -> list[tuple[tuple[int, int], int]]:
    """Reference model of the registered, self-referential, mod-256 accumulate.

    Unlike the memoryless ALU, each expected `acc` depends on the PREVIOUS one.
    The starting state is 0: the cocotb reset clears acc, and `en` is pre-driven
    to 0 by the generator's input-init block, so the reset-deassert edge HOLDS acc
    at 0 (no accumulation) — hence there is NO one-cycle offset here (contrast
    traffic_light, whose self-driven next-state DOES step on the deassert edge).
    Vector i drives (en_i, din_i) and the post-edge acc is f(acc_{i-1}, en_i, din_i).
    """
    acc = 0
    out = []
    for en, din in stimulus:
        if en:
            acc = (acc + din) & 0xFF
        out.append(((en, din), acc))
    return out


# Stimulus: accumulate, accumulate, hold (en=0), accumulate, wrap past 255.
_ACC_STIMULUS: list[tuple[int, int]] = [
    (1, 5),    # +5   -> 5
    (1, 10),   # +10  -> 15
    (0, 99),   # hold -> 15   (en=0; din ignored)
    (1, 1),    # +1   -> 16
    (1, 250),  # +250 -> 10   (266 mod 256 — exercises 8-bit wraparound)
]


def accumulator_cocotb_trace() -> list[tuple[tuple[int, int], int]]:
    """[((en,din), acc), ...] for each cocotb vector (see _accumulator_model)."""
    return _accumulator_model(_ACC_STIMULUS)


# ===========================================================================
# Registry — convenient iteration for parametrized tests
# ===========================================================================

MEDIUM_DESIGNS: dict[str, dict] = {
    "traffic_light": {
        "formal_spec": traffic_light_formal_spec,
        "summary": traffic_light_summary,
        "picker_sequence": traffic_light_picker_sequence,
        "cocotb_trace": traffic_light_cocotb_trace,
        "has_free_inputs": False,
    },
    "alu": {
        "formal_spec": alu_formal_spec,
        "summary": alu_summary,
        "picker_sequence": alu_picker_sequence,
        "cocotb_trace": alu_cocotb_trace,
        "has_free_inputs": True,
    },
    "accumulator": {
        "formal_spec": accumulator_formal_spec,
        "summary": accumulator_summary,
        "picker_sequence": accumulator_picker_sequence,
        "cocotb_trace": accumulator_cocotb_trace,
        "has_free_inputs": True,
    },
}
