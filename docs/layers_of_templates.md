# Layers of Refinement Templates

This document defines a compiler-like LLM refinement workflow that prevents direct jumps from abstract TLA+ to arbitrary detailed specs.

The core principle is:

- each stage performs exactly one refinement move,
- each stage has a strict contract,
- each stage is gated by a checker before the next stage runs.

---

## Design Goals

- Prevent large, unconstrained rewrites.
- Make failures attributable to a single refinement stage.
- Preserve behavioral equivalence through explicit abstraction mappings.
- Produce TLC-checkable obligations at every step.
- Separate generation from criticism to reduce self-approval bias.

---

## Pass Contract (Required for Every Template)

Every refinement template must define:

- `preconditions`: what must already hold for this pass to run.
- `postconditions`: what this pass guarantees on success.
- `frame_condition`: what is allowed to change.
- `failure_mode`: return `blocked` with reasons when preconditions do not hold.

Each pass output must include:

```json
{
  "pass_name": "",
  "rule_used": "",
  "status": "success|blocked|fail",
  "diff_summary": "",
  "changed_symbols": [],
  "unchanged_symbols": [],
  "artifact": {},
  "abstraction_mapping_delta": {},
  "obligations": {
    "init_obligations": [],
    "next_obligations": [],
    "invariants": [],
    "tlc_properties": []
  },
  "diagnostics": []
}
```

Notes:

- `artifact` contains transformed spec fragments only.
- `obligations` contains proof/check content only.
- `changed_symbols` and `unchanged_symbols` enforce "refine one part only."

---

## Pipeline Overview

```text
Prompt
  -> Agent 1: Abstract TLA+ spec
  -> Pass 1: FSM refinement
  -> Pass 2: Handshake/backpressure refinement
  -> Pass 3: Datapath/register refinement
  -> Pass 4: Reset/initialization refinement
  -> Pass 5: Mapping completion
  -> Pass 6: Refinement checker
  -> TLC refinement checks
  -> RTL generation
```

After each pass:

1. Proposer agent emits pass JSON.
2. Critic/checker agent validates contract and obligations.
3. Gate decides `pass` or `retry`.
4. Only then move to next pass.

---

## Stage Template 1: FSM Refinement

Use when abstract actions are too high-level and control states are implicit.

### Allowed rules

- Sequential Composition
- Iteration

### Inputs

- Abstract TLA+ action(s)
- Current variables
- Required output schema

### Instructions

1. Identify one abstract action to refine.
2. Propose explicit FSM states.
3. For each state define guard, update, next-state.
4. Do not introduce datapath details unless required.
5. Preserve original behavior via abstraction mapping.
6. Emit TLC obligations.

### Output artifact schema

```json
{
  "abstract_action": "",
  "new_states": [],
  "new_variables": [],
  "concrete_actions": [],
  "abstraction_mapping": {},
  "tlc_obligations": []
}
```

### Contract

- Preconditions:
  - action exists and lacks explicit control-state decomposition.
- Postconditions:
  - explicit control states and transition relation are introduced.
- Frame condition:
  - control-state variables/actions may change; datapath semantics must not.
- Failure mode:
  - `blocked` if action cannot be separated without datapath assumptions.

---

## Stage Template 2: Handshake/Backpressure Refinement

Use after FSM states exist but valid/ready semantics are missing.

### Allowed rules

- Strengthen During
- Piping Composition

### Inputs

- Concrete action(s) from FSM stage
- Interface signals requiring handshake
- Required output schema

### Instructions

1. Add valid/ready signals.
2. Define fire condition (`valid && ready` style).
3. Define stall condition (`valid && !ready` style).
4. Define stability constraints during stall.
5. Define when FSM is allowed to advance.
6. Preserve prior abstraction mapping.

### Output artifact schema

```json
{
  "refined_action": "",
  "new_signals": [],
  "fire_condition": "",
  "stall_condition": "",
  "stability_requirements": [],
  "updated_tla_actions": [],
  "tlc_obligations": []
}
```

### Contract

- Preconditions:
  - explicit FSM control exists.
- Postconditions:
  - handshake semantics are explicit and transition legality is constrained.
- Frame condition:
  - handshake/interface behavior may change; high-level function must not.
- Failure mode:
  - `blocked` if interface role (producer/consumer) is undefined.

---

## Stage Template 3: Datapath/Register Refinement

Use when abstract values must become concrete storage/register behavior.

### Allowed rules

- Data Refinement
- Introduce Variable
- Assignment

### Inputs

- Abstract variables
- Current concrete states/actions
- Required output schema

### Instructions

