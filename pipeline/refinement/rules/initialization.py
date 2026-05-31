import copy
from .base import RefinementRule


class Initialization(RefinementRule):
    """
    Table 1 — Initialization.

    Formal: ss ⊑ w : [pre, dur ∧ rst ⇒ reset_vals, post]
    Hardware role: adds synchronous reset — every variable gets a concrete
    initial value, and a Reset action is introduced.

    Required params:
        reset_values (dict[str, str]): maps each variable name to its reset
            expression, e.g. {"counter": "0", "state": "Idle"}.
        reset_action_name (str): name for the new reset action.
            Defaults to "Reset" if omitted.
    """

    def is_applicable(self, spec: dict) -> bool:
        variables = spec.get("variables", [])
        if not variables:
            return False
        has_unreset_var = any(v.get("reset_value") is None for v in variables)
        has_reset_action = spec.get("reset_action") is not None
        return has_unreset_var or not has_reset_action

    def apply(self, spec: dict, params: dict) -> dict:
        reset_values: dict[str, str] = params["reset_values"]
        action_name: str = params.get("reset_action_name", "Reset")

        result = copy.deepcopy(spec)

        for var in result["variables"]:
            if var["name"] in reset_values:
                var["reset_value"] = reset_values[var["name"]]

        reset_updates = [
            {"variable": name, "expression": expr}
            for name, expr in reset_values.items()
        ]
        reset_action = {
            "name": action_name,
            "guard": "rst = TRUE",
            "updates": reset_updates,
            "is_rtl_style": False,
            "clocked": False,
        }

        existing_names = {a["name"] for a in result.get("actions", [])}
        if action_name not in existing_names:
            result.setdefault("actions", []).append(reset_action)

        result["reset_action"] = action_name
        return result

    def describe(self) -> str:
        return (
            "Initialization: add a synchronous reset action and assign a "
            "concrete initial value to every variable. "
            "Params: reset_values (dict variable->expression), "
            "reset_action_name (str, default 'Reset')."
        )
