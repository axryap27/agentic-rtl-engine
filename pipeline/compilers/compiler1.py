"""
Compiler 1 -- JSON(TLA) -> TLA+ and TLC configuration.

This module is Compiler 1 in the agentic-rtl-engine pipeline.  It takes a
FormalSpec (the JSON(TLA) artifact written by Agent 3) and emits:

  1. A syntactically valid ``.tla`` module that TLC can model-check.
  2. A matching ``.cfg`` TLC configuration file.

Both outputs are pure Python string assembly -- no LLM calls, no external
tools required at emit time.  The pair is deterministic: identical FormalSpec
input always produces identical output bytes.

Public entry point
------------------
    compile(formal_spec: FormalSpec) -> tuple[str, str]

Returns ``(tla_source, cfg_source)``.  Raises CompilerError if the spec
contains constructs the template cannot represent.

TLA+ surface syntax emitted
----------------------------
  - MODULE / EXTENDS Integers
  - CONSTANTS  (one per constant name; values supplied in .cfg)
  - VARIABLES  (one per variable name)
  - TypeInvariant  (optional range constraints from Variable.width / .type)
  - Init  (conjunction of  var = <initial_value>  for every variable)
  - Next  (disjunction of guarded actions; each action is an ENABLED /\\ block)
  - Spec  (Init /\ [][Next]_vars /\ fairness)
  - Named invariants (from FormalSpec.invariants list)

Condition / update expression language
---------------------------------------
Agent 3 produces conditions and updates in a plain-English boolean style:
  AND, OR, NOT, =, /=, <, >, <=, >=

This compiler translates them to TLA+ surface syntax:
  AND -> /\\
  OR  -> \\/
  NOT -> ~
  /=  -> /=   (kept as-is; already TLA+)
  =   -> =    (kept as-is)
All other tokens pass through unchanged.

.cfg format
-----------
TLC's configuration file format:

    INIT Init
    NEXT Next
    INVARIANT <name>
    CONSTANTS <name> = <value>

See https://lamport.azurewebsites.net/tla/tlc.html for the full grammar.
Constant values default to a small model set (0..3) when no default is
provided; Agent 3 can override by encoding the value directly in the constant
name expression.
"""

import re
import sys
from typing import Optional

# FormalSpec lives in pipeline/schemas/tla_schema.py
# Import with a try so the module is also usable as a standalone script.
try:
    from pipeline.schemas.tla_schema import FormalSpec, Variable, Transition
except ImportError:
    # Standalone / test usage -- define minimal stubs so the file loads.
    from dataclasses import dataclass, field as dc_field
    from typing import Any

    @dataclass
    class Variable:  # type: ignore[no-redef]
        type: str = "Nat"
        width: int = 8

    @dataclass
    class Transition:  # type: ignore[no-redef]
        label: str = ""
        condition: str = ""
        updates: dict = dc_field(default_factory=dict)

    @dataclass
    class FormalSpec:  # type: ignore[no-redef]
        module_name: str = "Unnamed"
        description: str = ""
        variables: dict = dc_field(default_factory=dict)
        initial: dict = dc_field(default_factory=dict)
        transitions: list = dc_field(default_factory=list)
        invariants: list = dc_field(default_factory=list)


# ---------------------------------------------------------------------------
# Compiler error
# ---------------------------------------------------------------------------

class CompilerError(ValueError):
    """Raised when a FormalSpec node cannot be represented by the template."""


# ---------------------------------------------------------------------------
# Expression translator (Agent 3 plain-English -> TLA+ surface syntax)
# ---------------------------------------------------------------------------

# Ordered substitution table.  Applied left-to-right with word boundaries
# where the token is a standalone keyword.
_EXPR_SUBS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bAND\b"), "/\\\\"),   # AND -> /\
    (re.compile(r"\bOR\b"),  "\\\\/"),   # OR  -> \/
    (re.compile(r"\bNOT\b"), "~"),       # NOT -> ~
    (re.compile(r"\bTRUE\b"),  "TRUE"),  # pass-through (already TLA+)
    (re.compile(r"\bFALSE\b"), "FALSE"), # pass-through
]


