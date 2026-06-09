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


# ---------------------------------------------------------------------------
# Boolean word-operator translation (FIX 3)
# ---------------------------------------------------------------------------
# Agent 3 sometimes writes English boolean operators (AND/OR/NOT) in transition
# guards and update expressions. The Compiler-1/TLC path translates these (see
# compiler1.translate_expr), but the RTL path goes FormalSpec -> bridge ->
# Compiler 2, which only understands symbolic operators (/\ \/ ~). If we leave
# the words in, Compiler 2 never translates them AND the free-input scanner
# excludes AND/OR/NOT as reserved but would otherwise pass guard structure
# through wrongly. We normalise to symbolic form here, at the FormalSpec ->
# engine-spec boundary, so the engine spec is symbolic and every downstream
# RTL-path consumer (Compiler 2 + the free-input scanner) sees operators.
#
# This mirrors compiler1._EXPR_SUBS (AND -> /\, OR -> \/, NOT -> ~) and is
# idempotent: an already-symbolic expression has no AND/OR/NOT word tokens, so
# applying it again is a no-op. Existing engine-spec-built tests (which write
# symbolic guards directly) are therefore unaffected.
# NOTE: these are re.sub REPLACEMENT strings, where a backslash is itself
# special. To emit a single literal backslash we must write "\\\\" in source
# (two backslashes survive to the replacement template, which yields one literal
# backslash). This mirrors compiler1._EXPR_SUBS exactly.
_BOOL_WORD_SUBS: list[tuple["re.Pattern[str]", str]] = [
    (re.compile(r"\bAND\b"), "/\\\\"),   # AND -> /\
    (re.compile(r"\bOR\b"), "\\\\/"),    # OR  -> \/
    (re.compile(r"\bNOT\b"), "~"),       # NOT -> ~
    # Arithmetic word operator: `mod` is not valid TLA+ (TLC modulo is `%`) and
    # is not translated downstream, so it slips past Compiler-1/TLC and then both
    # leaks a phantom `input mod` port (scanned as a free identifier) AND emits
    # invalid Verilog `a mod b`. Fold it to `%` at the same boundary as AND/OR/NOT.
    (re.compile(r"\bmod\b"), "%"),       # mod -> %
]


def _translate_bool_words(expr: str) -> str:
    """Replace English word operators (AND/OR/NOT and the arithmetic `mod`) with
    their TLA+ symbolic forms.

    Word-boundary anchored so identifiers containing these substrings (e.g.
    ``ANDgate``, ``commander``, ``modcount``) are not touched. Idempotent on
    already-symbolic input. Pure. Comparison WORDS (``equals``/``less than``) are
    intentionally NOT handled here — they are ambiguous to parse deterministically
    and are instead prevented at the source by the Agent-3 prompt (FIX 3).
    """
    if not expr:
        return expr
    result = expr
    for pattern, replacement in _BOOL_WORD_SUBS:
        result = pattern.sub(replacement, result)
    return result


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


def _is_identity_hold(action: dict) -> bool:
    """True iff *action* only holds its own registers — every update is ``v' = v``.

    A pure register-hold / idle transition (e.g. a ``Hold`` action ``acc' = acc``)
    emits nothing distinct in RTL: the register already holds via the ELSE branch
    of its clocked driver. Such an action therefore (a) need NOT be separately
    clocked for the spec to be RTL-style (see ``engine.is_rtl_style``), and (b)
    must NOT be emitted as CombinationalLogic — doing so double-drives the register
    (``MultiDriverError``). An action carrying ``branches`` / ``sequential_steps``
    is real multi-way logic, never a pure hold. Pure and deterministic.
    """
    if action.get("branches") or action.get("sequential_steps"):
        return False
    updates = action.get("updates") or []
    if not updates:
        return False
    return all(
        str(u.get("expression", "")).strip() == str(u.get("variable", "")).strip()
        for u in updates
    )


