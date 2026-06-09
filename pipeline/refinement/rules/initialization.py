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
        # A memory array (depth set) is a register file / RAM and is never reset
        # (see engine.is_rtl_style). Its missing reset_value must NOT keep
        # Initialization applicable, or the rule would fire forever on any design
        # containing a memory (every re-pick a no-op → strikes → stall).
        has_unreset_var = any(
            v.get("reset_value") is None and not v.get("depth") for v in variables
        )
        has_reset_action = spec.get("reset_action") is not None
        return has_unreset_var or not has_reset_action

    def apply(self, spec: dict, params: dict) -> dict:
        reset_values: dict[str, str] = params["reset_values"]
        action_name: str = params.get("reset_action_name", "Reset")

        result = copy.deepcopy(spec)

        # A memory array (depth set) is never reset (see is_applicable above and
        # engine.is_rtl_style). Resetting it would emit an illegal whole-array
        # `mem <= 0` that iverilog rejects — and there is no codegen-time lint gate
        # to catch it before Stage 4. Drop any memory name the caller put in
        # reset_values so a stray pick can never produce that: the rule resets
        # scalar registers only.
        mem_names = {v["name"] for v in result.get("variables", []) if v.get("depth")}

        for var in result["variables"]:
            if var["name"] in reset_values and var["name"] not in mem_names:
                var["reset_value"] = reset_values[var["name"]]

        reset_updates = [
            {"variable": name, "expression": expr}
            for name, expr in reset_values.items()
            if name not in mem_names
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
            "concrete initial value to every NON-MEMORY variable (a memory / "
            "register file is not reset — omit it from reset_values). "
            "Params: reset_values (dict variable->expression), "
            "reset_action_name (str, default 'Reset')."
        )
