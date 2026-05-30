import copy
from .base import RefinementRule


class Alternation(RefinementRule):
    """
    Table 2 — Alternation.

    Formal: ss ⊑ if G_i then {w : [pre ∧ G_i, dur, post]}  for i = 1..n
    Hardware role: mux / case statement / FSM branch — splits one abstract
    action into mutually exclusive guarded branches, each with its own
    concrete update.

    Required params:
        action_name (str): name of the action to split into branches.
        branches (list[dict]): ordered list of branches, each with:
            - guard (str): TLA+ guard expression for this branch.
            - updates (list[dict]): list of {variable, expression} assignments.
    """

    def is_applicable(self, spec: dict) -> bool:
        return any(
            not a.get("branches")
            for a in spec.get("actions", [])
            if a["name"] != spec.get("reset_action")
        )

    def apply(self, spec: dict, params: dict) -> dict:
        action_name: str = params["action_name"]
        branches: list[dict] = params["branches"]

        result = copy.deepcopy(spec)

        for action in result.get("actions", []):
            if action["name"] == action_name:
                action["branches"] = copy.deepcopy(branches)
                # Merge all branch updates into the flat update list so the
                # action still expresses its full frame for other rules.
                all_vars: dict[str, dict] = {}
                for branch in branches:
                    for upd in branch.get("updates", []):
                        all_vars.setdefault(upd["variable"], upd)
                action["updates"] = list(all_vars.values())
                break

        return result

    def describe(self) -> str:
        return (
            "Alternation: split one abstract action into mutually exclusive "
            "guarded branches (if/case/mux), each with its own concrete "
            "variable assignments. "
            "Params: action_name (str), branches (list of {guard, updates})."
        )
