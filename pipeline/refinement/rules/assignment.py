import copy
from .base import RefinementRule


class Assignment(RefinementRule):
    """
    Table 2 — Assignment.

    Formal: w, x : [pre, dur, post] ⊑ w := E  (where post is satisfied by w := E)
    Hardware role: the fundamental register update — makes explicit which
    variable gets which expression on a clock edge or combinational path.

    Required params:
        action_name (str): name of the action receiving the explicit assignment.
        updates (list[dict]): list of {variable (str), expression (str)} pairs
            to add or replace in the action's update list.
    """

    def is_applicable(self, spec: dict) -> bool:
        # Applicable to any action that either has no updates yet (abstract
        # post-condition only) or has updates that could be made more concrete.
        return any(
            not a.get("updates")
            for a in spec.get("actions", [])
            if a["name"] != spec.get("reset_action")
        )

    def apply(self, spec: dict, params: dict) -> dict:
        action_name: str = params["action_name"]
        new_updates: list[dict] = params["updates"]

        result = copy.deepcopy(spec)

        for action in result.get("actions", []):
            if action["name"] == action_name:
                existing = {u["variable"]: u for u in action.get("updates", [])}
                for upd in new_updates:
                    existing[upd["variable"]] = copy.deepcopy(upd)
                action["updates"] = list(existing.values())
                break

        return result

    def describe(self) -> str:
        return (
            "Assignment: make explicit which variable(s) an action assigns "
            "and with what expression, turning an abstract post-condition into "
            "a concrete register write. "
            "Params: action_name (str), updates (list of {variable, expression})."
        )
