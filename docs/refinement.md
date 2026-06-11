# The Refinement Engine

Stage 3 lowers an **abstract** formal spec (what the circuit should do) to an
**RTL-style** spec (clocks, registers, reset, concrete updates) by applying a sequence
of small, provably correct transformations. The engine is the deterministic loop that
drives this; [Agent 3](agents.md#agent-3) only ever *chooses* the next rule. The full
sequence of choices is logged to `refinement_chain.json`, giving a replayable proof
trail from abstract spec to RTL.

For the theory and the refinement-calculus lineage of these rules, see
[background.md](background.md).

---

## The engine loop

**File:** `pipeline/refinement/engine.py`

```python
run(formal_spec: dict,
    pick_rule: Callable[[list[dict], dict], dict],
    *,
    run_id: str = "default",
    tlc_check: Callable[[dict], bool] | None = None,
    allowed_rule_names: set[str] | None = None,
    termination_check: Callable[[dict], bool] = is_rtl_style,
    max_steps: int = MAX_STEPS) -> dict
```

Each iteration:

1. **Filter** `RULE_REGISTRY` to the rules whose `is_applicable(spec)` is true,
   intersected with `allowed_rule_names` if a pass restricts them.
2. **Ask** `pick_rule(applicable, spec)` for a `{"rule_name", "params"}` choice
   (this is the injected Agent-3 callable; the engine itself makes no LLM call).
3. **Validate** the choice is in the applicable set and not already excluded.
4. **Apply** `rule.apply(spec, params)` — a pure function.
5. **Gate** the candidate through `tlc_check` if provided (mid-refinement TLC).
6. **Commit**: append a step to the chain, persist it, advance.

The loop ends when `termination_check(spec)` holds (default `is_rtl_style`). Hard
limits: `MAX_STEPS = 200` (raises `RefinementStall` if exceeded) and
`MAX_BACKTRACK_DEPTH = 5`.

`pick_rule` is injected, not imported — so the engine is fully testable with a scripted
stub picker and never depends on a live LLM. The deterministic test suite drives the
whole loop this way.

### When is a spec "RTL-style"?

`is_rtl_style(spec)` is the termination predicate. A spec is RTL-style when **all** of:

1. at least one variable exists;
2. a `reset_action` is named;
3. at least one non-reset action exists;
4. every variable has `abstract == False`;
5. every variable has a non-empty `type`;
6. every variable has a non-`None` `reset_value` — **except** memory arrays (`depth`
   set: synthesis-canonical memories carry no reset) and combinational wires
   (`combinational: True`: driven by a continuous `assign`, never reset);
7. every non-reset action has `clocked == True`;
8. every non-reset action has non-empty `updates`.

Conditions 7–8 have two structural carve-outs: a **pure identity hold** (every update
is `v' = v`, per `bridge._is_identity_hold`) emits nothing distinct and need not be
separately clocked, and a **combinational action** (`combinational: True`) is
continuous logic — the bridge emits it as CombinationalLogic / `assign`, never
clocked. Ordinary register variables and actions are still held to all eight.

---

## The rule library

**File:** `pipeline/refinement/rules/` · `RULE_REGISTRY` registers **eight** rules, in
order: the six structural Tier-1 rules, then the verified-derivation pair
(`LoopIntroduction`, `ScheduleHandshakeFSM` — [documented below](#verified-derivation-loopintroduction--schedulehandshakefsm)).
Every rule subclasses `RefinementRule` (`base.py`) and implements exactly three
methods:

```python
def is_applicable(self, spec: dict) -> bool   # can this rule fire now?
def apply(self, spec: dict, params: dict) -> dict   # pure; returns the refined spec
def describe(self) -> str                     # one-line description shown to pick_rule
```

`apply()` **must be pure** — same `(spec, params)` always yields the same output, and
neither `spec` nor `params` is mutated (deepcopy before writing). Purity is what makes
backtracking sound: the engine reconstructs any prior state by replaying the saved
chain from scratch.

| Rule | Fires when… | Key params | Hardware meaning |
|---|---|---|---|
| **Initialization** | a non-memory, non-combinational variable lacks a `reset_value`, or no `reset_action` is set | `reset_values: {var → expr}`, `reset_action_name="Reset"` | synchronous reset: every register gets a known start value |
| **Iteration** | a non-reset, non-combinational action has `clocked == False` | `action_name` | clock the action — its body becomes a per-cycle register update |
| **SequentialComposition** | a non-reset, non-combinational action has neither `sequential_steps` nor `branches` | `action_name`, `steps: [{name, guard, updates}]` | ordered combinational steps within one cycle |
| **Assignment** | a non-reset action has empty `updates` | `action_name`, `updates: [{variable, expression}]` | concrete register write(s) |
| **Alternation** | a non-reset, non-combinational action has no `branches` | `action_name`, `branches: [{guard, updates}]` | mutually-exclusive guarded branches (if / case / mux) |
| **IntroduceVariable** | always (engine checks name uniqueness) | `name`, `type`, `abstract=True`, `reset_value=None`, `width=1` | add a new register or wire |
| **LoopIntroduction** | a non-reset, non-combinational action carries `spec_statement: true` and a target variable is still abstract | ten required — [see below](#loopintroduction) | refine an abstract spec statement into an obligation-checked clocked loop |
| **ScheduleHandshakeFSM** | a non-reset action carries LoopIntroduction's `loop` marker (and no `state` register yet) | `action_name`; optional `state_var` / `done_var` / `start` | schedule the verified loop behind an IDLE/BUSY/DONE start/done handshake FSMD |

The combinational exclusions exist because a `combinational: True` action is
continuous logic (an `assign`) — never clocked, decomposed, or branched — and
memory-array / combinational-wire variables are never reset (the same carve-outs
`is_rtl_style` honours above).

The spec dict the rules operate on has this shape (see `base.py` and
[compilers.md](compilers.md#the-bridge) for the bridge that builds it from a
`FormalSpec`):

```jsonc
{
  "variables": [{"name", "type", "abstract", "reset_value", "clocked", "width",
                 "depth",            // memory array (register file / RAM) — never reset
                 "combinational"}],  // born-concrete wire driven by an `assign`
  "actions":   [{"name", "guard", "updates": [{"variable","expression"}],
                 "clocked", "is_rtl_style", "branches": [...], "sequential_steps": [...],
                 "combinational",                      // continuous logic, never clocked
                 "spec_statement", "postcondition",    // abstract Morgan spec statement
                 "refinement": {...}, "loop": {...}}], // post-LoopIntroduction markers
  "init": "...", "invariants": ["..."],
  "reset_action": "Reset" | null, "abstraction_mapping": {...}, "properties": [...]
}
```

The bridge sets `spec_statement: true` + `postcondition` from the matching
`FormalSpec` `Transition` fields and births the targeted variables **abstract** —
that is what arms `LoopIntroduction`. The `refinement` (obligation audit) and `loop`
(scheduling marker) fields appear only after a successful `LoopIntroduction`.

`Alternation` and `SequentialComposition` stash their structured `branches` /
`sequential_steps` on the action; the bridge composes them into one correct
next-state expression per variable (a nested ternary), so multi-branch logic is not
collapsed first-wins.

---

## Verified derivation: LoopIntroduction + ScheduleHandshakeFSM

The verified-derivation pair implements a **proved** abstract→concrete step: an
abstract Morgan specification statement (e.g. a multiplier's `product' = a * b`) is
refined into a concrete multi-cycle datapath only after the iteration-rule proof
obligations are mechanically discharged. This is the chain behind the FSMD
multiplier design class (first live verified derivation: run 102611 — chain
`LoopIntroduction → ScheduleHandshakeFSM → Initialization`, 3 picks, 0 strikes).
Both rules are pinned by `tests/test_verified_derivation.py`.

### LoopIntroduction

**File:** `rules/loop_introduction.py` — Morgan's iteration rule / Back's do–od
introduction (the Table-1 *Iteration* lineage in
[background.md](background.md#table-1--process-level-development), provisos included).

**Fires when** a non-reset, non-combinational action carries `spec_statement: true`
and at least one of the variables its `updates` name is still abstract (the bridge
arms this, as described above). Distinct from `Iteration`, which merely sets
`clocked = True` on an already-concrete action — LoopIntroduction *derives* the
concrete clocked body from an abstract postcondition, verified. Do not merge them.

**Params — all ten required** (`ValueError` on missing/malformed, which the engine
excludes): `action_name`, `postcondition`, `invariant`, `variant`, `guard`,
`init` (`{loop_var: expr}` load values), `body` (`{loop_var: expr}`, one
simultaneous read-before-write step), `mapping` (`{abstract_var: concrete_expr}`
data refinement at loop exit), `fresh_vars` (`[{name, width, type?, reset_value?}]`
new loop registers), `input_widths` (`{input: bit_width}` — the obligation domain).

**Obligation-gated apply.** `apply()` first calls `discharge_loop_obligations` (the
kernel below). On a **failed** discharge it returns an unchanged deepcopy of the
spec — a pure **no-op**, which trips the engine's no-op guard (see
[Backtracking](#backtracking)): the exact `(rule, params)` is excluded at that depth
and a strike is counted, so an unproven derivation can never enter the chain. On
**success** it installs the verified loop body as the action's guarded, clocked
updates, marks the mapping/body variables concrete, merges `mapping` into the spec's
`abstraction_mapping`, records the discharged audit on `action["refinement"]` —
`{invariant, variant, guard, mode, cases_checked, obligations}`, the chain-visible
certificate — and writes the scheduling marker `action["loop"] = {init, body,
variant, guard}` for ScheduleHandshakeFSM (`init`, the per-register load values, is
recorded nowhere else).

### ScheduleHandshakeFSM

**File:** `rules/schedule_handshake_fsm.py` — a **deterministic** scheduling
transform: no proof, no LLM. Soundness already lives in LoopIntroduction's
obligation gate; this rule only schedules the verified bare loop onto a control FSM.

**Fires when** a non-reset action carries the `loop` marker (and the default
`state` register is not yet declared — a defensive already-scheduled check). The
marker is **cleared** on schedule, so the rule is inert afterwards. Params:
`action_name` required; `state_var` (default `"state"`), `done_var` (default
`"done"`), `start` (default `"start"`) optional.

What it emits — the hardened IDLE(0)/BUSY(1)/DONE(2) start/done FSMD:

- **Load** fires on `((state = 0) OR (state = 2)) AND start = 1`. Accepting `start`
  in the 1-cycle DONE state is what makes back-to-back operation work — an
  IDLE-only load drops a start pulse that lands in DONE (the exact live-run bug
  this hardening fixed).
- Each loop register gets a load-on-start / step-in-BUSY / hold else-if chain.
- The body's own conditionals are **flattened** into that flat else-if chain, with
  the BUSY condition (`state = 1`) ANDed into each body-branch guard — Compiler 2's
  depth-0 IF-splitter recurses only into ELSE, so a nested IF left in a THEN
  position leaks untranslated into the Verilog.
- A combinational `done = (state = 2)` is appended as a `DoneFlag` action driving a
  born-concrete wire (`combinational: true`), and the 2-bit `state` register
  (reset `0`) is introduced.

### The obligation kernel

**File:** `pipeline/refinement/obligations.py` —
`discharge_loop_obligations(...) -> ObligationResult`

The kernel discharges the three Morgan/Back iteration obligations against the **real
expression semantics**: the evaluator is `pipeline.cocotb.spec_sim._eval` (bound
lazily to avoid an import cycle) — the exact grammar Compiler 2 emits, so a
derivation that discharges here is checked against the arithmetic the generated
Verilog will run.

| | Obligation | Checked as |
|---|---|---|
| **O1** | `pre ⇒ inv[init]` | the invariant holds after `init`, for every input |
| **O2** | `inv ∧ guard ⇒ inv[body]` ∧ variant decreases | the body preserves the invariant and the variant strictly decreases (termination, capped at `max_iters`) — walked over the **reachable** loop states per input, a documented limitation: not a symbolic proof over all invariant-satisfying states |
| **O3** | `inv ∧ ¬guard ⇒ post` | run to loop exit, bind the abstract variables via the data-refinement `mapping`, assert the postcondition |

The `mode` field is honest about proof strength: `"exhaustive-proof"` iff the
product of the input ranges is ≤ 65536 (the default `exhaustive_threshold`) — every
fixed-width input valuation is checked, a genuine finite proof over the declared
widths — else `"sampled"` (edge values plus a deterministic pseudo-random battery;
falsification only, never a proof). A failed obligation returns a concrete
counterexample `{obligation, inputs, state, detail}`; `ObligationResult` carries
`ok` / `mode` / `cases_checked` / `obligations` / `counterexample`.

**Native backend.** `discharge_loop_obligations(backend="auto" | "python" | "cpp")`
selects the implementation, never the verdict: under `"auto"` the
`OBLIGATIONS_BACKEND` env var may force a choice, else the optional compiled kernel
(`core/build.sh` → `pipeline.refinement._rtlcore`, C++17/pybind11) is used iff
built; `kernel_backend()` reports what `"auto"` resolves to, and pure Python is the
fallback. The native kernel is an **exact-verdict mirror** of the Python reference —
same enumeration order, same `mode`/`cases_checked`, byte-identical counterexamples,
pinned by `tests/test_native_kernel.py` — so chain replay is backend-independent. It
exists purely for speed: a 6-bit exhaustive proof drops 1.38 s → 6.8 ms (~205×) and
the full 8-bit 65,536-case proof 23.2 s → 74 ms (~311×), and these sweeps run on
**every** LoopIntroduction proposal, including failed ones during backtracking.

---

## Refinement driver: the catch-all pass

**File:** `pipeline/nodes/stage3.py` — single `engine.run()` with no `allowed_rule_names`

Stage 3 drives refinement with **one catch-all engine pass**: the base Agent-3 system
prompt, **all** rules available, run until `is_rtl_style` (cap `_CATCHALL_MAX_STEPS`).
A single `engine.run()` produces **one self-contained, replayable chain** — replaying it
from the initial abstract spec reconstructs the final RTL-style spec exactly.

### Retained-but-not-executed: the structured-pass schedule

An earlier design ran the engine over an ordered schedule of five **passes**, each
restricting the engine to a small `allowed` rule set with its own Agent-3 system prompt
(`pipeline/refinement_templates/passN_*.py`, driven by `stage3._PASS_CONFIGS`):

| Pass | Allowed rules | Purpose |
|---|---|---|
| `pass1_fsm` | `SequentialComposition`, `Iteration` | resolve control into an explicit FSM / clocked structure |
| `pass2_handshake` | `Alternation`, `IntroduceVariable` | add valid/ready handshake & backpressure branches |
| `pass3_datapath` | `Assignment`, `IntroduceVariable` | turn data variables into concrete registers with load/hold/clear |
| `pass4_reset` | `Initialization` | add synchronous reset to every state variable |
| `pass5_mapping` | `IntroduceVariable` | complete the abstraction mapping (supply missing symbols) |

This schedule is **no longer executed** — it is gated off by the module flag
`stage3._RUN_STRUCTURED_PASSES` (`False`). The `_PASS_CONFIGS` data structure and the
pass-template files are **retained** (pinned by `tests/test_pass_templates.py`, and
available for future re-enablement by flipping the flag).

**Why it was disabled.** The passes assumed every design needs every phase (a counter
has no handshake/datapath/mapping phase), so on simple designs Agent 3 was forced to
invent dead `IntroduceVariable`s — on the live 2-bit counter, 16 of 26 `pick_rule` calls
were junk. Worse, because each pass is a separate `engine.run()` with the same `run_id`,
the on-disk chain **accumulated** across passes (each pass appended its steps as a
committed prefix), but `IntroduceVariable`'s name-uniqueness check only sees the live
in-memory spec — not the cross-pass prefix. Two passes could therefore commit the same
variable name (`pass3` and `pass5` both added `count_concrete`), producing a chain that
**fails to replay from scratch**. Collapsing to a single catch-all run fixes this at the
root: with no prior passes the committed prefix is empty, the on-disk chain is exactly
one run's chain, and a duplicate name can never be committed within one run (`apply()`
raises → the engine excludes the choice and never appends it). After the catch-all,
Stage 3 applies the correctness critic below.

### The correctness critic

`pass6_checker` is **not** an engine pass — it is a one-shot Agent-3
[`critique_refinement`](agents.md#agent-3) call that independently checks the concrete
spec correctly refines the abstract spec under the proposed mapping. If the verdict is
not `accept`, Stage 3 writes a `partial` RTL artifact and **halts before Compiler 2** —
unverified RTL never reaches the testbench.

---

## The refinement chain

`refinement_chain.json` is an ordered list of committed steps:

```jsonc
{ "step": 0, "rule_name": "Initialization", "params": {...},
  "pre_hash": "<16-hex>", "post_hash": "<16-hex>" }
```

`pre_hash`/`post_hash` are SHA-256 prefixes of the spec before/after the step.
`_replay_chain(initial_spec, chain)` re-derives any state by re-applying steps from the
initial abstract spec — the chain is the proof trace, and replay is exact because every
rule is pure.

Alongside it, Stage 3 appends every `pick_rule` decision to
`refinement_decisions.jsonl` (`pipeline/nodes/stage3.py`): the chain records only
*committed* applies, while the decision log also captures what was offered and what
Agent 3 chose on calls the engine later rejected — so one metered live run leaves a
complete, offline-debuggable trace.

---

## Backtracking

When a pick is invalid or a depth dead-ends, the engine backtracks:

- **Invalid / repeated-bad picks** are counted per chain depth by an **integer**
  counter. After **3 strikes at a depth**, the engine backtracks. (The counter is an
  integer, not a set keyed on error text, so a picker that fails *identically* every
  call — the common LLM stall — still trips the threshold instead of spinning to
  `MAX_STEPS`.) Re-picking an already-excluded choice also counts as a strike, so a
  pure-of-spec picker cannot loop forever. A `pick_rule` that **throws** (unparseable
  LLM return, transport error, truncation) is counted as a strike under the same
  policy — one bad Agent-3 response never aborts the run.
- **No-op applications** are treated like invalid picks: when a step's `post_hash`
  equals its `pre_hash`, the exact `(rule, params)` is excluded at that depth and a
  strike is counted (backtrack after 3). This is the contract by which a failed
  [LoopIntroduction obligation discharge](#loopintroduction) — a pure no-op by
  design — surfaces to the engine, and it also stops a picker from spinning on an
  already-satisfied rule (e.g. Iteration on an already-clocked action).
- **`_backtrack`** pops the last committed step, records the popped `(rule, params)` as
  excluded at that depth, and replays the truncated chain to roll the spec back. If a
  depth's applicable rules are all excluded it rolls back further, up to
  `MAX_BACKTRACK_DEPTH = 5`; exhausting that raises `RefinementStall`.

At the Stage-3 level, a `refinement`-classified cocotb failure triggers
`run_stage3_backtrack_refinement`: it keeps the (correct) FormalSpec, truncates the
chain by a fixed number of steps (saving the prefix to
`refinement_chain_prefix.json`), replays to the truncation point, injects the
diagnosis into the `pick_rule` prompt, and re-runs the engine.

---

## Adding a rule

Use the `/add-refinement-rule <Name>` command, which scaffolds the rule file,
registers it in `RULE_REGISTRY`, and updates the docs. Keep `apply()` pure, add a
purity test (assert both the input `spec` and `params` are unmutated, not just
double-call identity), and record the rule's calculus lineage in
[background.md](background.md).