def translate_expr(expr: str) -> str:
    """
    Translate a plain-English boolean expression to TLA+ surface syntax.

    Handles AND/OR/NOT keyword substitution.  All other tokens (variable
    names, integer literals, comparison operators, existing TLA+ operators)
    pass through unchanged.

    Args:
        expr: Expression string as produced by Agent 3.

    Returns:
        TLA+ expression string.
    """
    result = expr
    for pattern, replacement in _EXPR_SUBS:
        result = pattern.sub(replacement, result)
    return result


# ---------------------------------------------------------------------------
# Range constraint helper
# ---------------------------------------------------------------------------

def _range_constraint(varname: str, var: "Variable") -> Optional[str]:
    """
    Return a TLA+ membership constraint for *varname* based on its type/width,
    or None if no range is applicable.

    Examples:
      Bit  width=1  ->  varname \\in {0, 1}
      Nat  width=8  ->  varname \\in 0..255
    """
    if var.type == "Bit":
        return f"{varname} \\in {{0, 1}}"
    if var.type == "Nat" and var.width > 0:
        hi = (1 << var.width) - 1
        return f"{varname} \\in 0..{hi}"
    return None


# ---------------------------------------------------------------------------
# TLA+ module emitter
# ---------------------------------------------------------------------------

def _emit_tla(spec: "FormalSpec") -> str:
    """
    Emit the TLA+ module source for *spec*.

    Raises CompilerError on any node that cannot be represented.
    """
    name = spec.module_name
    if not re.match(r"^[A-Za-z_]\w*$", name):
        raise CompilerError(
            f"module_name {name!r} is not a valid TLA+ identifier"
        )

    lines: list[str] = []

    # ---- Module header ----
    sep = "-" * 20
    lines.append(f"{sep} MODULE {name} {sep}")
    lines.append("EXTENDS Integers")
    lines.append("")

    # ---- CONSTANTS (model values; TLC assigns them in .cfg) ----
    # We don't emit CONSTANTS unless the spec explicitly carries constant
    # names.  FormalSpec has no dedicated constants field, so we skip this
    # section for now.  If Agent 3 encodes constants, it should list them
    # in variables with a sentinel type; extend here as needed.

    # ---- VARIABLES ----
    if not spec.variables:
        raise CompilerError("FormalSpec.variables is empty -- nothing to model-check")

    var_names = list(spec.variables.keys())
    lines.append("VARIABLES")
    for i, vn in enumerate(var_names):
        comma = "," if i < len(var_names) - 1 else ""
        lines.append(f"    {vn}{comma}")
    lines.append("")

    # ---- TypeInvariant ----
    constraints = []
    for vn, var in spec.variables.items():
        c = _range_constraint(vn, var)
        if c:
            constraints.append(c)

    if constraints:
        lines.append("TypeInvariant ==")
        for c in constraints:
            lines.append(f"    /\\ {c}")
        lines.append("")

    # ---- Init ----
    if not spec.initial:
        raise CompilerError("FormalSpec.initial is empty -- TLC requires an Init predicate")

    lines.append("Init ==")
    init_items = list(spec.initial.items())
    for vn, val in init_items:
        if vn not in spec.variables:
            raise CompilerError(
                f"FormalSpec.initial contains unknown variable {vn!r} "
                f"(not declared in FormalSpec.variables)"
            )
        lines.append(f"    /\\ {vn} = {translate_expr(val)}")
    lines.append("")

    # ---- Individual action definitions ----
    # Each Transition becomes a named TLA+ action.
    # If a transition has an empty label, fall back to "Action_<index>".
    action_names: list[str] = []
    for idx, t in enumerate(spec.transitions):
        label = t.label.strip() if t.label else f"Action_{idx}"
        if not re.match(r"^[A-Za-z_]\w*$", label):
            raise CompilerError(
                f"Transition label {label!r} is not a valid TLA+ identifier "
                f"(transition index {idx})"
            )
        action_names.append(label)

        # Guard (enabling condition)
        guard = translate_expr(t.condition) if t.condition.strip() else "TRUE"

        # Build UNCHANGED list for variables not mentioned in updates
        updated = set(t.updates.keys())
        unchanged = [vn for vn in var_names if vn not in updated]

        lines.append(f"{label} ==")
        lines.append(f"    /\\ {guard}")

        # Primed update conjuncts
        for vn, expr in t.updates.items():
            if vn not in spec.variables:
                raise CompilerError(
                    f"Transition {label!r} updates unknown variable {vn!r} "
                    f"(not declared in FormalSpec.variables)"
                )
            lines.append(f"    /\\ {vn}' = {translate_expr(expr)}")

        # UNCHANGED for variables not touched by this action
        if unchanged:
            unchanged_str = ", ".join(unchanged)
            lines.append(f"    /\\ UNCHANGED <<{unchanged_str}>>")
        lines.append("")

    # ---- Next (disjunction of all actions) ----
    if not action_names:
        raise CompilerError(
            "FormalSpec.transitions is empty -- TLC requires a Next predicate "
            "with at least one action"
        )

    lines.append("Next ==")
    for an in action_names:
        lines.append(f"    \\/ {an}")
    lines.append("")

    # ---- Named invariants from spec ----
    for i, inv in enumerate(spec.invariants):
        inv_name = f"Inv{i}" if len(spec.invariants) > 1 else "Invariant"
        lines.append(f"{inv_name} ==")
        lines.append(f"    {translate_expr(inv)}")
        lines.append("")

    # ---- TypeInvariant referenced in Spec if constraints were emitted ----
    type_inv_ref = " /\\ [][TypeInvariant]_vars" if constraints else ""

    # ---- Spec (standard TLA+ liveness formula) ----
    vars_tuple = ", ".join(var_names)
    lines.append(f"vars == <<{vars_tuple}>>")
    lines.append("")
    lines.append("Spec ==")
    lines.append("    /\\ Init")
    lines.append("    /\\ [][Next]_vars")
    lines.append("    /\\ WF_vars(Next)")
    lines.append("")

    # ---- Module footer ----
    # Footer must be >= header length. Header is f"{sep} MODULE {name} {sep}"
    # = len(sep)*2 + len(" MODULE ") + len(name) + len(" ") = len(sep)*2 + len(name) + 9.
    lines.append("=" * (len(sep) * 2 + len(name) + 9))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# .cfg emitter
