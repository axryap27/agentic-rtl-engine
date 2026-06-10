"""
LoopIntroduction — the refinement-calculus iteration / loop-introduction rule
(Morgan's iteration rule / Back's do–od introduction).

This is the rule that makes refinement REAL instead of a rubber stamp. It refines
an ABSTRACT specification statement (a Morgan spec statement, e.g. a multiplier's
`product' = a * b`) into a CONCRETE clocked loop (e.g. shift-add) — but only after
the deterministic obligation kernel (`pipeline.refinement.obligations`) DISCHARGES
the three iteration-rule proof obligations against the real expression semantics:

    O1   pre  =>  inv[init]
    O2   inv /\ guard  =>  inv[body] /\ variant decreases
    O3   inv /\ ~guard  =>  post

Soundness comes from the CHECK, not from trusting the proposer. If the obligations
do not hold, apply() is a NO-OP (returns an unchanged deepcopy of the spec): that is
the engine's backtrack signal (the no-op guard at engine.py:496-503 excludes this
exact (rule, params) and, after 3 distinct failed proposals at a depth, backtracks —
forcing the proposer toward a correct invariant/body). A ValueError is reserved for
MALFORMED params (missing keys), which the engine excludes at engine.py:482-485.

DISTINCT FROM `Iteration`
-------------------------
`Iteration` merely sets clocked=True on an already-concrete action. LoopIntroduction
takes an abstract postcondition and DERIVES the concrete clocked body, verified. They
are different rules and must not be merged.

PURITY
------
apply() is pure: it deepcopies first and never mutates its inputs. The kernel is
pure. No LLM imports.
"""

import copy

from .base import RefinementRule
from ..obligations import discharge_loop_obligations


# Params the proposer (pick_rule) must supply for this rule.
_REQUIRED_PARAMS = (
    "action_name",
    "postcondition",
    "invariant",
    "variant",
    "guard",
    "init",
    "body",
    "mapping",
    "fresh_vars",
    "input_widths",
)


