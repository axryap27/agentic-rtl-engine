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
            and not a.get("combinational", False)
        )

    def apply(self, spec: dict, params: dict) -> dict:
        action_name: str = params["action_name"]
        branches: list[dict] = params["branches"]

        result = copy.deepcopy(spec)

        for action in result.get("actions", []):
            if action["name"] == action_name:
                # Never split a combinational action into branches — it is a
                # continuous `assign`, not a mux of register updates. is_applicable
                # excludes them, but it can return True on the strength of OTHER
                # (register) actions, so a stray pick naming a combinational action
                # would otherwise corrupt it into self-referential RTL (e.g.
                # `assign full = (count==4) ? 1 : full`) that iverilog accepts.
                # Mirror the Iteration/Initialization apply()-side guard: a no-op
                # the engine then excludes (a genuine mutation would slip past the
                # no-op guard).
                if action.get("combinational", False):
                    return result
                action["branches"] = copy.deepcopy(branches)
                # The flat `updates` list is only a FRAME summary (one entry per
                # variable this action touches) used by is_rtl_style and by
                # other rules' applicability checks. It is NOT the source of the
                # emitted next-state: the bridge composes the real per-variable
                # RHS from `branches` (priority-ordered nested IF) so that two
                # branches assigning the SAME variable both survive into RTL
                # (G12). First-wins here is therefore safe — the dropped branch
                # exprs are reconstructed downstream from `branches`.
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
