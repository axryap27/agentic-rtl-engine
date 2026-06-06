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
# Free-input detection (BUG-18)
# ---------------------------------------------------------------------------
# An identifier that appears in an action guard or update expression but is NOT
# a declared engine variable, clk/reset, or a TLA+/Verilog keyword is a "free
# input" — an externally-driven port (e.g. `d` on a DFF, `en` on a counter).
# Such identifiers never enter the VARIABLES block on their own, so Compiler 2
# never sees them and emits Verilog that references an undeclared wire (iverilog
# fails to bind it; worse, a guard-only `en` is silently dropped). We scan for
# them here and inject them into VARIABLES so Compiler 2's existing
# "not driven by either block → input port" classifier ports them automatically.
#
# The reserved set below intentionally mirrors the operators/keywords Compiler 2
# already understands in its expression translator (compiler2.RTLTLACompiler):
# the TLA+ surface keywords (IF/THEN/ELSE/TRUE/FALSE) plus the boolean/word
# operators TLC allows. Numeric literals and the `'` primed-suffix are handled
# by the identifier regex itself. Keep this list in sync with compiler2's
# translator if new keyword forms are added there.

#: TLA+ / Verilog reserved words that must NOT be misclassified as free inputs.
_RESERVED_IDENTIFIERS: frozenset[str] = frozenset({
    # TLA+ surface keywords used in RTL-style guards/expressions
    "IF", "THEN", "ELSE", "TRUE", "FALSE",
    "UNCHANGED", "CASE", "OTHER", "LET", "IN",
    # Word-form boolean operators TLC accepts
    "AND", "OR", "NOT",
    # Module-level keywords (won't appear in expressions, but cheap to exclude)
    "MODULE", "EXTENDS", "VARIABLES", "CONSTANTS", "CONSTANT", "VARIABLE",
})

#: Matches a TLA+/Verilog identifier (optionally primed). The trailing `'?`
#: lets us strip a primed-suffix so `q'` and `q` are treated as the same name.
_IDENT_RE = re.compile(r"[A-Za-z_]\w*'?")


def _scan_identifiers(expr: str) -> set[str]:
    """Return the set of (unprimed) identifiers appearing in a TLA+ expression.

    Numeric literals never match (the regex requires a leading letter/underscore).
    A primed suffix (`x'`) is stripped so the primed and unprimed forms collapse
    to one name.
    """
    found: set[str] = set()
    for tok in _IDENT_RE.findall(expr or ""):
        found.add(tok[:-1] if tok.endswith("'") else tok)
    return found


# ---------------------------------------------------------------------------
# Multi-branch / multi-step update composition (G12)
# ---------------------------------------------------------------------------
# The Alternation rule stores its mutually-exclusive guarded branches on
# action["branches"]; SequentialComposition stores its ordered sub-steps on
# action["sequential_steps"]. Both rules ALSO keep a flat action["updates"]
# list, but that flat list collapses multiple assignments to the SAME variable
# down to one (first-wins) — so emitting clocked logic from "updates" alone
# silently drops every branch/step after the first for any shared variable
# (G12). The helpers below reconstruct the correct composed next-state RHS for
# each variable from the structured branches/steps, so the bridge emits the
# full logic. They are pure functions of the action dict.


def _substitute_idents(expr: str, subst: dict[str, str]) -> str:
    """Replace whole-identifier occurrences in *expr* per the *subst* map.

    Each key in *subst* is an (unprimed) identifier; the value is the text to
    splice in (already parenthesised by the caller when needed). Matching is
    identifier-boundary aware via the same regex used for free-input scanning,
    and a primed suffix is honoured (``v'`` matches the key ``v``). Identifiers
    not present in *subst* are left untouched. Pure and deterministic.
    """
    if not expr or not subst:
        return expr

    def _repl(m: "re.Match[str]") -> str:
        tok = m.group(0)
        name = tok[:-1] if tok.endswith("'") else tok
        return subst.get(name, tok)

    return _IDENT_RE.sub(_repl, expr)


def _nested_if(clauses: list[tuple[str, str]], default: str) -> str:
    """Build a nested TLA+ conditional from priority-ordered (guard, expr) pairs.

    Produces ``IF g1 THEN e1 ELSE IF g2 THEN e2 ELSE ... ELSE default``.
    With no clauses, returns *default* verbatim. The trailing ELSE is the
    register-hold value (the variable's prior value) so an un-guarded cycle
    leaves the register unchanged — exactly RTL flip-flop semantics.

    Compiler 2's translate_expr renders this recursively into a nested ternary
    ``(g1) ? (e1) : ((g2) ? (e2) : (default))`` (see compiler2._split_if_then_else).
    Keyword spacing (`` IF ``/`` THEN ``/`` ELSE ``) is exactly what its
    depth-0 splitter expects.
    """
    if not clauses:
        return default
    out = default
    for guard, value in reversed(clauses):
        out = f"IF {guard} THEN {value} ELSE {out}"
    return out


