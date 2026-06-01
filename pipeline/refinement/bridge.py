"""
Bridge between FormalSpec (LLM-facing schema) and the refinement engine's
internal spec format.

FormalSpec (pipeline/schemas/tla_schema.py):
    variables:   dict[str, Variable]    keyed by name; Variable has type, width
    transitions: list[Transition]       Transition has label, condition, updates: dict[str,str]
    initial:     dict[str, str]
    invariants:  list[str]

Engine spec (pipeline/refinement/rules/base.py):
    variables:   list[dict]             each: name, type, abstract, reset_value, clocked
    actions:     list[dict]             each: name, guard, updates: list[{variable,expression}],
                                              clocked, is_rtl_style
    init:        str                    TLA+ Init predicate string
    invariants:  list[str]
    abstraction_mapping: dict[str, str]
    reset_action: str | None
    properties:  list[str]

RTL-style TLA+ (consumed by Compiler 2):
    VARIABLES block
    CombinationalLogic ==  (non-clocked action updates → assign statements)
    UpdatePipeline ==      (clocked actions; IF reset THEN ... ELSE ...)
"""

from __future__ import annotations

import re

from pipeline.schemas.tla_schema import FormalSpec


# ---------------------------------------------------------------------------
# Forward bridge: FormalSpec → engine spec
# ---------------------------------------------------------------------------

def formal_spec_to_engine_spec(spec: FormalSpec) -> dict:
    """
    Convert a FormalSpec to the engine's internal spec dict.

    All variables start as abstract=True with no reset_value. The refinement
    rules add reset_value (Initialization), clocked (Iteration), etc.
    """
    variables = [
        {
            "name": name,
            "type": var.type,
            "abstract": True,
            "reset_value": None,
            "clocked": False,
        }
        for name, var in spec.variables.items()
    ]

    actions = [
        {
            "name": t.label,
            "guard": t.condition,
            "updates": [
                {"variable": k, "expression": v}
                for k, v in t.updates.items()
            ],
            "is_rtl_style": False,
            "clocked": False,
        }
        for t in spec.transitions
    ]

    init_str = " /\\ ".join(
        f"{name} = {expr}" for name, expr in spec.initial.items()
    )

    return {
        "variables": variables,
        "actions": actions,
        "init": init_str,
        "invariants": list(spec.invariants),
        "abstraction_mapping": {},
        "reset_action": None,
        "properties": [],
    }


# ---------------------------------------------------------------------------
# Reverse bridge: engine spec → RTL-style TLA+ text (for Compiler 2)
# ---------------------------------------------------------------------------

def engine_spec_to_rtl_tla(engine_spec: dict, module_name: str) -> str:
    """
    Convert a post-refinement engine spec dict to RTL-style TLA+ text.

    Compiler 2 looks for three sections: VARIABLES, CombinationalLogic,
    UpdatePipeline. This function emits exactly those sections from the
    refined engine spec.

    Args:
        engine_spec: RTL-style engine spec (output of engine.run()).
        module_name: TLA+ module name (used in the module header).

    Returns:
        TLA+ source string ready for Compiler 2.
    """
    variables = engine_spec.get("variables", [])
    actions = engine_spec.get("actions", [])
    reset_action_name = engine_spec.get("reset_action")

    lines: list[str] = []

    # Module header
    sep = "-" * 20
    lines.append(f"{sep} MODULE {module_name} {sep}")
    lines.append("EXTENDS Integers")
    lines.append("")

    # VARIABLES — engine vars plus the implicit clk and reset ports
    var_names = [v["name"] for v in variables] + ["clk", "reset"]
    lines.append("VARIABLES")
    for i, name in enumerate(var_names):
        comma = "," if i < len(var_names) - 1 else ""
        lines.append(f"    {name}{comma}")
    lines.append("")

    # Init (emitted for completeness; Compiler 2 ignores it)
    init_str = engine_spec.get("init", "TRUE")
    lines.append("Init ==")
    lines.append(f"    /\\ {init_str}")
    lines.append("")

    # Split actions into reset / clocked / combinational
    reset_action = None
    clocked_actions = []
    comb_actions = []

    for action in actions:
        if action["name"] == reset_action_name:
            reset_action = action
        elif action.get("clocked", False):
            clocked_actions.append(action)
        else:
            comb_actions.append(action)

    # CombinationalLogic — non-clocked, non-reset actions
    if comb_actions:
        lines.append("CombinationalLogic ==")
        for action in comb_actions:
            for update in action.get("updates", []):
                lines.append(f"    /\\ {update['variable']}' = {update['expression']}")
        lines.append("")

    # UpdatePipeline — clocked actions wrapped in IF reset THEN ... ELSE ...
    lines.append("UpdatePipeline ==")
    lines.append("    /\\ clk' = 1 - clk")

    if reset_action:
        lines.append("    /\\ IF reset = 1 THEN")
        for update in reset_action.get("updates", []):
            lines.append(f"          /\\ {update['variable']}' = {update['expression']}")
        lines.append("       ELSE")
        for action in clocked_actions:
            for update in action.get("updates", []):
                lines.append(f"          /\\ {update['variable']}' = {update['expression']}")
    else:
        # No reset action yet — emit clocked updates flat (partial refinement)
        for action in clocked_actions:
            for update in action.get("updates", []):
                lines.append(f"    /\\ {update['variable']}' = {update['expression']}")

    lines.append("")

    # Module footer
    lines.append("=" * (len(sep) * 2 + len(module_name) + 2))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TLC bridge: engine spec → abstract TLA+ for mid-refinement model checking
