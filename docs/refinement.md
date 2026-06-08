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
6. every variable has a non-`None` `reset_value`;
7. every non-reset action has `clocked == True`;
8. every non-reset action has non-empty `updates`.

---

## The six Tier-1 rules

**File:** `pipeline/refinement/rules/` · `RULE_REGISTRY` registers these six, in order.
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
| **Initialization** | a variable lacks a `reset_value`, or no `reset_action` is set | `reset_values: {var → expr}`, `reset_action_name="Reset"` | synchronous reset: every register gets a known start value |
| **Iteration** | a non-reset action has `clocked == False` | `action_name` | clock the action — its body becomes a per-cycle register update |
| **SequentialComposition** | a non-reset action has neither `sequential_steps` nor `branches` | `action_name`, `steps: [{name, guard, updates}]` | ordered combinational steps within one cycle |
| **Assignment** | a non-reset action has empty `updates` | `action_name`, `updates: [{variable, expression}]` | concrete register write(s) |
| **Alternation** | a non-reset action has no `branches` | `action_name`, `branches: [{guard, updates}]` | mutually-exclusive guarded branches (if / case / mux) |
| **IntroduceVariable** | always (engine checks name uniqueness) | `name`, `type`, `abstract=True`, `reset_value=None`, `width=1` | add a new register or wire |

The spec dict the rules operate on has this shape (see `base.py` and
[compilers.md](compilers.md#the-bridge) for the bridge that builds it from a
`FormalSpec`):

```jsonc
{
  "variables": [{"name", "type", "abstract", "reset_value", "clocked", "width"}],
  "actions":   [{"name", "guard", "updates": [{"variable","expression"}],
                 "clocked", "is_rtl_style", "branches": [...], "sequential_steps": [...]}],
  "init": "...", "invariants": ["..."],
  "reset_action": "Reset" | null, "abstraction_mapping": {...}, "properties": [...]
}
```

`Alternation` and `SequentialComposition` stash their structured `branches` /
`sequential_steps` on the action; the bridge composes them into one correct
next-state expression per variable (a nested ternary), so multi-branch logic is not
collapsed first-wins.

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

---

## Backtracking

When a pick is invalid or a depth dead-ends, the engine backtracks:

- **Invalid / repeated-bad picks** are counted per chain depth by an **integer**
  counter. After **3 strikes at a depth**, the engine backtracks. (The counter is an
  integer, not a set keyed on error text, so a picker that fails *identically* every
  call — the common LLM stall — still trips the threshold instead of spinning to
  `MAX_STEPS`.) Re-picking an already-excluded choice also counts as a strike, so a
  pure-of-spec picker cannot loop forever.
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
