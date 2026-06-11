# The Compilers

Two deterministic compilers bracket the refinement engine. **Compiler 1** turns the
LLM-authored formal spec into model-checkable TLA+; **Compiler 2** turns the refined,
RTL-style spec into synthesizable Verilog-2001. Neither calls an LLM. Between them sits
the **bridge**, which translates the engine's spec format to and from TLA+ text.

```
FormalSpec ──Compiler 1──► abstract TLA+ + .cfg ──► TLC (optional model check)
   │
   └─bridge: formal_spec_to_engine_spec──► engine spec ──Refinement Engine──► RTL-style engine spec
                                                                                    │
                                          bridge: engine_spec_to_rtl_tla◄───────────┘
                                                       │
                                                       ▼
                                            RTL-style TLA+ ──Compiler 2──► Verilog-2001 (output.v)
```

---

## Compiler 1

**File:** `pipeline/compilers/compiler1.py` · `compile(formal_spec: FormalSpec) -> (tla_source, cfg_source)`

Templates a `FormalSpec` (JSON(TLA), authored by Agent 3) into an abstract TLA+ module
plus a TLC `.cfg`, suitable for model checking during Stage 3. The `FormalSpec` uses
plain-English boolean operators so the LLM can author it reliably; Compiler 1
translates them to TLA+:

| FormalSpec | TLA+ |
|---|---|
| `AND` | `/\` |
| `OR` | `\/` |
| `NOT` | `~` |
| `=`, `/=`, `<`, `>`, `<=`, `>=`, `TRUE`, `FALSE` | pass through |

Invariants are named `Invariant` when there is one, or `Inv0`, `Inv1`, … `InvN-1` when
there are several (the `.cfg` lists each as an `INVARIANT`). If `FormalSpec.raw_tla` is
set, Compiler 1 emits it verbatim and only generates the `.cfg` from the structured
fields — an escape hatch for hand-written TLA+.

An update **key** may name a single memory-array element (`mem[waddr]` — a
register-file write). Compiler 1 renders it via the TLA+ function-update form
`mem' = [mem EXCEPT ![waddr] = expr]` (`mem[i]' = e` is not legal TLA+), uses the
**base** array name when building each action's `UNCHANGED` tuple (so the array never
lands in `UNCHANGED` alongside its own EXCEPT update — a contradiction), and skips the
scalar range constraint for a memory (`depth` set): `mem \in 0..255` would be wrong
for a function-valued variable, so element widths are enforced in the generated RTL
instead.

The `FormalSpec` schema (`pipeline/schemas/tla_schema.py`):

```python
class Variable(BaseModel):    type: str          # "Nat" | "Bit"
                              width: int          # bit width → range constraint
                              depth: Optional[int] = None  # set → MEMORY ARRAY of depth
                                                  # words (register file / RAM): emitted
                                                  # reg [w-1:0] name [0:depth-1], never a
                                                  # port, never reset
class Transition(BaseModel):  label: str          # action name (matches TLA+ label)
                              condition: str       # AND/OR/NOT enabling condition
                              updates: dict[str, str]   # var → next-value expression; a
                                                  # KEY may be an indexed memory-element
                                                  # write ("mem[waddr]")
                              combinational: bool = False  # True → continuous (assign)
                                                  # logic: wire targets, never clocked,
                                                  # never reset
                              spec_statement: bool = False # True → abstract Morgan spec
                                                  # statement; targets born abstract so
                                                  # LoopIntroduction fires
                              postcondition: Optional[str] = None  # the abstract post the
                                                  # derived loop must establish (used with
                                                  # spec_statement=True)
class FormalSpec(BaseModel):  module_name: str
                              description: str
                              variables: dict[str, Variable]
                              initial: dict[str, str]
                              transitions: list[Transition]
                              invariants: list[str]
                              raw_tla: str | None = None
```

---

## The bridge

**File:** `pipeline/refinement/bridge.py`

The engine works on a plain dict, not on `FormalSpec` or TLA+ text. The bridge
translates in both directions:

- **`formal_spec_to_engine_spec(spec)`** — `FormalSpec` → the engine's spec dict. Every
  variable starts `abstract=True` with no `reset_value`; the refinement rules add reset
  values, clocking, etc. Declared bit widths are carried through so signals are sized
  correctly downstream.
- **`engine_spec_to_rtl_tla(engine_spec, module_name, port_widths=None)`** — the refined
  engine spec → RTL-style TLA+ text for Compiler 2. This is where two subtle but
  important behaviors live:
  - **Free-input declaration & sizing.** An identifier that appears only in a guard or
    update expression (e.g. `d` on a flip-flop, `op` on an ALU) is a *free input* — it
    is never a declared variable, so without help Compiler 2 would emit Verilog
    referencing an undeclared wire. The bridge scans for these and injects them into
    the `VARIABLES` block, sized via, in priority order: (1) a `port_widths` hint
    threaded from the Stage-1 `SpecSummary`; (2) inference from a register it feeds
    directly (`data' = din` → `din` inherits `data`'s width); (3) default 1.
  - **Guard gating.** A clocked action guarded by a non-trivial condition (e.g.
    `en = 1`) must only update its registers when the guard holds. The bridge weaves
    the guard into the next state as `IF <not guard> THEN <var> ELSE <update>` (a
    negated-guard ELSE-chain, kept flat so Compiler 2 renders a clean nested ternary).
