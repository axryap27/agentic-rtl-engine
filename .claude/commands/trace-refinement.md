Pretty-print the refinement chain for a pipeline run, showing each rule application step with its hardware meaning.

Run ID: $ARGUMENTS

## Steps

1. Read `artifacts/<run_id>/refinement_chain.json`. If it doesn't exist or is empty, say so and stop — suggest running Stage 2 first.

2. For each entry in the chain (ordered list of rule applications), print a formatted step block:

   ```
   Step N: <RuleName>
     Params:  <params dict>
     Hardware meaning: <one-line description from docs/architecture.md>
   ```

   Use the hardware meanings from `docs/architecture.md` (Tier-1 rules table):
   - `Initialization` → Reset behavior: every register gets a default value
   - `Iteration` → Free-running clocked loop: the body is the per-cycle update
   - `SequentialComposition` → Combinational path within one clock cycle
   - `Assignment` → Register update in an always block
   - `Alternation` → Mux / case / FSM branch
   - `IntroduceVariable` → Adds a new register or internal wire

3. Print a one-line summary at the end:
   ```
   Total steps: N  |  Rules used: <comma-separated unique list>
   ```

4. If `artifacts/<run_id>/02_pluscal_impl.json` exists, read it and print:
   - The `status` field
   - The list of state variables in the final RTL-style spec (if present in the JSON)
   - Whether the spec is marked as RTL-style / synthesis-ready
