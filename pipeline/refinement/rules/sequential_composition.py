import copy
from .base import RefinementRule


class SequentialComposition(RefinementRule):
    """
    Table 2 — Sequential Composition.

    Formal: w : [pre, dur, post] ⊑ w : [pre, dur, mid]; w : [mid, dur_j, post]
    Conditions: (i) w0, x0 ∉ vars(mid), (ii) pre ∧ dur ⇒ mid,
                (iii) mid ∧ dur_j ⇒ post.
    Hardware role: splits a combinational path into ordered sub-steps within
    one clock cycle, e.g. compute → latch → compare.

    Required params:
        action_name (str): name of the action to decompose.
        steps (list[dict]): ordered list of sub-steps, each with:
            - name (str): label for this step.
            - guard (str): TLA+ guard (first step inherits original guard).
            - updates (list[dict]): list of {variable, expression} assignments.
    """

    def is_applicable(self, spec: dict) -> bool:
        return any(
            not a.get("sequential_steps")
            and not a.get("branches")
            for a in spec.get("actions", [])
            if a["name"] != spec.get("reset_action")
        )

    def apply(self, spec: dict, params: dict) -> dict:
        action_name: str = params["action_name"]
        steps: list[dict] = params["steps"]

        result = copy.deepcopy(spec)

        for action in result.get("actions", []):
            if action["name"] == action_name:
                # Carry the original guard into the first step if not supplied.
                if steps and not steps[0].get("guard"):
                    steps[0]["guard"] = action.get("guard", "TRUE")
                action["sequential_steps"] = copy.deepcopy(steps)
                # Replace the flat updates with the union of all step updates
                # so the action still represents its full effect.
                merged_updates = []
                seen = set()
                for step in steps:
                    for upd in step.get("updates", []):
                        if upd["variable"] not in seen:
                            merged_updates.append(upd)
                            seen.add(upd["variable"])
                action["updates"] = merged_updates
                break

        return result

    def describe(self) -> str:
        return (
            "SequentialComposition: decompose one abstract action into an "
            "ordered sequence of sub-steps with an explicit intermediate "
            "condition between each pair. "
            "Params: action_name (str), steps (list of {name, guard, updates})."
        )
