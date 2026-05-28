Create a new refinement rule for the agentic-rtl-engine project.

Rule name (PascalCase): $ARGUMENTS

## Steps

1. **Create the rule file** at `pipeline/refinement/rules/<snake_case_name>.py`.

   The class must subclass `RefinementRule` from `base.py` and implement all three required methods:

   ```python
   from pipeline.refinement.rules.base import RefinementRule

   class <Name>Rule(RefinementRule):
       def is_applicable(self, spec: dict) -> bool:
           # Return True if this rule can fire on the current spec.
           ...

       def apply(self, spec: dict, params: dict) -> dict:
           # Apply the rule deterministically. Returns the refined spec.
           # Must be pure — no side effects, same inputs always give same output.
           ...

       def describe(self) -> str:
           return "<one-line description for the Rule Picker LLM prompt>"
   ```

   Use the hardware meaning from `docs/architecture.md` to inform the implementation. Look at existing rule files for patterns.

2. **Register the rule** in `pipeline/refinement/engine.py` (import and add to the rule registry list).

3. **Add a row** to `docs/refinement_rules.md` under the appropriate table (Table 1 for process-level rules, Table 2 for control/data flow rules). Use the same symbol conventions already in the file (`⊑`, `⇒`, `∧`, `_a` subscript style).

4. **Report** what files were created or modified and show the full content of the new rule file.
