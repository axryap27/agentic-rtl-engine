Validate all JSON artifacts for a pipeline run against their Pydantic v2 schemas.

Run ID: $ARGUMENTS

## Steps

1. List all files present under `artifacts/<run_id>/`.

2. For each of the five stage artifacts that exists, load and validate it:

   | File | Pydantic model in `pipeline/schemas.py` |
   |------|-----------------------------------------|
   | `00_nl_spec.json` | `NLSpec` |
   | `01_formal_spec.json` | `FormalSpec` |
   | `02_pluscal_impl.json` | `PluscalImpl` |
   | `03_rtl_output.json` | `RTLOutput` |
   | `04_evaluation.json` | `Evaluation` |

   Use `Model.model_validate(json.loads(content))` for each. Skip files that don't exist yet.

3. For each artifact, report:
   - Present: yes/no
   - Schema valid: yes / no (with the Pydantic `ValidationError` message if invalid)
   - `status` field value (required on all artifacts except `00_nl_spec.json`)

4. Also check `refinement_chain.json` if present: confirm it is a valid JSON list of `[rule_name, params]` pairs.

5. Print a final summary table: which artifacts are present, valid, and what their status is.