1. Select abstract variables needing concrete storage.
2. Introduce registers/buffers.
3. Define when each register is loaded, held, cleared.
4. Update abstraction mapping.
5. Do not alter control behavior except required register updates.

### Output artifact schema

```json
{
  "abstract_variables_refined": [],
  "new_registers": [],
  "register_update_rules": {},
  "abstraction_mapping": {},
  "tlc_obligations": []
}
```

### Contract

- Preconditions:
  - control structure exists and data variables are still abstract.
- Postconditions:
  - storage model is concrete and update points are explicit.
- Frame condition:
  - datapath/storage definitions may change; control sequencing should remain.
- Failure mode:
  - `blocked` if required load/store events are missing from control stage.

---

## Stage Template 4: Reset/Initialization Refinement

Use before RTL generation to avoid unconstrained initial-state behavior.

### Allowed rule

- Initialization

### Inputs

- All spec variables (control, datapath, interface)
- Current init/reset semantics (if any)
- Required output schema

### Instructions

1. Add reset action/input.
2. Assign reset values for all state variables.
3. Ensure reset state satisfies abstract `Init`.
4. Define behavior for mid-transaction reset.
5. Emit reset-related TLC obligations.

### Output artifact schema

```json
{
  "reset_variables": {},
  "reset_action": "",
  "abstraction_mapping_after_reset": {},
  "tlc_obligations": []
}
```

### Contract

- Preconditions:
  - state variables and transitions are already concrete enough to reset.
- Postconditions:
  - full reset semantics are explicit and compatible with abstract init.
- Frame condition:
  - initialization/reset behavior may change; steady-state functional behavior must not.
- Failure mode:
  - `blocked` if variable ownership/update domains are ambiguous.

---

## Stage Template 5: Mapping Completion Pass

Use to ensure the abstraction relation is total and checker-ready.

### Allowed rules

- Mapping completion only (no behavioral rewrite)

### Inputs

- Abstract spec symbols
- Current concrete spec symbols
- Existing partial mapping

### Instructions

1. Ensure every abstract variable has a concrete expression mapping.
2. Mark intentional stuttering conditions.
3. Define mapping constraints required by invariants.
4. Do not change transition behavior.

### Output artifact schema

```json
{
  "mapping_complete": false,
  "new_mappings": {},
  "missing_mappings": [],
  "possible_stuttering_steps": [],
  "required_invariants": []
}
```

### Contract

- Preconditions:
  - concrete behavior is present (control, handshake, datapath, reset).
- Postconditions:
  - mapping coverage is complete or explicit gaps are reported.
- Frame condition:
  - mapping/invariant metadata only; behavior unchanged.
- Failure mode:
  - `blocked` if concrete symbol definitions are inconsistent.

---

## Stage Template 6: Refinement Checker Pass (Separate Agent)

This pass must be run by a checker/critic agent distinct from the generator.

### Inputs

- Abstract spec
- Concrete spec
- Proposed refinement mapping

### Instructions

1. Check mapping completeness.
2. Check `ConcreteInit => AbstractInit`.
3. Check each `ConcreteNext` step maps to abstract step or allowed stutter.
4. Identify missing invariants.
5. Produce TLC-checkable properties.
6. Do not assume correctness without explicit checks.

### Output schema

```json
{
  "mapping_complete": true,
  "missing_mappings": [],
  "init_obligations": [],
  "next_obligations": [],
  "possible_stuttering_steps": [],
  "required_invariants": [],
  "tlc_properties": [],
  "verdict": "pass|fail|unknown",
  "diagnostics": []
}
```

---

## Gate Between Passes

Before advancing to the next stage, run a gate checker that verifies:

- schema validity,
- pass preconditions/postconditions,
- frame-condition compliance,
- allowed-rule compliance,
- abstraction-mapping consistency,
- obligations emitted and non-empty where required.

If gate fails:

- retry the same pass only,
- include failure diagnostics in prompt,
- prohibit edits outside the pass frame condition.

---

## Operational Roles

- **Proposer agent**: generates pass output.
- **Critic agent**: validates contract and flags defects; cannot rewrite.
- **Repair agent (optional)**: edits only critic-flagged fields.
- **TLC runner**: executes generated obligations/properties.

This separation ensures generator and checker incentives are distinct.

---

## Why This Works

- One-pass-at-a-time refinement localizes errors.
- Strict contracts constrain LLM behavior to compiler-like moves.
- Gate checks prevent error propagation across stages.
- Trace artifacts provide a debuggable proof trail from abstract to RTL-style TLA+.

This structure is the intended design for `layers_of_templates`: deterministic refinement progression with explicit verification boundaries.
