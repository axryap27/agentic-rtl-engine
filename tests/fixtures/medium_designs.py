r"""
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
# Fixture 4 — 8x8 register file (memory array + registered read)
# ===========================================================================
#
# A small register file: a memory array `mem` of 8 words x 8 bits with a
# synchronous, write-enabled write port and a REGISTERED (1-cycle-latency) read
# port. This is the first fixture with a MEMORY ARRAY (`reg [7:0] mem [0:7]`) —
# a codegen capability no prior design exercises — and the first whose state is
# indexed (mem[waddr] <= wdata; rdata <= mem[raddr]).
#
#   Write port:  on a rising edge, if we=1 then mem[waddr] <= wdata.
#   Read  port:  rdata <= mem[raddr]  (registered: rdata one cycle behind raddr).
#   Reset:       clears rdata to 0 (the memory itself is NOT reset — memories
#                are synthesis-canonically un-reset; engine.is_rtl_style carves
#                memory variables out of the reset_value requirement).
#
# WHY REGISTERED (not combinational) READ
# ---------------------------------------
# A registered read keeps BOTH ports clocked, so refinement is the SAME rule
# sequence as every prior design — Initialization, then Iteration on each clocked
# action — using only the existing Tier-1 rules (no new rule, no combinational
# action). It also lets the read port reset (rdata <= 0), so the design has a
# non-empty reset and a clean reset port. The cost is a 1-cycle read latency and
# read-before-write semantics, both modelled exactly in _register_file_model.
#
# INDEXED WRITE ENCODING (load-bearing!)
# --------------------------------------
# A memory-element write rides in the FormalSpec transition's `updates` KEY:
# `{"mem[waddr]": "wdata"}`. The bridge parses the `[index]` off the key into an
# engine-spec update {"variable":"mem","index":"waddr","expression":"wdata"}; the
# variable carries `depth` to mark it a memory. No new Transition field is needed.

_RF_DEPTH = 8     # 8 words
_RF_AW = 3        # 3-bit address (ceil log2 8)
_RF_DW = 8        # 8-bit data


def register_file_formal_spec() -> FormalSpec:
    """FormalSpec for the 8x8 register file (the LLM-facing form).

    `mem` is a memory array (depth=8); the Write transition targets one element
    via the indexed update key `mem[waddr]`. `rdata` is a registered read of
    `mem[raddr]`. Refinement adds reset (Initialization, clears rdata only) and
    clocking (Iteration on BOTH Write and Read). `mem` is not reset/initialized.
    """
    return FormalSpec(
        module_name="register_file",
        description=(
            "8-word x 8-bit register file. A synchronous write port writes "
            "`wdata` into `mem[waddr]` when `we` is high; a registered read port "
            "presents `mem[raddr]` on `rdata` one cycle later. Synchronous reset "
            "clears `rdata` to 0; the memory contents are not reset."
        ),
        variables={
            "mem":   {"type": "Nat", "width": _RF_DW, "depth": _RF_DEPTH},
            "rdata": {"type": "Nat", "width": _RF_DW},
        },
        initial={"rdata": "0"},   # mem is a memory — left uninitialised
        transitions=[
            {
                "label": "Write",
                "condition": "we = 1",
                "updates": {"mem[waddr]": "wdata"},
            },
            {
                "label": "Read",
                "condition": "TRUE",
                "updates": {"rdata": "mem[raddr]"},
            },
        ],
        invariants=["rdata \\in 0..255"],
    )


def register_file_summary() -> SpecSummary:
    """SpecSummary (Stage-1 form) for the register file.

    The interface is entirely SCALAR — clk, reset, we, waddr, wdata, raddr, rdata
    — so the cocotb generator needs no array support; `mem` is internal. `waddr`
    and `raddr` are declared 3-bit so the bridge sizes those free-input address
    ports correctly (D2). Test vectors come from the registered-read reference
    model (with its reset-offset and read-before-write semantics).
    """
    return SpecSummary(
        module_name="register_file",
        description="8x8 register file: we-gated sync write, registered read; sync reset clears rdata.",
        ports=[
            {"name": "clk", "direction": "input", "width": 1},
            {"name": "reset", "direction": "input", "width": 1},
            {"name": "we", "direction": "input", "width": 1},
            {"name": "waddr", "direction": "input", "width": _RF_AW},
            {"name": "wdata", "direction": "input", "width": _RF_DW},
            {"name": "raddr", "direction": "input", "width": _RF_AW},
            {"name": "rdata", "direction": "output", "width": _RF_DW},
        ],
        test_vectors=[
            {"inputs": {"we": we, "waddr": waddr, "wdata": wdata, "raddr": raddr},
             "expected": expected}
            for (we, waddr, wdata, raddr), expected in register_file_cocotb_trace()
        ],
        reset_port="reset",
        reset_active_low=False,
    )


def register_file_picker_sequence() -> list[tuple[str, dict]]:
    """Applicability-driven rule sequence that drives the register file to RTL-style.

    Initialization (reset clears rdata only — `mem` is a memory and is not reset),
    then Iteration on EACH clocked action (Write and Read). Only existing Tier-1
    rules: a registered read means both ports are clocked, so the sequence is the
    same shape as every prior design (no new rule for memories).
    """
    return [
        (
            "Initialization",
            {"reset_values": {"rdata": "0"}, "reset_action_name": "Reset"},
        ),
        ("Iteration", {"action_name": "Write"}),
        ("Iteration", {"action_name": "Read"}),
    ]


# Per-cycle stimulus (we, waddr, wdata, raddr). v0 is a warm-up write whose read
# is of an unwritten cell (X) and therefore carries no assertion.
_RF_STIMULUS: list[tuple[int, int, int, int]] = [
    (1, 1, 11, 1),   # v0: write mem[1]=11; read mem[1] (still X) -> warm-up
    (1, 2, 22, 1),   # v1: write mem[2]=22; read mem[1]=11
    (0, 2, 99, 2),   # v2: we=0 -> NO write (waddr/wdata ignored); read mem[2]=22
    (1, 3, 33, 2),   # v3: write mem[3]=33; read mem[2]=22 (persistence)
    (0, 0, 0, 3),    # v4: read mem[3]=33
    (1, 1, 55, 1),   # v5: write mem[1]=55; read mem[1]=11 (read-BEFORE-write)
    (0, 0, 0, 1),    # v6: read mem[1]=55 (overwrite visible next cycle)
]


def _register_file_model(
    stimulus: list[tuple[int, int, int, int]],
) -> list[tuple[tuple[int, int, int, int], dict]]:
    """Registered-read reference model (matches the generated RTL exactly).

    rdata is REGISTERED: rdata after edge i = mem[raddr_i] sampled BEFORE that
    edge's write (read-before-write — both are nonblocking off the same edge).
    The cocotb generator clocks one reset-deassert edge before vector 0 with all
    inputs at 0, so no write happens before v0 and mem starts empty. A vector
    whose read addresses a cell never written in a PRIOR cycle yields X, so it
    carries an empty `expected` (the generator emits no assertion for it).
    """
    mask = (1 << _RF_DW) - 1
    mem: dict[int, int] = {}            # addr -> value; absent = unwritten (X)
    out: list[tuple[tuple[int, int, int, int], dict]] = []
    for (we, waddr, wdata, raddr) in stimulus:
        read_val = mem.get(raddr)        # sampled before this edge's write
        if we:
            mem[waddr] = wdata & mask     # write on this edge
        expected: dict = {} if read_val is None else {"rdata": read_val}
        out.append(((we, waddr, wdata, raddr), expected))
    return out


def register_file_cocotb_trace() -> list[tuple[tuple[int, int, int, int], dict]]:
    """[((we,waddr,wdata,raddr), expected_dict), ...] for each cocotb vector."""
    return _register_file_model(_RF_STIMULUS)


# ===========================================================================
# Fixture 5 — 4-deep synchronous FIFO (memory + pointers + combinational flags)
# ===========================================================================
#
# A synchronous FIFO / circular buffer: an 8-bit-wide, 4-deep memory with a
# we-gated write port, a registered read port, and COMBINATIONAL full/empty
# flags. This is the first design with a combinational OUTPUT — the flags must
# reflect the CURRENT occupancy (a registered flag would lag and allow over/under-
# flow), so `full`/`empty` are continuous `assign`s, not registers. It reuses the
# register file's memory codegen (mem[4][8], indexed write/read) and adds two
# pointers, an occupancy counter, and the flag logic.
#
#   Write port:  if wr_en and not full, mem[wptr] <= din; wptr advances (mod 4).
#   Read  port:  if rd_en and not empty, dout <= mem[rptr] (registered); rptr++.
#   count:       occupancy; +1 on a write-only cycle, -1 on a read-only cycle,
#                unchanged on simultaneous read+write (a flat ELSE-IF priority
#                chain — nesting only in the ELSE, never the THEN).
#   full/empty:  COMBINATIONAL — assign full = count == 4; assign empty = count == 0.
#   Reset:       clears wptr/rptr/count/dout to 0 (NOT mem, NOT the flags).
#
# wptr/rptr/count are emitted as observability output ports (state vars, not
# r_-prefixed); the summary below lists only the eight real ports, so the cocotb
# bench drives/checks exactly the FIFO interface and ignores the extra outputs.

_FIFO_DEPTH = 4
_FIFO_AW = 2        # 2-bit pointers
_FIFO_DW = 8        # 8-bit data
_FIFO_CW = 3        # count is 0..4 -> 3 bits

# count next-state: a flat ELSE-IF priority chain (simultaneous r+w first ->
# unchanged; then write-only -> +1; then read-only -> -1; else hold).
_FIFO_COUNT_NEXT = (
    "IF wr_en = 1 AND full = 0 AND rd_en = 1 AND empty = 0 THEN count "
    "ELSE IF wr_en = 1 AND full = 0 THEN count + 1 "
    "ELSE IF rd_en = 1 AND empty = 0 THEN count - 1 "
    "ELSE count"
)


def fifo_formal_spec() -> FormalSpec:
    """FormalSpec for the 4-deep FIFO (the LLM-facing form).

    `mem` is a memory (depth 4); `full`/`empty` are COMBINATIONAL (the Flags
    transition carries combinational=True). Refinement clocks the three register
    transitions (Write, Read, UpdateCount) and resets wptr/rptr/count/dout; the
    Flags transition is born combinational and is never iterated or reset.
    """
    return FormalSpec(
        module_name="fifo",
        description=(
            "4-deep, 8-bit synchronous FIFO. A we-gated write port writes din into "
            "mem[wptr] and advances wptr; a registered read port presents mem[rptr] "
            "on dout and advances rptr; an occupancy counter drives COMBINATIONAL "
            "full (count==4) and empty (count==0) flags. Synchronous reset clears "
            "the pointers, counter, and dout; the memory and flags are not reset."
        ),
        variables={
            "mem":   {"type": "Nat", "width": _FIFO_DW, "depth": _FIFO_DEPTH},
            "wptr":  {"type": "Nat", "width": _FIFO_AW},
            "rptr":  {"type": "Nat", "width": _FIFO_AW},
            "count": {"type": "Nat", "width": _FIFO_CW},
            "dout":  {"type": "Nat", "width": _FIFO_DW},
            "full":  {"type": "Bit", "width": 1},
            "empty": {"type": "Bit", "width": 1},
        },
        initial={"wptr": "0", "rptr": "0", "count": "0", "dout": "0"},
        transitions=[
            {"label": "Write", "condition": "wr_en = 1 AND full = 0",
             "updates": {"mem[wptr]": "din", "wptr": "(wptr + 1) % 4"}},
            {"label": "Read", "condition": "rd_en = 1 AND empty = 0",
             "updates": {"dout": "mem[rptr]", "rptr": "(rptr + 1) % 4"}},
            {"label": "UpdateCount", "condition": "TRUE",
             "updates": {"count": _FIFO_COUNT_NEXT}},
            {"label": "Flags", "condition": "TRUE", "combinational": True,
             "updates": {"full": "count = 4", "empty": "count = 0"}},
        ],
        invariants=["count \\in 0..4"],
    )


def fifo_summary() -> SpecSummary:
    """SpecSummary (Stage-1 form) for the FIFO — the eight real interface ports.

    The internal pointers/counter are not listed (they emit as extra observability
    outputs the bench ignores). The test vectors drive wr_en/rd_en/din and assert
    dout/full/empty, from the reference model below.
    """
    return SpecSummary(
        module_name="fifo",
        description="4-deep 8-bit synchronous FIFO with combinational full/empty.",
        ports=[
            {"name": "clk", "direction": "input", "width": 1},
            {"name": "reset", "direction": "input", "width": 1},
            {"name": "wr_en", "direction": "input", "width": 1},
            {"name": "rd_en", "direction": "input", "width": 1},
            {"name": "din", "direction": "input", "width": _FIFO_DW},
            {"name": "dout", "direction": "output", "width": _FIFO_DW},
            {"name": "full", "direction": "output", "width": 1},
            {"name": "empty", "direction": "output", "width": 1},
        ],
        test_vectors=[
            {"inputs": {"wr_en": we, "rd_en": re, "din": din}, "expected": exp}
            for (we, re, din), exp in fifo_cocotb_trace()
        ],
        reset_port="reset",
        reset_active_low=False,
    )


def fifo_picker_sequence() -> list[tuple[str, dict]]:
    """Init (reset the registers only) then Iteration on EACH clocked register
    transition (Write, Read, UpdateCount). The Flags transition is combinational,
    so it is never iterated — only the existing Tier-1 rules are used."""
    return [
        ("Initialization", {
            "reset_values": {"wptr": "0", "rptr": "0", "count": "0", "dout": "0"},
            "reset_action_name": "Reset"}),
        ("Iteration", {"action_name": "Write"}),
        ("Iteration", {"action_name": "Read"}),
        ("Iteration", {"action_name": "UpdateCount"}),
    ]


# Stimulus (wr_en, rd_en, din): fill to full, blocked write, drain, simultaneous
# read+write, empty, blocked read, write after drain.
_FIFO_STIMULUS: list[tuple[int, int, int]] = [
    (1, 0, 10), (1, 0, 20), (1, 0, 30), (1, 0, 40),  # fill -> full after the 4th
    (1, 0, 50),                                       # write blocked (full)
    (0, 1, 0), (0, 1, 0),                             # read 10, 20 (registered dout)
    (1, 1, 60),                                       # simultaneous r+w (count holds)
    (0, 1, 0), (0, 1, 0),                             # drain to empty
    (0, 1, 0),                                        # read blocked (empty)
    (1, 0, 70),                                       # write after drain
]


def _fifo_model(stim: list[tuple[int, int, int]]) -> list[tuple[tuple[int, int, int], dict]]:
    """Reference model matching the generated RTL exactly.

    Combinational full/empty are sampled from the PRE-edge count (so the write/read
    gates use current occupancy); dout is a REGISTERED read (takes mem[rptr] sampled
    before this edge's write); the asserted full/empty reflect the POST-edge count
    (they are combinational off the just-updated counter). The generator clocks one
    reset-deassert edge before vector 0 with all inputs 0 (a no-op), so the model
    starts from the reset state. dout is always defined (reset 0, then read values),
    so every vector asserts it.
    """
    mem = [None] * _FIFO_DEPTH
    wptr = rptr = count = 0
    dout = 0
    out: list[tuple[tuple[int, int, int], dict]] = []
    for (we, re, din) in stim:
        full = 1 if count == _FIFO_DEPTH else 0
        empty = 1 if count == 0 else 0
        do_w = 1 if (we == 1 and full == 0) else 0
        do_r = 1 if (re == 1 and empty == 0) else 0
        new_dout = mem[rptr] if do_r else dout   # read-before-write
        if do_w:
            mem[wptr] = din
            wptr = (wptr + 1) % _FIFO_DEPTH
        if do_r:
            rptr = (rptr + 1) % _FIFO_DEPTH
        count = count + do_w - do_r
        dout = new_dout
        out.append((
            (we, re, din),
            {"dout": dout,
             "full": 1 if count == _FIFO_DEPTH else 0,
             "empty": 1 if count == 0 else 0},
        ))
    return out


def fifo_cocotb_trace() -> list[tuple[tuple[int, int, int], dict]]:
    """[((wr_en,rd_en,din), {dout,full,empty}), ...] for each cocotb vector."""
    return _fifo_model(_FIFO_STIMULUS)


# ===========================================================================
# Fixture 6 — 8x8 sequential shift-add multiplier (control FSM + datapath)
# ===========================================================================
#
# The first FSMD: a control FSM sequencing a multi-cycle datapath behind a
# start/done handshake — the canonical "real hardware" pattern. An 8x8->16-bit
# multiply is computed by the classic shift-add algorithm over 8 clocks:
#
#   state: IDLE(0) --start--> BUSY(1) x8 cycles --> DONE(2) --> IDLE(0)
#   on load (start while NOT busy): product=0, mcand=a, mplier=b, count=8
#   each BUSY cycle: if mplier's low bit set, product += mcand;
#                    then shift mcand left, shift mplier right, count--;
#                    when count reaches 1, also go DONE (the 8th/MSB bit is
#                    still processed on that cycle).
#   done = COMBINATIONAL (state == 2); product is the 16-bit accumulator output.
#
# HANDSHAKE (load-bearing!): the load fires on start while NOT BUSY — i.e. in
# IDLE *or* DONE: `(state = 0 OR state = 2) AND start = 1`. Accepting start in
# DONE is what makes back-to-back work: a start pulse that coincides with a
# previous multiply's 1-cycle DONE reloads immediately instead of being silently
# dropped. (The first live run used an IDLE-only load and DROPPED a start that
# landed in DONE — the third multiply never ran; this is the fix, and the
# stimulus below exercises a start landing in DONE.)
#
# SHIFT/BIT PRIMITIVES VIA ARITHMETIC (load-bearing!)
# ---------------------------------------------------
# The expression pipeline has no <<, >>, bitwise &, bit-select, or concat. The
# shift-add primitives are therefore expressed arithmetically — all SUPPORTED
# end to end (bridge -> Compiler 2 -> spec_sim):
#   mplier's low bit  ->  (mplier % 2) = 1     (% modulo)
#   shift mcand left  ->  mcand * 2            (* by constant)
#   shift mplier right->  mplier / 2           (/ by constant)
# `mcand` is 16-bit so the left-shifts don't lose the high partial products.
#
# REFINEMENT: this is the SIMPLEST refinement of any medium design — a single
# clocked `Step` transition (every register updates together each clock) plus a
# combinational `done`. Reuses ONLY Initialization + Iteration; no new rule.
# `start`, `a`, `b` are FREE INPUTS. state/count/mcand/mplier emit as extra
# observability output regs (the summary lists only the real interface).
#
# MULTI-CYCLE VERIFICATION: each multiply is a start pulse + 8 BUSY cycles, with
# `done` pulsing 8 cycles after its start (index 8 of the block). The spec-derived
# golden vectors make this tractable live — Agent 1 supplies only the start/operand
# STIMULUS; spec_sim derives the per-cycle product/done.

_MUL_DW = 8         # operand width
_MUL_PW = 16        # product width (8+8)
_MUL_CW = 4         # iteration counter 0..8 -> 4 bits
_MUL_SW = 2         # state 0..2 -> 2 bits

# Control FSM next-state (flat ELSE-IF priority chain).
_MUL_STATE_NEXT = (
    "IF (state = 0 OR state = 2) AND start = 1 THEN 1 "
    "ELSE IF state = 1 AND count = 1 THEN 2 "
    "ELSE IF state = 1 THEN 1 "
    "ELSE IF state = 2 THEN 0 "
    "ELSE 0"
)
# Accumulator: clear on load, conditionally add the (shifted) multiplicand each
# BUSY cycle when the multiplier's current low bit is set, else hold.
_MUL_PRODUCT_NEXT = (
    "IF (state = 0 OR state = 2) AND start = 1 THEN 0 "
    "ELSE IF state = 1 AND (mplier % 2) = 1 THEN product + mcand "
    "ELSE product"
)
# Shifting multiplicand (left shift = *2); loaded from operand `a`.
_MUL_MCAND_NEXT = (
    "IF (state = 0 OR state = 2) AND start = 1 THEN a "
    "ELSE IF state = 1 THEN mcand * 2 "
    "ELSE mcand"
)
# Shifting multiplier (right shift = /2); loaded from operand `b`.
_MUL_MPLIER_NEXT = (
    "IF (state = 0 OR state = 2) AND start = 1 THEN b "
    "ELSE IF state = 1 THEN mplier / 2 "
    "ELSE mplier"
)
# Iteration counter: load 8 on start, decrement each BUSY cycle, else hold.
_MUL_COUNT_NEXT = (
    "IF (state = 0 OR state = 2) AND start = 1 THEN 8 "
    "ELSE IF state = 1 THEN count - 1 "
    "ELSE count"
)


def multiplier_formal_spec() -> FormalSpec:
    """FormalSpec for the 8x8 sequential shift-add multiplier (the LLM-facing form).

    All datapath registers update together in one clocked `Step` transition whose
    `updates` carry the guarded next-state chains; `done` is a combinational flag.
    Refinement adds reset (Initialization) and clocking (Iteration on Step) — the
    combinational DoneFlag is born concrete and never iterated or reset.
    """
    return FormalSpec(
        module_name="multiplier",
        description=(
            "8x8 sequential shift-add multiplier with a start/done handshake. A "
            "control FSM (IDLE/BUSY/DONE) sequences an 8-cycle shift-add datapath: "
            "on start it loads the operands, each BUSY cycle conditionally adds the "
            "shifted multiplicand and shifts, and after 8 cycles asserts a "
            "combinational done with the 16-bit product. Synchronous reset returns "
            "to IDLE with the datapath cleared."
        ),
        variables={
            "product": {"type": "Nat", "width": _MUL_PW},
            "mcand":   {"type": "Nat", "width": _MUL_PW},
            "mplier":  {"type": "Nat", "width": _MUL_DW},
            "count":   {"type": "Nat", "width": _MUL_CW},
            "state":   {"type": "Nat", "width": _MUL_SW},
            "done":    {"type": "Bit", "width": 1},
        },
        initial={"product": "0", "mcand": "0", "mplier": "0", "count": "0", "state": "0"},
        transitions=[
            {"label": "Step", "condition": "TRUE", "updates": {
                "state":   _MUL_STATE_NEXT,
                "product": _MUL_PRODUCT_NEXT,
                "mcand":   _MUL_MCAND_NEXT,
                "mplier":  _MUL_MPLIER_NEXT,
                "count":   _MUL_COUNT_NEXT,
            }},
            {"label": "DoneFlag", "condition": "TRUE", "combinational": True,
             "updates": {"done": "state = 2"}},
        ],
        invariants=["state \\in 0..2", "count \\in 0..8"],
    )


def multiplier_summary() -> SpecSummary:
    """SpecSummary (Stage-1 form) for the sequential multiplier.

    The interface is start/a/b (inputs) -> product/done (outputs); the FSM state
    and datapath scratch registers are internal (emitted as observability outputs
    the bench ignores). Test vectors drive the start/operand stimulus and assert
    product+done every cycle, from the cycle-accurate reference model.
    """
    return SpecSummary(
        module_name="multiplier",
        description="8x8 sequential shift-add multiplier with start/done handshake.",
        ports=[
            {"name": "clk", "direction": "input", "width": 1},
            {"name": "reset", "direction": "input", "width": 1},
            {"name": "start", "direction": "input", "width": 1},
            {"name": "a", "direction": "input", "width": _MUL_DW},
            {"name": "b", "direction": "input", "width": _MUL_DW},
            {"name": "product", "direction": "output", "width": _MUL_PW},
            {"name": "done", "direction": "output", "width": 1},
        ],
        test_vectors=[
            {"inputs": {"start": st, "a": a, "b": b}, "expected": exp}
            for (st, a, b), exp in multiplier_cocotb_trace()
        ],
        reset_port="reset",
        reset_active_low=False,
    )


def multiplier_picker_sequence() -> list[tuple[str, dict]]:
    """Init (reset the datapath registers) then Iteration on the single clocked
    `Step` transition. The combinational DoneFlag is never iterated — only the
    existing Tier-1 rules are used (the simplest medium-design refinement)."""
    return [
        ("Initialization", {
            "reset_values": {"product": "0", "mcand": "0", "mplier": "0",
                             "count": "0", "state": "0"},
            "reset_action_name": "Reset"}),
        ("Iteration", {"action_name": "Step"}),
    ]


# ===========================================================================
# Abstract multiplier — the VERIFIED-DERIVATION form
# ===========================================================================
#
# The concrete `multiplier` above hands the engine a fully-scheduled FSMD and
# only adds reset + clocking. THIS fixture instead hands the engine an ABSTRACT
# Morgan spec statement — one transition whose postcondition is `product = a * b`
# over a still-abstract `product` — and DERIVES the scheduled FSMD with the
# refinement calculus:
#
#   LoopIntroduction   discharges the iteration-rule obligations (O1/O2/O3) for
#                      the shift-add invariant, then installs a verified bare
#                      loop + records the loop structure (init/body/variant/guard).
#   ScheduleHandshakeFSM  mechanically schedules that bare loop into the same
#                      hardened IDLE/BUSY/DONE start/done FSMD as the concrete
#                      fixture (a deterministic transform — no proof, no LLM).
#   Initialization     adds the synchronous reset.
#
# The cocotb interface (ports + stimulus + expected trace) is IDENTICAL to the
# concrete multiplier — `abstract_multiplier_summary` simply re-skins the proven
# `multiplier_summary` with the derived module name — so the DERIVED RTL is
# verified against the very same vectors. The picker_sequence below is the
# derivation; it is what a correct pick_rule would emit.


def abstract_multiplier_formal_spec() -> FormalSpec:
    """FormalSpec for the multiplier as an ABSTRACT spec statement.

    ONE transition, `spec_statement=True`, postcondition `product = a * b`, with
    `product` still abstract (width 16). The bridge marks `product` abstract so
    LoopIntroduction fires; the `updates` RHS carries the abstract relation as a
    documentation placeholder (refinement replaces it). `a`/`b` are free inputs
    (never declared as state) — the obligation kernel takes their widths via the
    LoopIntroduction params, not the spec.
    """
    return FormalSpec(
        module_name="shift_add_multiplier",
        description=(
            "8x8 multiplier specified abstractly as product = a * b (a Morgan "
            "spec statement). The refinement engine DERIVES a verified shift-add "
            "FSMD: LoopIntroduction discharges the iteration-rule obligations for "
            "the shift-add invariant, ScheduleHandshakeFSM schedules the verified "
            "loop into an IDLE/BUSY/DONE start/done datapath, and Initialization "
            "adds synchronous reset."
        ),
        variables={
            "product": {"type": "Nat", "width": _MUL_PW},
        },
        initial={},
        transitions=[
            {"label": "Multiply", "condition": "start = 1", "spec_statement": True,
             "postcondition": "product = a * b",
             "updates": {"product": "a * b"}},
        ],
        invariants=[],
    )


# The derivation chain (what a correct pick_rule emits). LoopIntroduction's params
# are the shift-add proposal: invariant `product + mplier*mcand = a*b`, variant
# `count`, loaded {product:0, mcand:a, mplier:b, count:8}, one shift-add step.
#
# NOTE: input_widths is 6-bit (a,b in 0..63) ONLY to keep the obligation check
# fast in the test suite — exhaustive over 2^(6+6)=4096 (a,b) pairs, ~1s — rather
# than 8-bit (2^16=65536, slow). The shift-add invariant is width-GENERIC: the
# 6-bit proof certifies the same algebraic identity that the 8-bit datapath runs.
# count is loaded to 8 either way (8 iterations cover 8-bit operands), and the
# generated RTL is the full 8-bit datapath (the cocotb interface drives 8-bit a/b).
_ABS_MUL_INIT = {"product": "0", "mcand": "a", "mplier": "b", "count": "8"}
_ABS_MUL_BODY = {
    "product": "IF (mplier % 2) = 1 THEN product + mcand ELSE product",
    "mcand": "mcand * 2",
    "mplier": "mplier / 2",
    "count": "count - 1",
}
_ABS_MUL_LOOP_PARAMS = {
    "action_name": "Multiply",
    "postcondition": "product = a * b",
    "invariant": "product + mplier * mcand = a * b",
    "variant": "count",
    "guard": "count > 0",
    "init": _ABS_MUL_INIT,
    "body": _ABS_MUL_BODY,
    "mapping": {"product": "product"},
    "fresh_vars": [
        {"name": "mcand", "width": 16},
        {"name": "mplier", "width": 8},
        {"name": "count", "width": 4},
    ],
    "input_widths": {"a": 6, "b": 6},   # 6-bit => exhaustive proof, fast (see NOTE)
}


def abstract_multiplier_picker_sequence() -> list[tuple[str, dict]]:
    """The verified DERIVATION: introduce the verified loop, schedule it into the
    handshake FSMD, then add reset. This is what a correct pick_rule emits."""
    return [
        ("LoopIntroduction", _ABS_MUL_LOOP_PARAMS),
        ("ScheduleHandshakeFSM", {"action_name": "Multiply"}),
        ("Initialization", {
            "reset_values": {"product": "0", "mcand": "0", "mplier": "0",
                             "count": "0", "state": "0"},
            "reset_action_name": "Reset"}),
    ]


def abstract_multiplier_summary() -> SpecSummary:
    """Stage-1 summary for the DERIVED multiplier: identical interface, stimulus,
    and expected trace as the concrete multiplier — only the module name differs —
    so the derived RTL is verified against the very same cocotb vectors."""
    summ = multiplier_summary().model_dump()
    summ["module_name"] = "shift_add_multiplier"
    return SpecSummary.model_validate(summ)


def _multiplier_model(
    stim: list[tuple[int, int, int]],
) -> list[tuple[tuple[int, int, int], dict]]:
    """Cycle-accurate reference model matching the generated RTL exactly.

    Mirrors the cocotb generator: reset clears the registers, one reset-deassert
    edge runs with inputs 0 (a no-op hold in IDLE), then one edge per vector.
    Outputs are sampled AFTER each edge: product is the 16-bit accumulator and
    done is combinational (state == DONE). All register updates are read-before-
    write off the current state, exactly like the nonblocking RTL.
    """
    pmask, mmask, cmask = (1 << _MUL_PW) - 1, (1 << _MUL_DW) - 1, (1 << _MUL_CW) - 1
    product = mcand = mplier = count = state = 0

    def step(start: int, a: int, b: int) -> None:
        nonlocal product, mcand, mplier, count, state
        if state != 1 and start == 1:        # load from IDLE *or* DONE (not BUSY)
            ns, npd, nmc, nmp, nc = 1, 0, a & pmask, b & mmask, 8
        elif state == 1:
            npd = (product + mcand) & pmask if (mplier % 2) == 1 else product
            nmc = (mcand * 2) & pmask
            nmp = (mplier // 2) & mmask
            nc = (count - 1) & cmask
            ns = 2 if count == 1 else 1
        else:  # DONE with no start, or idle -> IDLE, hold the datapath
            ns, npd, nmc, nmp, nc = 0, product, mcand, mplier, count
        state, product, mcand, mplier, count = ns, npd, nmc, nmp, nc

    step(0, 0, 0)  # reset-deassert edge (inputs 0)
    out: list[tuple[tuple[int, int, int], dict]] = []
    for (st, a, b) in stim:
        step(st, a, b)
        out.append(((st, a, b), {"product": product, "done": 1 if state == 2 else 0}))
    return out


def _mul_stimulus() -> list[tuple[int, int, int]]:
    """(start, a, b) per cycle: three multiplies exercising both handshake paths.

    Each multiply is a start pulse + 8 BUSY cycles, with `done` pulsing on the
    8th idle cycle (index 8 after its start). The second start lands in the FIRST
    multiply's DONE cycle (a true back-to-back) — the hardened handshake accepts
    start in IDLE *or* DONE, so it reloads rather than dropping the start (the
    live-run bug). The third uses a normal IDLE start after a gap. Covers a normal
    product, the 16-bit maximum (255*255), a zero operand, and back-to-back."""
    stim: list[tuple[int, int, int]] = []
    # mult 1: 13*11=143, start in IDLE; done at index 8.
    stim.append((1, 13, 11)); stim.extend((0, 0, 0) for _ in range(8))
    # mult 2: 255*255, start lands in mult-1's DONE cycle (back-to-back reload).
    stim.append((1, 255, 255)); stim.extend((0, 0, 0) for _ in range(8))
    # return to IDLE (DONE->IDLE, then idle), then mult 3 with a normal IDLE start.
    stim.extend((0, 0, 0) for _ in range(2))
    # mult 3: 0*99=0 (zero operand), start in IDLE.
    stim.append((1, 0, 99)); stim.extend((0, 0, 0) for _ in range(8))
    stim.append((0, 0, 0))
    return stim


def multiplier_cocotb_trace() -> list[tuple[tuple[int, int, int], dict]]:
    """[((start,a,b), {product,done}), ...] for each cocotb vector."""
    return _multiplier_model(_mul_stimulus())


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
    "register_file": {
        "formal_spec": register_file_formal_spec,
        "summary": register_file_summary,
        "picker_sequence": register_file_picker_sequence,
        "cocotb_trace": register_file_cocotb_trace,
        "has_free_inputs": True,
    },
    "fifo": {
        "formal_spec": fifo_formal_spec,
        "summary": fifo_summary,
        "picker_sequence": fifo_picker_sequence,
        "cocotb_trace": fifo_cocotb_trace,
        "has_free_inputs": True,
    },
    "multiplier": {
        "formal_spec": multiplier_formal_spec,
        "summary": multiplier_summary,
        "picker_sequence": multiplier_picker_sequence,
        "cocotb_trace": multiplier_cocotb_trace,
        "has_free_inputs": True,
    },
    # The verified-derivation form: an abstract product=a*b spec statement that the
    # engine DERIVES into the same FSMD via LoopIntroduction + ScheduleHandshakeFSM
    # + Initialization. Reuses the concrete multiplier's interface/stimulus/trace.
    "abstract_multiplier": {
        "formal_spec": abstract_multiplier_formal_spec,
        "summary": abstract_multiplier_summary,
        "picker_sequence": abstract_multiplier_picker_sequence,
        "cocotb_trace": multiplier_cocotb_trace,
        "has_free_inputs": True,
    },
}
