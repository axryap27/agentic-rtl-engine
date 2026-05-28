Lint-check the generated Verilog for a pipeline run.

Run ID: $ARGUMENTS

## Steps

1. Find `.v` file(s) under `artifacts/<run_id>/`. Report an error if none are found.

2. Run the linter. Try in order:
   - `verilator --lint-only <file>.v`
   - `iverilog -Wall -t null <file>.v`

   Note which tool was used. If neither is available, report it and stop.

3. Report:
   - Exit code (0 = clean, non-zero = errors/warnings)
   - Full linter output

4. If lint failed, categorize the output:
   - **Errors** (synthesis-blocking): list each one with its line number
   - **Warnings**: list separately

5. Check the Verilog source for SystemVerilog constructs that are banned per project constraints (Verilog-2001 only):
   - `logic` keyword
   - `always_ff`, `always_comb`, `always_latch`
   - `interface`, `modport`, `typedef`

   Report any found with their line numbers even if the linter did not flag them.

6. If there are errors, quote the lines most useful for a Stage 3 retry prompt so they can be pasted into `"lint_errors"`.