# ---------------------------------------------------------------------------
# Memory-element (register-file) writes
# ---------------------------------------------------------------------------
# A register-file / RAM write targets ONE element of a memory array, e.g.
# ``mem[waddr] <= wdata``. The engine-spec update carries an optional ``index``
# field: {"variable": "mem", "index": "waddr", "expression": "wdata"}. The LHS
# then renders as ``mem[waddr]`` for the RTL path (Compiler 2 emits a Verilog
# indexed assignment) while the base name ``mem`` is what is classified and
# declared. A scalar update has no ``index`` and renders as the bare variable.

#: An LHS of the form ``base[index]`` (a memory element). The base is a declared
#: array variable; the bracketed text is the (possibly compound) index expr.
#: Whitespace around / inside the brackets is tolerated so a key written
#: ``"mem [waddr]"`` parses identically here and in Compiler 1 (which uses the
#: same shape) — a divergence otherwise drops the write on one path but not the
#: other.
_INDEXED_LHS_RE = re.compile(r"^([A-Za-z_]\w*)\s*\[\s*(.+?)\s*\]$")


def _update_lhs(upd: dict) -> str:
    """Left-hand side of an update: ``mem[waddr]`` if indexed, else the var name.

    Pure. The index is spliced verbatim (it is an identifier or simple expr the
    free-input scanner already understands). Used so a memory-element write flows
    through the same composition machinery as a scalar update, keyed on the full
    indexed LHS so two actions writing different elements never collide.
    """
    var = upd.get("variable", "")
    index = upd.get("index")
    return f"{var}[{index}]" if index not in (None, "") else var


def _lhs_base(lhs: str) -> str:
    """Base variable of a (possibly indexed) LHS: ``mem[waddr]`` -> ``mem``."""
    m = _INDEXED_LHS_RE.match(lhs.strip())
    return m.group(1) if m else lhs.strip()


def _tla_primed_update(lhs: str, expr: str) -> str:
    """Render one update as a VALID TLA+ primed conjunct (abstract-TLC path).

    Scalar ``v``      -> ``v' = expr``.
    Indexed ``mem[i]`` -> ``mem' = [mem EXCEPT ![i] = expr]`` (``mem[i]' = e`` is
    not legal TLA+; the function-update form is). Pure. The RTL path instead
    emits ``mem[i]' = expr`` verbatim, which Compiler 2 turns into a Verilog
    indexed assignment — only the abstract model-checking text needs EXCEPT.
    """
    m = _INDEXED_LHS_RE.match(lhs.strip())
    if m:
        base, index = m.group(1), m.group(2)
        return f"{base}' = [{base} EXCEPT ![{index}] = {expr}]"
    return f"{lhs}' = {expr}"


def _build_update(key: str, value: str) -> dict:
    """Build one engine-spec update dict from a FormalSpec (key, value) pair.

    Scalar key ``"rdata"`` -> {"variable": "rdata", "expression": <value>}.
    Indexed key ``"mem[waddr]"`` (a register-file write) ->
        {"variable": "mem", "index": "waddr", "expression": <value>}.
    Word operators (AND/OR/NOT/mod) are folded to symbolic form in BOTH the index
    and the value, matching every other expression on the RTL path. Pure.
    """
    expr = _translate_bool_words(value)
    m = _INDEXED_LHS_RE.match(key.strip())
    if m:
        return {
            "variable": m.group(1),
            "index": _translate_bool_words(m.group(2)),
            "expression": expr,
        }
    return {"variable": key.strip(), "expression": expr}


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
        # A nested conditional in the THEN position would otherwise confuse
        # Compiler 2's depth-0 IF-splitter (its first ` ELSE ` would be the
        # INNER else). Parenthesise a conditional THEN value so the splitter
        # treats it atomically; Compiler 2's translate_expr strips the enclosing
        # paren and recurses (FIX 2). A leaf value is left bare (no regression).
        then_val = f"({value})" if value.strip().startswith("IF ") else value
        out = f"IF {guard} THEN {then_val} ELSE {out}"
    return out