def _ordered_assigned_vars(groups: list[list[dict]]) -> list[str]:
    """First-seen-ordered list of variables assigned across *groups* of updates."""
    order: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for upd in group:
            v = upd.get("variable")
            if v is not None and v not in seen:
                seen.add(v)
                order.append(v)
    return order


def _alternation_exprs(action: dict) -> list[tuple[str, str]]:
    """Composed (variable, rhs) pairs for an Alternation action's branches.

    Branches are mutually exclusive and priority-ordered. For each variable
    assigned in any branch we emit a nested conditional that selects the
    branch's expression under that branch's guard, in order, falling through to
    the variable holding its previous value (trailing ``ELSE <var>``). A branch
    that does not assign the variable is simply absent from that variable's
    conditional chain (it holds). Deterministic.
    """
    branches: list[dict] = action.get("branches") or []
    var_order = _ordered_assigned_vars([b.get("updates", []) for b in branches])

    result: list[tuple[str, str]] = []
    for var in var_order:
        clauses: list[tuple[str, str]] = []
        for branch in branches:
            guard = branch.get("guard", "TRUE") or "TRUE"
            for upd in branch.get("updates", []):
                if upd.get("variable") == var:
                    clauses.append((guard, upd["expression"]))
                    break
        result.append((var, _nested_if(clauses, var)))
    return result


def _sequential_exprs(action: dict) -> list[tuple[str, str]]:
    """Composed (variable, rhs) pairs for a SequentialComposition action.

    RTL nonblocking semantics chosen and documented here:

      The sub-steps execute *in order within a single clock cycle*. We collapse
      them into one next-state expression per variable by SUBSTITUTION: each
      step's RHS is evaluated against the running symbolic state produced by all
      EARLIER steps in the same cycle (compute -> latch -> compare reads the
      freshly-computed value, not the registered one). This makes the emitted
      single-cycle next-state equal to the net effect of running the steps
      sequentially, while the final assignment is still a single nonblocking
      register update (`reg <= rhs`) — so there is exactly one driver per
      variable and no intra-cycle multi-driver. A step guarded by G updates the
      variable only when G holds, otherwise the variable keeps its running value
      (`IF G THEN new ELSE running`).

    The running state begins as identity (each variable maps to itself), so a
    variable never written by any step is never emitted (it is left to hold via
    its own register, same as Alternation). Deterministic.
    """
    steps: list[dict] = action.get("sequential_steps") or []
    var_order = _ordered_assigned_vars([s.get("updates", []) for s in steps])

    # running[v] = current symbolic next-state expression for v within the cycle.
    # Absent key == "still holds its registered value" (identity).
    running: dict[str, str] = {}

    for step in steps:
        guard = step.get("guard", "TRUE") or "TRUE"
        for upd in step.get("updates", []):
            var = upd.get("variable")
            if var is None:
                continue
            # Substitute earlier-step results into this step's RHS so later
            # steps observe the freshly-computed intra-cycle values.
            new_expr = _substitute_idents(
                upd["expression"],
                {k: f"({v})" for k, v in running.items()},
            )
            prior = running.get(var, var)  # value if this step's guard is false
            if guard == "TRUE":
                running[var] = new_expr
            else:
                running[var] = f"IF {guard} THEN {new_expr} ELSE {prior}"

    return [(var, running[var]) for var in var_order]


def _action_update_exprs(action: dict) -> list[tuple[str, str]]:
    """Return ordered (variable, rhs_expression) pairs for one action.

    Branch-/step-aware: consumes action["branches"] (Alternation) or
    action["sequential_steps"] (SequentialComposition) when present so that
    multiple assignments to the same variable are composed into one correct
    next-state RHS, instead of being collapsed first-wins by the flat
    action["updates"] list (G12). Falls back to the flat updates otherwise.
    Pure function of *action*.
    """
    if action.get("branches"):
        return _alternation_exprs(action)
    if action.get("sequential_steps"):
        return _sequential_exprs(action)
    return [
        (upd["variable"], upd["expression"])
        for upd in action.get("updates", [])
    ]


