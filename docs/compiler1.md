# compiler1: RTL-style TLA+ → Verilog-2001

`pipeline/compilers/compiler1.py` is the Stage 3 code-generation step. It takes an RTL-style TLA+ specification — the output of the refinement pipeline — and compiles it to synthesizable Verilog-2001.

---

## What it does (and doesn't do)

The compiler is a **structural translator**, not a general TLA+ interpreter. It looks for three named sections in the spec and ignores everything else. The `Next` and `Spec` formulas, the `Init` block, and any `CONSTANTS` or quantified expressions are formal-verification constructs and are completely skipped.

| TLA+ section | Compiled to |
|---|---|
| `VARIABLES` | Port declarations and internal `reg`/`wire` declarations |
| `CombinationalLogic` | `assign` statements |
| `UpdatePipeline` | `always @(posedge clk)` block with synchronous reset |
| `Init`, `Next`, `Spec`, `\E ...` | ignored |

---

## Expected TLA+ format

The spec must follow this skeleton exactly. Deviating from the structure (e.g. omitting the standalone `ELSE` line, or splitting `CombinationalLogic` into two definitions) will cause sections to be silently skipped.

```tla
---- MODULE ModuleName ----
EXTENDS Integers[, Sequences, ...]

[CONSTANTS ...]           \* optional; compiler ignores

VARIABLES
    clk, reset,           \* always module input ports
    in_valid, in_a, in_b, \* not driven by module → inferred as input ports
    in_ready,             \* driven by CombinationalLogic → inferred as output port
    out_valid, out_data,  \* driven by CombinationalLogic → inferred as output ports
    out_ready,            \* not driven by module → inferred as input port
    r_stg1_valid,         \* r_* prefix → always internal reg, never a port
    r_stg1_mult,          \* r_* prefix → always internal reg
    hw_in_history         \* hw_* prefix → dropped entirely

CombinationalLogic ==
    /\ in_ready'  = <expr>
    /\ out_valid' = <expr>
    /\ out_data'  = <expr>

UpdatePipeline ==
    /\ clk' = ...                   \* always skipped
    /\ IF reset = 1 THEN
          /\ r_stg1_valid' = 0
          ...
       ELSE
          /\ r_stg1_valid' = IF <cond> THEN <val> ELSE <val>
          ...

Next == /\ CombinationalLogic /\ UpdatePipeline /\ \E ...   \* ignored
Spec == Init /\ [][Next]_hw_vars                             \* ignored
====
```

---

## Port inference

Ports are inferred automatically from two signals: which block drives a variable, and its name prefix. No annotations are required.

| Variable | Rule | Verilog declaration |
|---|---|---|
| `clk`, `reset` | always fixed | `input clk` / `input reset` |
| `hw_*` | verification-only | dropped entirely |
| `r_*` | internal register prefix | `reg` declared inside module body |
| driven by `CombinationalLogic` | module drives it combinationally | `output` port (wire, driven by `assign`) |
| driven by `UpdatePipeline` (non-`r_*`) | module drives it sequentially | `output reg` port |
| not driven by either block | externally supplied | `input` port |

Using the sample spec, this produces:

```verilog
module pipeline_processor (
    input  clk,
    input  reset,
    input  in_valid,      // not driven → input
    input  in_a,          // not driven → input
    input  in_b,          // not driven → input
    input  out_ready,     // not driven → input
    output in_ready,      // CombinationalLogic drives in_ready' → output wire
    output out_valid,     // CombinationalLogic drives out_valid' → output wire
    output out_data       // CombinationalLogic drives out_data' → output wire
);

    // Internal registers (r_* prefix, never exposed as ports)
    reg  r_stg1_valid;
    reg  r_stg1_mult;
    reg  r_stg2_valid;
    reg  r_stg2_acc;
    ...
```

---

## Expression translation

Every right-hand-side expression goes through the translator:

| TLA+ | Verilog |
|---|---|
| `/\` | `&&` |
| `\/` | `\|\|` |
| `/=` | `!=` |
| `=` (inside expressions) | `==` |
| `IF a THEN b ELSE c` | `(a_v) ? (b_v) : (c_v)` |
| `Append(...)`, `<< >>` | `/* FORMAL_ONLY */` (dropped) |

### Nested IF-THEN-ELSE

The most common pattern in `UpdatePipeline` is a chained conditional:

```tla
r_stg1_valid' = IF (in_valid = 1 /\ in_ready = 1) THEN 1
                ELSE IF (out_ready = 1) THEN 0
                ELSE r_stg1_valid
```

The translator processes this recursively. It scans character by character at parenthesis depth 0 to find the `THEN` and `ELSE` keywords that belong to the outermost `IF`, then recurses on the `ELSE` branch. The result is a nested ternary:

```verilog
((in_valid == 1 && in_ready == 1)) ? (1) : (((out_ready == 1)) ? (0) : (r_stg1_valid))
```

Multi-line expressions are joined before translation: any line that does not start with `/\` is treated as a continuation of the previous conjunct.

---

## The reset block

The `UpdatePipeline` parser splits the block into two sections by finding the standalone `ELSE` line — a line whose entire non-whitespace content is just `ELSE`. This distinguishes the outer reset-vs-normal branch from inner `ELSE` clauses that appear inside ternary expressions.

Reset assignments are emitted in the `if (reset)` branch; normal assignments are emitted in the `else` branch.

---

## Output structure

```verilog
module <name> (
    input  clk,
    input  reset,
    input  in_valid,      // inferred inputs
    input  in_a,
    input  out_ready,
    output in_ready,      // inferred output wires (driven by assign)
    output out_valid,
    output out_data
);

    // Internal registers (r_* prefix)
    reg  r_stg1_valid;
    reg  r_stg1_mult;
    ...

    // Combinational logic
    assign in_ready  = (r_stg1_valid == 0 || r_stg2_ready);
    assign out_valid = r_stg2_valid;
    ...

    // Clocked pipeline evolution
    always @(posedge clk) begin
        if (reset) begin
            r_stg1_valid <= 0;
            ...
        end else begin
            r_stg1_valid <= ((in_valid == 1 && in_ready == 1)) ? (1) : (...);
            ...
        end
    end

endmodule
```

The output is Verilog-2001 only. No `logic`, no `always_ff`, no `always_comb`, no `initial` blocks.

---

## Running the compiler

```bash
# Compile a spec file
python3 pipeline/compilers/compiler1.py path/to/spec.tla [module_name]

# Test against the built-in sample spec
python3 pipeline/compilers/compiler1.py --sample
```

Or use it as a library:

```python
from pipeline.compilers.compiler1 import RTLTLACompiler

with open("artifacts/<run_id>/spec.tla") as f:
    tla = f.read()

verilog = RTLTLACompiler(tla).compile(module_name="my_core")
```

---

## Limitations

- **Single `CombinationalLogic` and `UpdatePipeline` block.** If the spec splits logic across multiple definitions, only the ones with those exact names are compiled.
- **No bit-width inference.** All signals are declared without explicit widths (`reg` not `reg [7:0]`). Width annotation must be added in a post-processing pass or by the LLM in Stage 3.
- **Internal register prefix is `r_`.** Any register that should not be a port must use the `r_` naming convention. Other prefixes are not currently recognised as internal.
- **Formal-only constructs produce `/* FORMAL_ONLY */`.** Any `Append(...)` or sequence literal that slips into a compiled section emits a comment rather than valid Verilog. Inspect the output before synthesis.