def _ordered_assigned_vars(groups: list[list[dict]]) -> list[str]:
    """First-seen-ordered list of assignment LHSs across *groups* of updates.

    Keys on the FULL LHS via _update_lhs so a memory-element write keeps its index
    (``mem[waddr]``) through Alternation/SequentialComposition composition — two
    branches writing the same element compose into one driver, while writes to
    different elements stay distinct. Identity for a scalar update (no index), so
    every existing branch/step design is unchanged.
    """
    order: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for upd in group:
            v = _update_lhs(upd)
            if v and v not in seen:
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
                # Match on the FULL LHS so a memory-element write (mem[waddr])
                # composes under its indexed key, not the bare array name.
                if _update_lhs(upd) == var:
                    clauses.append((guard, upd["expression"]))
                    break
        # default = var: an un-guarded cycle holds the (possibly indexed) target.
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
            # Key on the FULL LHS so a memory-element write keeps its index
            # through sequential composition (identity for a scalar update).
            var = _update_lhs(upd)
            if not var:
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
    # Flat updates. Render the LHS with its memory index when present so a
    # register-file write (mem[waddr]) flows through composition/emit keyed on
    # the full element, holding via ELSE <mem[waddr]> exactly like a scalar reg.
    return [
        (_update_lhs(upd), upd["expression"])
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
        # in the flat updates list) are still detected as free inputs (G12). Also
        # scan the composed LHS: a memory-element write's address lives in the LHS
        # (mem[waddr]), NOT in its RHS (which is just `wdata`). Because every
        # composition path now keys the LHS via _update_lhs, scanning it recovers
        # the write address (`waddr`) regardless of whether the write is a flat
        # update, an Alternation branch, or a SequentialComposition step — the
        # declared base array (`mem`) is filtered out below, leaving the index.
        for lhs, expr in _action_update_exprs(action):
            exprs.append(expr or "")
            exprs.append(lhs or "")
        for expr in exprs:
            for ident in _scan_identifiers(expr):
                if ident in declared or ident in _RESERVED_IDENTIFIERS:
                    continue
                free.add(ident)
    return sorted(free)


# ---------------------------------------------------------------------------
# Free-input width inference (D2)
# ---------------------------------------------------------------------------
# A free input (see _free_inputs) has no declaration of its own, so it has no
# declared bit width. Defaulting every free input to 1 bit silently truncates
# any multi-bit external bus (a 2-bit ALU `op`, an 8-bit data input) — lint may
# even pass while the hardware is wrong. We resolve a free input's width from
# two sources, in priority order:
#   (1) An explicit Stage-1 SpecSummary port width (the design's declared truth,
#       e.g. `op` is 2-bit). Threaded in as `port_widths` from stage3.
#   (2) Inference from a register the input feeds directly: if `var' = din` and
#       `var` is W bits, `din` must be W bits or Verilog flags WIDTHEXPAND.
# Falling back to 1 only when neither applies.


def _infer_free_input_width(name: str, engine_spec: dict) -> int | None:
    """Width of a free input inferred from a register it feeds directly.

    If the free input is the ENTIRE right-hand side of an update whose target is
    a declared variable of width W (e.g. ``data' = din`` with ``data`` 8-bit),
    the free input must be W bits wide or Verilog flags WIDTHEXPAND. Returns the
    widest such W, or None if the free input never directly feeds a register.

    Uses the ungated composed updates (``_action_update_exprs``); it runs before
    any D5 guard-wrapping, so a bare-identifier RHS is still visible here.
    """
    var_widths = {
        v["name"]: int(v.get("width") or 1)
        for v in engine_spec.get("variables", [])
    }
    best: int | None = None
    for action in engine_spec.get("actions", []):
        for var, expr in _action_update_exprs(action):
            rhs = (expr or "").strip()
            while rhs.startswith("(") and rhs.endswith(")"):
                rhs = rhs[1:-1].strip()
            if rhs == name and var in var_widths:
                w = var_widths[var]
                if best is None or w > best:
                    best = w
    return best


def _free_input_width(
    name: str, engine_spec: dict, port_widths: dict | None,
) -> int:
    """Resolve a free input's bit width (priority: port hint → register feed → 1)."""
    if port_widths:
        w = port_widths.get(name)
        if w:
            return max(1, int(w))
    inferred = _infer_free_input_width(name, engine_spec)
    if inferred is not None:
        return inferred
    return 1


# ---------------------------------------------------------------------------
# Clocked-action guard → next-state gating (D5)
# ---------------------------------------------------------------------------
# A clocked action guarded by a non-trivial condition (e.g. a counter's Tick
# guarded by `en = 1`) must only update its registers when the guard holds;
# otherwise each register holds its prior value. The bridge previously emitted
# the update unconditionally, silently dropping the enable (BUG-18 lineage / D5:
# the counter counted every cycle regardless of `en`). We weave the guard in.

#: Relational-operator negations used to build the D5 hold-else form.
_REL_NEG: dict[str, str] = {
    "/=": "=", "<=": ">", ">=": "<", "<": ">=", ">": "<=", "=": "/=",
}


def _negate_guard(guard: str) -> str | None:
    """Negate a single-clause relational guard, or None if it can't be safely negated.

    Returns None when the guard is trivial (``TRUE``/empty → no gating needed)
    or is a conjunction/disjunction (De Morgan is out of scope — bail so the
    caller emits the update ungated rather than risk a wrong negation). Handles
    only the single relational forms the Tier-1 rules actually produce
    (``=``, ``/=``, ``<``, ``>``, ``<=``, ``>=``). Pure.
    """
    g = (guard or "").strip()
    if not g or g == "TRUE":
        return None
    if "/\\" in g or "\\/" in g:
        return None
    while g.startswith("(") and g.endswith(")"):
        g = g[1:-1].strip()
    # Two-char operators are listed first so they win over their one-char
    # prefixes at the same position; `.*?` picks the first operator.
    m = re.match(r"^(.*?)\s*(/=|<=|>=|=|<|>)\s*(.*)$", g)
    if not m:
        return None
    lhs, op, rhs = m.group(1).strip(), m.group(2), m.group(3).strip()
    if not lhs or not rhs:
        return None
    return f"{lhs} {_REL_NEG[op]} {rhs}"


def _clocked_update_exprs(action: dict) -> list[tuple[str, str]]:
    """(variable, rhs) pairs for a clocked action, with its guard woven in (D5).

    SUPERSEDED by `_compose_clocked_actions` (FIX 2), which composes guards
    ACROSS all clocked actions writing a variable (not just the single action's
    own guard) and emits the equivalent POSITIVE-guard form. The emit path no
    longer calls this; it is retained only for reference / direct unit use.
    `_negate_guard` and `_REL_NEG` exist solely to support this function.

    Emits ``IF <not guard> THEN <var> ELSE <update>`` — the negated-guard form,
    which keeps every THEN branch a simple leaf so Compiler 2's expression
    translator renders a clean nested ternary. (A guard in THEN position with a
    nested IF in it leaks untranslated IF/THEN/ELSE keywords through Compiler 2's
    structural splitter; the negated form sidesteps that. FIX 2 instead
    parenthesises a conditional THEN value and teaches translate_expr to strip
    the enclosing paren and recurse.)

    When the guard is trivial or cannot be safely negated, the update is emitted
    ungated — identical to the prior behaviour, so no regression. The reset
    action is handled separately by the caller and never passes through here.
    """
    pairs = _action_update_exprs(action)
    neg = _negate_guard(action.get("guard", "") or "")
    if neg is None:
        return pairs
    return [(var, f"IF {neg} THEN {var} ELSE {expr}") for var, expr in pairs]


# ---------------------------------------------------------------------------
# Cross-action clocked composition (FIX 2)
# ---------------------------------------------------------------------------
# Agent 3 frequently models one register's evolution as SEVERAL clocked actions
# guarded by mutually-exclusive conditions (e.g. a counter's Increment
# `count < 3 -> count + 1` and Wrap `count = 3 -> 0`). Each action is a separate
# entry in clocked_actions, and emitting each action's updates as its own flat
# `count' = ...` conjunct in the ELSE branch produces TWO drivers for `count`:
#
#     count <= count + 1;
#     count <= 0;          // last nonblocking assign wins -> count is always 0
#
# `_clocked_update_exprs` (D5) only weaves a SINGLE action's own guard in; it
# does not see the other actions writing the same variable. We must instead
# compose ACROSS the clocked actions into ONE guarded next-state per variable.
#
# This SUBSUMES the per-action D5 loop for the common cases:
#   * one clocked action, guard TRUE  -> default = its expr, no IF (no change)
#   * one clocked action, guard `en=1` -> IF en=1 THEN expr ELSE var
#     (positive-guard form; equivalent to the old negated `IF en/=1 THEN var
#     ELSE expr`, but expressed directly)
#   * many clocked actions on one var  -> nested IF in priority order


def _compose_clocked_actions(
    clocked_actions: list[dict],
) -> list[tuple[str, str]]:
    """Compose every clocked action into one guarded next-state RHS per variable.

    For each assigned variable (first-seen order across all actions), collect
    each clocked action's (guard, expr) for that variable in ACTION order. The
    composed RHS is a nested conditional:

        IF g1 THEN e1 ELSE IF g2 THEN e2 ELSE ... ELSE <default>

    where the clauses are the NON-``TRUE``-guarded (guard, expr) pairs in
    priority order, and ``<default>`` is the first ``TRUE``-guarded action's
    expression for that variable (an unconditional write), or — if no action
    writes it unconditionally — the variable itself (register hold). This means:

      * A single TRUE-guarded action collapses to ``default = expr`` with NO IF
        (identical to the old single-action emit — no regression).
      * A single non-TRUE-guarded action (the D5 ``en = 1`` case) yields
        ``IF en = 1 THEN expr ELSE var`` — the positive-guard form, equivalent
        to the old negated-guard ``IF en /= 1 THEN var ELSE expr``.
      * Multiple mutually-exclusive actions compose into one nested-IF, so there
        is exactly ONE driver per variable (FIX 2).

    Positive-guard nested-IFs nest in the ELSE chain and keep every THEN branch
    a leaf, which is exactly what Compiler 2's _split_if_then_else handles.
    Branch-/step-composed (Alternation / SequentialComposition) RHS is honoured
    via `_action_update_exprs`. Pure and deterministic.
    """
    # First-seen variable order across all clocked actions' composed updates.
    var_order = _ordered_assigned_vars(
        [
            [{"variable": v} for v, _ in _action_update_exprs(a)]
            for a in clocked_actions
        ]
    )

    # Index each action's composed (var -> expr) once for lookup.
    per_action: list[tuple[str, dict[str, str]]] = []
    for action in clocked_actions:
        guard = (action.get("guard", "TRUE") or "TRUE").strip() or "TRUE"
        per_action.append((guard, dict(_action_update_exprs(action))))

    result: list[tuple[str, str]] = []
    for var in var_order:
        clauses: list[tuple[str, str]] = []
        default: str | None = None
        for guard, updates in per_action:
            if var not in updates:
                continue
            expr = updates[var]
            if guard == "TRUE":
                # First unconditional write becomes the fall-through default.
                if default is None:
                    default = expr
            else:
                clauses.append((guard, expr))
        result.append((var, _nested_if(clauses, default if default is not None else var)))
    return result


# ---------------------------------------------------------------------------
# Forward bridge: FormalSpec → engine spec
# ---------------------------------------------------------------------------

def formal_spec_to_engine_spec(spec: FormalSpec) -> dict:
    """
    Convert a FormalSpec to the engine's internal spec dict.

    All variables start as abstract=True with no reset_value. The refinement
    rules add reset_value (Initialization), clocked (Iteration), etc.
    """
    # A combinational transition (Transition.combinational) defines continuous
    # logic — its target signals are wires, not registers. Collect their base
    # names so the matching variables are born concrete (no Iteration needed) and
    # carved out of the reset requirement, symmetric to a memory's depth carve-out.
    comb_targets: set[str] = set()
    for t in spec.transitions:
        if getattr(t, "combinational", False):
            for k in t.updates.keys():
                comb_targets.add(_lhs_base(k))

    variables = [
        {
            "name": name,
            "type": var.type,
            # Carry the declared bit width through the engine spec so the RTL
            # emitter (engine_spec_to_rtl_tla → Compiler 2) can size the signal.
            # Without this, multi-bit signals silently truncate to 1 bit (BUG-17).
            "width": var.width,
            # Carry the memory depth: a variable with depth set is an array
            # (register file / RAM), emitted as `reg [w-1:0] name [0:depth-1]`,
            # never a port, and not reset (engine.is_rtl_style carve-out).
            "depth": getattr(var, "depth", None),
            # A combinational output (driven only by a continuous `assign`) is a
            # wire, not a register: born concrete (abstract=False, never Iterated),
            # never reset. is_rtl_style honours both flags.
            "combinational": name in comb_targets,
            "abstract": name not in comb_targets,
            "reset_value": None,
            "clocked": False,
        }
        for name, var in spec.variables.items()
    ]

    # FIX 3: normalise English boolean operators (AND/OR/NOT) to TLA+ symbolic
    # form in every guard and update expression, so the RTL path (bridge ->
    # Compiler 2) and the free-input scanner see operators, not words. The
    # Compiler-1/TLC path already does its own translation, so this is purely
    # for the engine-spec / RTL branch. Idempotent on symbolic input.
    actions = [
        {
            "name": t.label,
            "guard": _translate_bool_words(t.condition),
            "updates": [
                _build_update(k, v)
                for k, v in t.updates.items()
            ],
            "is_rtl_style": False,
            "clocked": False,
            # A combinational transition becomes CombinationalLogic (an `assign`),
            # never a clocked register update. Iteration skips it; is_rtl_style
            # accepts it unclocked.
            "combinational": bool(getattr(t, "combinational", False)),
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

def engine_spec_to_rtl_tla(
    engine_spec: dict,
    module_name: str,
    port_widths: dict | None = None,
    reset_port: str = "reset",
    reset_active_low: bool = False,
) -> str:
    """
    Convert a post-refinement engine spec dict to RTL-style TLA+ text.

    Compiler 2 looks for three sections: VARIABLES, CombinationalLogic,
    UpdatePipeline. This function emits exactly those sections from the
    refined engine spec.

    Args:
        engine_spec: RTL-style engine spec (output of engine.run()).
        module_name: TLA+ module name (used in the module header).
        port_widths: optional {name: width} hints for free inputs, sourced from
            the Stage-1 SpecSummary ports. A free input listed here is sized to
            the declared width instead of inferred/defaulted (D2).
        reset_port: the design's actual reset input name (FIX 1). The cocotb
            generator drives ``dut.<reset_port>``; Compiler 2 must emit a port of
            the same name or the reset floats. Defaults to "reset" so existing
            specs that name their reset "reset" are unchanged. This name is used
            for the ``(reset_port, 1)`` VARIABLES entry and the
            ``IF {reset_port} = 1 THEN`` reset condition in UpdatePipeline; the
            matching Compiler-2 instance must be constructed with the same
            reset_port.
        reset_active_low: reset polarity (FIX RC1). When True the reset is
            asserted at 0, so the emitted reset condition becomes
            ``IF {reset_port} = 0 THEN``; when False (default) it stays
            ``IF {reset_port} = 1 THEN`` (active-high). This value is cosmetic for
            codegen on its own — Compiler 2 derives the actual Verilog reset test
            from its own ``reset_active_low`` flag — but keeping the TLA condition
            consistent with the Verilog avoids a confusing spec/RTL mismatch. The
            matching Compiler-2 instance MUST be constructed with the same
            reset_active_low.

    Returns:
        TLA+ source string ready for Compiler 2.
    """
    variables = engine_spec.get("variables", [])
    # FIX 1: Agent 3 sometimes models the reset (or clock) as a STATE variable,
    # which would otherwise be emitted as a bogus `output reg <reset>` / `output
    # reg clk`. The reset and clock are PORTS, not registers — drop any engine
    # variable whose name collides with the reset port or "clk" so the canonical
    # port declarations below are the only source of those names.
    variables = [
        v for v in variables if v.get("name") not in (reset_port, "clk")
    ]
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
        (v["name"], int(v.get("width") or 1), v.get("depth")) for v in variables
    ] + [("clk", 1, None), (reset_port, 1, None)]

    # Free inputs (BUG-18): identifiers used in guards/update expressions that
    # are NOT declared variables, clk/reset, or TLA+ keywords. Without this they
    # never reach the VARIABLES block, so Compiler 2 emits Verilog referencing an
    # undeclared wire (e.g. `d` on a DFF -> iverilog "Unable to bind wire `d'";
    # a guard-only `en` is silently dropped). Declaring them here lets Compiler
    # 2's "not driven by either block -> input port" classifier expose them as
    # inputs. Each free input is sized via _free_input_width (D2): a Stage-1
    # SpecSummary port hint, else inference from a register it directly feeds,
    # else 1. Sorted (by _free_inputs) for deterministic output.
    declared = {name for name, *_ in sized_vars}
    free_inputs = _free_inputs(engine_spec, declared)
    sized_vars += [
        (name, _free_input_width(name, engine_spec, port_widths), None)
        for name in free_inputs
    ]

    lines.append("VARIABLES")
    for i, (name, width, depth) in enumerate(sized_vars):
        comma = "," if i < len(sized_vars) - 1 else ""
        # A memory array carries its depth in the same comment channel as width
        # (BUG-17): "\* width: W depth: K". Compiler 2 reads both and emits
        # `reg [W-1:0] name [0:K-1]`. Scalars omit the depth note (depth None).
        depth_note = f" depth: {depth}" if depth else ""
        lines.append(f"    {name}{comma}  \\* width: {width}{depth_note}")
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
        elif _is_identity_hold(action):
            # A non-clocked pure register-hold (e.g. Hold: acc' = acc) emits nothing
            # distinct — the register already holds via the ELSE branch of its
            # clocked driver. Emitting it into CombinationalLogic would double-drive
            # the register (MultiDriverError). Drop it. (engine.is_rtl_style likewise
            # does not require such an action to be clocked, so the design converges
            # even when the Rule Picker never iterates a dedicated Hold action.)
            continue
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
        # Reset-asserted level: 0 for active-low, 1 for active-high (default).
        # _split_reset_else in Compiler 2 matches "IF {reset_port}" value-agnostic,
        # so emitting "= 0" does not break reset/else splitting.
        reset_level = 0 if reset_active_low else 1
        lines.append(f"    /\\ IF {reset_port} = {reset_level} THEN")
        for var, expr in _action_update_exprs(reset_action):
            lines.append(f"          /\\ {var}' = {expr}")
        lines.append("       ELSE")
        # FIX 2: compose ACROSS all clocked actions into one guarded next-state
        # per variable, so several actions writing the same register (e.g.
        # Increment + Wrap on `count`) become a single nested-IF driver instead
        # of colliding nonblocking assigns (last-wins). This subsumes the old
        # per-action D5 guard-weaving for the single-action cases.
        for var, expr in _compose_clocked_actions(clocked_actions):
            lines.append(f"          /\\ {var}' = {expr}")
    else:
        # No reset action yet — emit composed clocked updates flat (partial
        # refinement). Still composed across actions (FIX 2) so a multi-action
        # register does not double-drive even without a reset branch.
        for var, expr in _compose_clocked_actions(clocked_actions):
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
        # A memory-element write's LHS is `mem[waddr]`; UNCHANGED must list the
        # BASE array name, and the conjunct must use the TLA+ EXCEPT form
        # (`mem[i]' = e` is illegal). _tla_primed_update / _lhs_base handle both.
        updated_vars = {_lhs_base(var) for var, _ in composed}
        unchanged = [v for v in var_names if v not in updated_vars]

        lines.append(f"{aname} ==")
        lines.append(f"    /\\ {guard}")
        for var, expr in composed:
            lines.append(f"    /\\ {_tla_primed_update(var, expr)}")
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
