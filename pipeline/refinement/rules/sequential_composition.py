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
            and not a.get("combinational", False)
        )

    def apply(self, spec: dict, params: dict) -> dict:
        action_name: str = params["action_name"]
        # Deepcopy up front so we never mutate the caller's params: the engine
        # writes these exact params into refinement_chain.json and replays them
        # to backtrack, so apply() must leave its inputs untouched.
        steps: list[dict] = copy.deepcopy(params["steps"])

        result = copy.deepcopy(spec)

        for action in result.get("actions", []):
            if action["name"] == action_name:
                # Never decompose a combinational action — it is continuous logic
                # (an `assign`), not a sequenced register update. is_applicable
                # already excludes them, but it can return True on the strength of
                # OTHER (register) actions, so a stray pick naming a combinational
                # action by name would otherwise corrupt it here. Mirror the
                # Iteration/Initialization apply()-side guard: a no-op the engine's
                # no-op guard then excludes (a genuine mutation would slip past it).
                if action.get("combinational", False):
                    return result
                # Carry the original guard into the first step if not supplied.
                if steps and not steps[0].get("guard"):
                    steps[0]["guard"] = action.get("guard", "TRUE")
                action["sequential_steps"] = copy.deepcopy(steps)
                # The flat `updates` list is only a FRAME summary (one entry per
                # variable this action touches) used by is_rtl_style and by
                # other rules' applicability checks. It is NOT the source of the
                # emitted next-state: the bridge composes the real per-variable
                # RHS from `sequential_steps` by ordered substitution so that
                # successive steps assigning the SAME variable all survive into
                # RTL (G12). Keeping one entry per variable here is therefore
                # safe — the per-step exprs are reconstructed downstream.
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