class LoopIntroduction(RefinementRule):
    """
    Table 1 — Iteration (refinement-calculus loop introduction, Morgan/Back).

    Formal:  w : [pre, post]  ⊑  Var locals; locals := init;
             do guard -> body od   provided  pre => inv[init],
             inv /\ guard => inv[body] /\ variant'<variant,  inv /\ ~guard => post.

    Hardware role: turn an abstract arithmetic specification (e.g. product = a*b)
    into a concrete, synthesizable iterative datapath (shift-add) whose correctness
    is verified by discharging the iteration-rule obligations.

    Required params:
        action_name (str):   the spec-statement action to refine.
        postcondition (str): the abstract post the loop must establish.
        invariant (str):     candidate loop invariant.
        variant (str):       strictly-decreasing measure (termination).
        guard (str):         loop-continuation condition.
        init (dict):         {loop_var: expr} establishing the invariant.
        body (dict):         {loop_var: expr} one simultaneous loop step.
        mapping (dict):      {abstract_var: concrete_expr} data refinement at exit.
        fresh_vars (list):   [{name, width, type?, reset_value?}] new loop registers.
        input_widths (dict): {input_name: bit_width} the obligation input domain.
    """

    # Marker field on an action that makes it a refinement target for this rule.
    SPEC_STATEMENT_MARKER = "spec_statement"

    def is_applicable(self, spec: dict) -> bool:
        """True iff some non-reset, non-combinational action is a spec statement
        (`spec_statement: True`) whose target variable(s) are still abstract.

        The target variables are read from the action's `updates` (the variables
        the postcondition constrains); at least one must still be marked
        `abstract: True` in `spec["variables"]`.
        """
        reset_name = spec.get("reset_action")
        var_abstract = {
            v["name"]: v.get("abstract", False) for v in spec.get("variables", [])
        }
        for action in spec.get("actions", []):
            if action.get("name") == reset_name:
                continue
            if action.get("combinational", False):
                continue
            if not action.get(self.SPEC_STATEMENT_MARKER, False):
                continue
            targets = [u.get("variable") for u in action.get("updates", [])]
            if any(var_abstract.get(t, False) for t in targets):
                return True
        return False

    def apply(self, spec: dict, params: dict) -> dict:
        # --- validate params (malformed -> ValueError, engine excludes) ---
        missing = [k for k in _REQUIRED_PARAMS if k not in params]
        if missing:
            raise ValueError(
                f"LoopIntroduction: missing required params: {missing}"
            )
        action_name = params["action_name"]
        if not isinstance(params["init"], dict) or \
                not isinstance(params["body"], dict) or \
                not isinstance(params["mapping"], dict) or \
                not isinstance(params["input_widths"], dict) or \
                not isinstance(params["fresh_vars"], list):
            raise ValueError(
                "LoopIntroduction: init/body/mapping/input_widths must be dicts "
                "and fresh_vars a list."
            )

        # --- 1. Discharge the obligations (the soundness gate) ---
        result_check = discharge_loop_obligations(
            post=params["postcondition"],
            invariant=params["invariant"],
            variant=params["variant"],
            guard=params["guard"],
            init=params["init"],
            body=params["body"],
            mapping=params["mapping"],
            input_widths=params["input_widths"],
        )

        # --- 2. Not verified -> NO-OP (the backtrack signal) ---
        # An unchanged deepcopy keeps apply() pure and trips the engine's no-op
        # guard, which excludes this (rule, params) and backtracks after 3 strikes.
        if not result_check.ok:
            return copy.deepcopy(spec)

        # --- 3. Verified -> install the concrete loop ---
        result = copy.deepcopy(spec)

        # 3a. Introduce the fresh loop variables as CONCRETE registers
        #     (reuse IntroduceVariable's variable shape; abstract=False).
        existing = {v["name"] for v in result.get("variables", [])}
        variables = result.setdefault("variables", [])
        for fv in params["fresh_vars"]:
            name = fv["name"]
            if name in existing:
                # Already present (e.g. the accumulator is the output itself).
                # Mark it concrete and continue; do not duplicate.
                for v in variables:
                    if v["name"] == name:
                        v["abstract"] = False
                continue
            variables.append({
                "name": name,
                "type": fv.get("type", f"0..{(1 << int(fv.get('width', 1))) - 1}"),
                "width": int(fv.get("width", 1)),
                "abstract": False,
                "reset_value": fv.get("reset_value", None),
                "clocked": True,
            })
            existing.add(name)

        # 3b. Find the target action and replace its abstract updates with the
        #     verified loop body, guarded by the loop guard. Clear the marker.
        target_action = None
        for action in result.get("actions", []):
            if action["name"] == action_name:
                target_action = action
                break
        if target_action is None:
            # Named action absent -> nothing to refine; no-op (backtrack signal).
            return copy.deepcopy(spec)

        # The verified loop body becomes the per-cycle register update set.
        target_action["updates"] = [
            {"variable": var, "expression": expr}
            for var, expr in params["body"].items()
        ]
        # Guard the body by the loop-continuation condition (clocked iteration).
        target_action["guard"] = params["guard"]
        target_action["clocked"] = True
        target_action.pop(self.SPEC_STATEMENT_MARKER, None)
        target_action.pop("postcondition", None)

        # 3c. Mark the abstract target variable(s) concrete now they are a register
        #     loop. The targets are the abstract vars named by the mapping AND the
        #     loop variables written by the body.
        now_concrete = set(params["mapping"].keys()) | set(params["body"].keys())
        for v in result.get("variables", []):
            if v["name"] in now_concrete:
                v["abstract"] = False
                v["clocked"] = True

        # 3d. Record the data-refinement mapping on the spec.
        result.setdefault("abstraction_mapping", {})
        result["abstraction_mapping"].update(dict(params["mapping"]))

        # 3e. Record the discharged obligations on the action (audit + critic).
        target_action["refinement"] = {
            "invariant": params["invariant"],
            "variant": params["variant"],
            "guard": params["guard"],
            "mode": result_check.mode,
            "cases_checked": result_check.cases_checked,
            "obligations": dict(result_check.obligations),
        }

        # 3f. Record the loop STRUCTURE on the action so a downstream scheduler
        #     (ScheduleHandshakeFSM) can mechanically turn the verified bare loop
        #     into a clocked start/done handshake FSMD. The obligation audit above
        #     is for the critic; this is the scheduler's input. In particular `init`
        #     (the per-register LOAD values, e.g. {"product":"0","mcand":"a",...}) is
        #     recorded NOWHERE else and the scheduler needs it for the load branch.
        #     A deepcopy keeps the marker independent of the params dict (purity).
        target_action["loop"] = {
            "init": copy.deepcopy(params["init"]),
            "body": copy.deepcopy(params["body"]),
            "variant": params["variant"],
            "guard": params["guard"],
        }

        return result

    def describe(self) -> str:
        return (
            "LoopIntroduction: refine an abstract specification statement (a "
            "postcondition over abstract variables, e.g. product = a*b) into a "
            "verified concrete clocked loop (e.g. shift-add). Discharges the "
            "iteration-rule obligations (O1 init establishes invariant, O2 body "
            "maintains invariant and decreases variant, O3 exit establishes post) "
            "against the real semantics; only installs the loop if all hold, else "
            "no-op (backtrack). Params: action_name (str), postcondition (str), "
            "invariant (str), variant (str), guard (str), init (dict), body (dict), "
            "mapping (dict), fresh_vars (list), input_widths (dict)."
        )