# ---------------------------------------------------------------------------

def _emit_cfg(spec: "FormalSpec") -> str:
    """
    Emit a TLC .cfg configuration file for *spec*.

    Format:
        INIT Init
        NEXT Next
        INVARIANT <name>     (one per invariant; TypeInvariant always included)
        CONSTANTS <n> = <v>  (currently no constants; reserved)
    """
    lines: list[str] = []

    lines.append("INIT Init")
    lines.append("NEXT Next")

    # Type invariant always added when variables have width constraints
    has_constraints = any(
        _range_constraint(vn, var) is not None
        for vn, var in spec.variables.items()
    )
    if has_constraints:
        lines.append("INVARIANT TypeInvariant")

    # Named invariants
    for i, _inv in enumerate(spec.invariants):
        inv_name = f"Inv{i}" if len(spec.invariants) > 1 else "Invariant"
        lines.append(f"INVARIANT {inv_name}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public entry point (pinned signature)
# ---------------------------------------------------------------------------

def compile(formal_spec: "FormalSpec") -> tuple[str, str]:
    """
    Compiler 1 public entry point.

    Translate *formal_spec* (a FormalSpec / JSON(TLA) object) into:
      - a TLA+ module source string
      - a TLC .cfg configuration string

    Both are deterministic: identical input -> identical output bytes.

    If ``formal_spec.raw_tla`` is populated (attribute present and non-empty),
    the raw TLA+ text is passed through directly and only the .cfg is generated
    from the structured fields.  This lets Agent 3 bypass the template for
    specs it writes by hand.

    Args:
        formal_spec: Validated FormalSpec object (from tla_schema.py).

    Returns:
        (tla_source, cfg_source) -- both as strings.

    Raises:
        CompilerError: if the spec contains constructs the template cannot
                       represent (e.g. invalid identifiers, empty variables).
    """
    # raw_tla pass-through: if Agent 3 wrote the TLA+ directly, use it.
    raw = getattr(formal_spec, "raw_tla", None)
    if raw and raw.strip():
        tla_source = raw.strip()
    else:
        tla_source = _emit_tla(formal_spec)

    cfg_source = _emit_cfg(formal_spec)
    return tla_source, cfg_source


# ---------------------------------------------------------------------------
# CLI / self-test
# ---------------------------------------------------------------------------

_COUNTER_SPEC_DICT = {
    "module_name": "TwoBitCounter",
    "description": "A simple 2-bit synchronous up-counter with active-high reset.",
    "variables": {
        "count": {"type": "Nat", "width": 2},
        "clk":   {"type": "Bit", "width": 1},
    },
    "initial": {
        "count": "0",
        "clk":   "0",
    },
    "transitions": [
        {
            "label": "Tick",
            "condition": "TRUE",
            "updates": {
                "count": "(count + 1) % 4",
                "clk":   "1 - clk",
            },
        },
        {
            "label": "Reset",
            "condition": "TRUE",
            "updates": {
                "count": "0",
                "clk":   "0",
            },
        },
    ],
    "invariants": [
        "count >= 0 AND count <= 3",
    ],
}


def _make_counter_spec() -> "FormalSpec":
    """Construct the 2-bit counter FormalSpec for self-test."""
    try:
        from pipeline.schemas.tla_schema import FormalSpec as FS, Variable as V, Transition as T
        d = _COUNTER_SPEC_DICT
        return FS(
            module_name=d["module_name"],
            description=d["description"],
            variables={k: V(**v) for k, v in d["variables"].items()},
            initial=d["initial"],
            transitions=[T(**t) for t in d["transitions"]],
            invariants=d["invariants"],
        )
    except ImportError:
        # Standalone mode -- use stub dataclasses
        spec = FormalSpec()
        spec.module_name = _COUNTER_SPEC_DICT["module_name"]
        spec.description = _COUNTER_SPEC_DICT["description"]
        spec.variables = {
            k: Variable(type=v["type"], width=v["width"])
            for k, v in _COUNTER_SPEC_DICT["variables"].items()
        }
        spec.initial = _COUNTER_SPEC_DICT["initial"]
        spec.transitions = [
            Transition(**t) for t in _COUNTER_SPEC_DICT["transitions"]
        ]
        spec.invariants = _COUNTER_SPEC_DICT["invariants"]
        return spec


def main() -> None:
    import argparse, json, os

    parser = argparse.ArgumentParser(
        description="Compiler 1: JSON(TLA) -> TLA+ module + TLC .cfg"
    )
    parser.add_argument(
        "spec",
        nargs="?",
        help="Path to a JSON(TLA) file (FormalSpec JSON).  Omit to run the built-in 2-bit counter self-test.",
    )
    parser.add_argument(
        "--out-dir",
        default=".",
        help="Directory to write <module>.tla and <module>.cfg (default: current dir)",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print TLA+ and .cfg to stdout instead of writing files",
    )
    args = parser.parse_args()

    if args.spec:
        with open(args.spec) as f:
            data = json.load(f)
        try:
            from pipeline.schemas.tla_schema import FormalSpec as FS
            spec = FS.model_validate(data)
        except ImportError:
            raise SystemExit("pipeline package not found; run from repo root")
    else:
        spec = _make_counter_spec()
        print("[self-test] Using built-in 2-bit counter spec", file=sys.stderr)

    tla_src, cfg_src = compile(spec)

    if args.stdout:
        print("=== TLA+ ===")
        print(tla_src)
        print()
        print("=== .cfg ===")
        print(cfg_src)
    else:
        os.makedirs(args.out_dir, exist_ok=True)
        tla_path = os.path.join(args.out_dir, f"{spec.module_name}.tla")
        cfg_path = os.path.join(args.out_dir, f"{spec.module_name}.cfg")
        with open(tla_path, "w") as f:
            f.write(tla_src)
        with open(cfg_path, "w") as f:
            f.write(cfg_src)
        print(f"Wrote {tla_path}")
        print(f"Wrote {cfg_path}")


if __name__ == "__main__":
    main()
