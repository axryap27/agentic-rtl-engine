Run TLC model checker on the TLA+ spec generated for a pipeline run.

Run ID: $ARGUMENTS

## Steps

1. Find `.tla` and `.cfg` files under `artifacts/<run_id>/`. There should be one pair; report an error if multiple or none are found.

2. Run TLC. Try in order:
   - `tlc <spec>.tla -config <spec>.cfg`
   - `java -jar ~/tla2tools.jar <spec>.tla -config <spec>.cfg`

   Note which command was used. If neither is available, report it and stop.

3. Report:
   - Exit code (0 = passed, non-zero = failed)
   - Number of distinct states explored (from TLC output)
   - Whether all invariants and properties passed
   - Full error output if TLC failed

4. If TLC failed, extract the lines most useful for an Agent 3 retry prompt: the invariant name that was violated, the counterexample state, and the error message. Quote them clearly so they can be pasted directly into the `"tlc_errors"` field of the next prompt.