# ---------------------------------------------------------------------------

def engine_spec_to_abstract_tla(engine_spec: dict, module_name: str) -> tuple[str, str]:
    """
    Convert an engine spec to abstract TLA+ suitable for TLC model-checking
    during refinement (not the RTL-style format Compiler 2 needs).

    Emits Init, per-action definitions, Next, and invariants so TLC can check
    that each rule application preserves the spec's formal validity.

    Returns:
        (tla_source, cfg_source) — both as strings, same contract as Compiler 1.
    """
    variables = engine_spec.get("variables", [])
    actions = engine_spec.get("actions", [])
    init_str = engine_spec.get("init", "TRUE")
    invariants = engine_spec.get("invariants", [])

    var_names = [v["name"] for v in variables]
    if not var_names:
        raise ValueError("engine_spec_to_abstract_tla: spec has no variables")

    lines: list[str] = []
    sep = "-" * 20
    lines.append(f"{sep} MODULE {module_name} {sep}")
    lines.append("EXTENDS Integers")
    lines.append("")

    lines.append("VARIABLES")
    for i, name in enumerate(var_names):
        lines.append(f"    {name}{',' if i < len(var_names) - 1 else ''}")
    lines.append("")

    lines.append("Init ==")
    lines.append(f"    /\\ {init_str}")
    lines.append("")

    action_names: list[str] = []
    for action in actions:
        aname = action.get("name", "")
        if not re.match(r"^[A-Za-z_]\w*$", aname):
            continue
        action_names.append(aname)

        guard = action.get("guard", "TRUE") or "TRUE"
        updates = action.get("updates", [])
        updated_vars = {u["variable"] for u in updates}
        unchanged = [v for v in var_names if v not in updated_vars]

        lines.append(f"{aname} ==")
        lines.append(f"    /\\ {guard}")
        for upd in updates:
            lines.append(f"    /\\ {upd['variable']}' = {upd['expression']}")
        if unchanged:
            lines.append(f"    /\\ UNCHANGED <<{', '.join(unchanged)}>>")
        lines.append("")

    if not action_names:
        lines.append("Stutter ==")
        lines.append(f"    /\\ UNCHANGED <<{', '.join(var_names)}>>")
        lines.append("")
        action_names = ["Stutter"]

    lines.append("Next ==")
    for aname in action_names:
        lines.append(f"    \\/ {aname}")
    lines.append("")

    for i, inv in enumerate(invariants):
        lines.append(f"Inv{i} ==")
        lines.append(f"    {inv}")
        lines.append("")

    lines.append("=" * (len(sep) * 2 + len(module_name) + 2))

    tla_source = "\n".join(lines)

    cfg_lines = ["INIT Init", "NEXT Next"]
    for i in range(len(invariants)):
        cfg_lines.append(f"INVARIANT Inv{i}")
    cfg_source = "\n".join(cfg_lines) + "\n"

    return tla_source, cfg_source
