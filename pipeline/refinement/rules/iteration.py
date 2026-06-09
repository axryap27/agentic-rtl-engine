import copy
from .base import RefinementRule


class Iteration(RefinementRule):
    """
    Table 1 — Iteration.

    Formal: sf ⊑ process[output : w]{w : [T = t0 ∧ inv, env, T = (t0+1) mod N]}
    Hardware role: wraps an action in a clock loop — the action body becomes the
    per-cycle register update, making it synthesizable as clocked logic.

    Required params:
        action_name (str): name of the action to mark as clocked.
    """

    def is_applicable(self, spec: dict) -> bool:
        return any(
            not a.get("clocked", False) and not a.get("combinational", False)
            for a in spec.get("actions", [])
            if a["name"] != spec.get("reset_action")
        )

    def apply(self, spec: dict, params: dict) -> dict:
        action_name: str = params["action_name"]

        result = copy.deepcopy(spec)

        for action in result.get("actions", []):
            if action["name"] == action_name:
                # Never clock a combinational action — it is continuous logic
                # (an `assign`), not a register. A stray pick is a no-op (the
                # engine's no-op guard then excludes it).
                if action.get("combinational", False):
                    return result
                # Idempotent: re-applying Iteration to an already-clocked action
                # must be a no-op. The guard re-wrapping below used to add one
                # paren layer on EVERY call, so a picker that re-picked the same
                # action kept changing the spec hash and the engine cycled to
                # max_steps instead of converging (the 2-bit-counter stall:
                # Iteration(Increment) was chosen 8 times in a row). Returning
                # here leaves first-application behaviour byte-for-byte unchanged.
                if action.get("clocked", False):
                    return result
                action["clocked"] = True
                # An action wrapped in iteration checks its guard every cycle.
                # Prepend the clock-enable guard if not already present.
                if "clk_enable" not in action.get("guard", ""):
                    existing_guard = action.get("guard", "TRUE")
                    action["guard"] = (
                        existing_guard
                        if existing_guard == "TRUE"
                        else f"({existing_guard})"
                    )
                break

        # Mark variables updated by this action as clocked storage.
        clocked_vars = set()
        for action in result.get("actions", []):
            if action["name"] == action_name:
                for upd in action.get("updates", []):
                    clocked_vars.add(upd["variable"])

        for var in result.get("variables", []):
            if var["name"] in clocked_vars:
                var["clocked"] = True
                # A variable that has entered a clock domain is concrete
                # hardware storage (a register). Mark it non-abstract so that
                # the RTL-style termination predicate can recognise it.
                var["abstract"] = False

        return result

    def describe(self) -> str:
        return (
            "Iteration: wrap an action in a clock iteration so it executes "
            "every clock cycle, turning its body into a synthesizable "
            "per-cycle register update. "
            "Params: action_name (str)."
        )