- **`engine_spec_to_abstract_tla(engine_spec, module_name)`** — emits abstract TLA+
  (`Init`/`Next`/invariants) for mid-refinement TLC checking, distinct from the
  RTL-style form Compiler 2 consumes.

---

## Compiler 2

**File:** `pipeline/compilers/compiler2.py` ·
`compile_tla_to_verilog(tla_source, module_name) -> str` (class `RTLTLACompiler`)

A **structural translator**, not a general TLA+ interpreter. It looks for exactly three
named sections and ignores everything else (`Init`, `Next`, `Spec`, `CONSTANTS`,
quantifiers are verification-only and skipped):

| TLA+ section | Compiled to |
|---|---|
| `VARIABLES` | port declarations + internal `reg` declarations |
| `CombinationalLogic` | `assign` statements |
| `UpdatePipeline` | one `always @(posedge clk)` block with synchronous reset |

The emitted module begins with `` `timescale 1ns / 1ps `` so simulators can represent
the cocotb clock, then the inferred port list, internal registers, combinational
`assign`s, and the clocked block.

### Port inference

Ports are inferred from how a variable is driven and its name prefix — no annotations:

| Variable | Rule | Declaration |
|---|---|---|
| `clk`, `reset` | always | `input clk` / `input reset` |
| `hw_*` | verification-only | dropped entirely |
| `r_*` | internal register prefix | `reg` inside the module body |
| driven by `CombinationalLogic` | combinational output | `output` (wire, driven by `assign`) |
| driven by `UpdatePipeline` (non-`r_*`) | sequential output | `output reg` |
| not driven by either block | externally supplied | `input` |

### Width handling

Each `VARIABLES` entry carries its width as a TLA+ comment (`\* width: N`), invisible to
TLC. Compiler 2 captures that width before stripping comments and a `_range()` helper
emits a `[N-1:0]` prefix when `N > 1` (scalar otherwise). This carries declared widths
end-to-end so multi-bit signals are not silently truncated to one bit.

### Expression translation

Every right-hand side goes through a recursive translator:

| TLA+ | Verilog |
|---|---|
| `/\` / `\/` | `&&` / `\|\|` |
| `/=` | `!=` |
| `=` (in expressions) | `==` |
| `IF a THEN b ELSE c` | `(a) ? (b) : (c)`, applied recursively |
| `Append(...)`, `<< >>` | `/* FORMAL_ONLY */` (dropped) |

The `UpdatePipeline` parser splits the reset skeleton at the standalone `ELSE` line
(distinguishing the outer reset/normal branch from inner ternary `ELSE`s), emitting
reset assignments under `if (reset)` and the rest under `else`.

### Safety checks at codegen

- **The banlist** (below) rejects any non-Verilog-2001 / leaked-TLA+ construct.
- **Undeclared-input guard** (`_undeclared_inputs`) scans the translated RHS of every
  emitted assignment and declares any still-undeclared identifier as a scalar input, so
  the module always elaborates even when the bridge is bypassed (hand-written TLA+).
- **Multi-driver guard** raises `MultiDriverError` if a variable would be driven by
  both a combinational `assign` and a clocked block.

---

## The banlist

Compiler 2 enforces **Verilog-2001 only** at codegen time via `verify_banlist`, run
*before* the source is returned — nothing banned ever reaches disk. It rejects:

- SystemVerilog / non-synthesizable tokens: `logic`, `always_ff`, `always_comb`,
  `always_latch`, `interface`, `modport`, `typedef`, `initial`, `#`-delays, `$`-system
  tasks;
- leaked uppercase TLA+ keywords (`IF`/`THEN`/`ELSE`/`IN`/`CASE`/`LET`) and a bare
  `FORMAL_ONLY`, which indicate an untranslated expression.

Ban-words appearing inside comment headers do not trigger a false positive. This is a
build-time check, not a prompt-retry loop — see invariant 3 in
[architecture.md](architecture.md#6-the-four-design-invariants).

Lint the result with `verilator --lint-only output.v` or
`iverilog -Wall -t null output.v` (the `/lint-rtl` command wraps this).