def _free_inputs(engine_spec: dict, declared: set[str]) -> list[str]:
    """Collect free input identifiers across action guards and updates.

    *declared* is the set of names that already have a declaration (engine
    variables plus clk/reset). Any identifier referenced in a scanned guard or
    update expression that is not declared and not reserved is a free input.

    What is scanned mirrors what Compiler 2 can actually emit from this spec:

      * Every update expression of every action (these become the RHS of
        assign / non-blocking-assignment statements).
      * The guard of every NON-reset action — a guard-only enable like `en = 1`
        is a real external input even though Compiler 2 does not currently
        translate the guard itself (declaring the port at least surfaces the
        signal rather than silently dropping it; see BUG-18).

    The reset action's own guard is deliberately NOT scanned. `engine_spec_to_
    rtl_tla` replaces it with a hardcoded `IF reset = 1 THEN ...`, so the reset
    action's formal guard (e.g. `rst = TRUE`, written by the Initialization
    rule) never appears in emitted Verilog. Scanning it would manufacture a
    dangling `rst` input port that nothing reads.

    Returns the free inputs sorted for deterministic output.
    """
    reset_name = engine_spec.get("reset_action")
    free: set[str] = set()
    for action in engine_spec.get("actions", []):
        exprs: list[str] = []
        if action.get("name") != reset_name:
            exprs.append(action.get("guard", "") or "")
        # Use the branch-/step-composed RHS so identifiers that appear only
        # inside an Alternation branch or a SequentialComposition step (and not
        # in the flat updates list) are still detected as free inputs (G12).
        for _var, expr in _action_update_exprs(action):
            exprs.append(expr or "")
        for expr in exprs:
            for ident in _scan_identifiers(expr):
                if ident in declared or ident in _RESERVED_IDENTIFIERS:
                    continue
                free.add(ident)
    return sorted(free)


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
            # Carry the declared bit width through the engine spec so the RTL
            # emitter (engine_spec_to_rtl_tla → Compiler 2) can size the signal.
            # Without this, multi-bit signals silently truncate to 1 bit (BUG-17).
            "width": var.width,
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

    # VARIABLES — engine vars plus the implicit clk and reset ports, plus any
    # free input identifiers referenced in guards/updates (BUG-18).
    # Each name carries its bit width as a TLA+ comment ("\* width: N") so
    # Compiler 2 can size the signal (BUG-17). The comment is invisible to TLC
    # and is stripped by Compiler 2 after the width is captured. clk and reset
    # are always single-bit. Missing/zero width defaults to 1.
    sized_vars = [
        (v["name"], int(v.get("width") or 1)) for v in variables
    ] + [("clk", 1), ("reset", 1)]

    # Free inputs (BUG-18): identifiers used in guards/update expressions that
    # are NOT declared variables, clk/reset, or TLA+ keywords. Without this they
    # never reach the VARIABLES block, so Compiler 2 emits Verilog referencing an
    # undeclared wire (e.g. `d` on a DFF -> iverilog "Unable to bind wire `d'";
    # a guard-only `en` is silently dropped). Declaring them here lets Compiler
    # 2's "not driven by either block -> input port" classifier expose them as
    # inputs. Default width 1 — we do not invent multi-bit widths from a bare
    # identifier reference. Sorted (by _free_inputs) for deterministic output.
    declared = {name for name, _ in sized_vars}
    free_inputs = _free_inputs(engine_spec, declared)
    sized_vars += [(name, 1) for name in free_inputs]

    lines.append("VARIABLES")
    for i, (name, width) in enumerate(sized_vars):
        comma = "," if i < len(sized_vars) - 1 else ""
        lines.append(f"    {name}{comma}  \\* width: {width}")
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
            for var, expr in _action_update_exprs(action):
                lines.append(f"    /\\ {var}' = {expr}")
        lines.append("")

    # UpdatePipeline — clocked actions wrapped in IF reset THEN ... ELSE ...
    lines.append("UpdatePipeline ==")
    lines.append("    /\\ clk' = 1 - clk")

    if reset_action:
        lines.append("    /\\ IF reset = 1 THEN")
        for var, expr in _action_update_exprs(reset_action):
            lines.append(f"          /\\ {var}' = {expr}")
        lines.append("       ELSE")
        for action in clocked_actions:
            for var, expr in _action_update_exprs(action):
                lines.append(f"          /\\ {var}' = {expr}")
    else:
        # No reset action yet — emit clocked updates flat (partial refinement)
        for action in clocked_actions:
            for var, expr in _action_update_exprs(action):
                lines.append(f"    /\\ {var}' = {expr}")

    lines.append("")

    # Module footer
    # Footer must be >= header length. Header is f"{sep} MODULE {module_name} {sep}"
    # = len(sep)*2 + len(" MODULE ") + len(module_name) + len(" ") = len(sep)*2 + len(module_name) + 9.
    lines.append("=" * (len(sep) * 2 + len(module_name) + 9))

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
        # Branch-/step-aware composition (G12): a multi-branch / multi-step
        # action updates each shared variable with one composed expression, not
        # the first-wins flat update — so TLC checks the real next-state too.
        composed = _action_update_exprs(action)
        updated_vars = {var for var, _ in composed}
        unchanged = [v for v in var_names if v not in updated_vars]

        lines.append(f"{aname} ==")
        lines.append(f"    /\\ {guard}")
        for var, expr in composed:
            lines.append(f"    /\\ {var}' = {expr}")
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

    # Footer must be >= header length (see header f"{sep} MODULE {module_name} {sep}"):
    # len(sep)*2 + len(" MODULE ") + len(module_name) + len(" ") = len(sep)*2 + len(module_name) + 9.
    lines.append("=" * (len(sep) * 2 + len(module_name) + 9))

    tla_source = "\n".join(lines)

    cfg_lines = ["INIT Init", "NEXT Next"]
    for i in range(len(invariants)):
        cfg_lines.append(f"INVARIANT Inv{i}")
    cfg_source = "\n".join(cfg_lines) + "\n"

    return tla_source, cfg_source
