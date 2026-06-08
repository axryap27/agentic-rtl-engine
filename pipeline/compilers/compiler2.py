"""
Compiler 2 — RTL-style TLA+ → Verilog-2001.

This module is Compiler 2 in the agentic-rtl-engine pipeline.  It takes
RTL-style TLA+ produced in-memory by
``pipeline/refinement/bridge.py:engine_spec_to_rtl_tla()`` (called from
Stage 3, ``pipeline/nodes/stage3.py``) and emits synthesizable Verilog-2001.
This RTL-style TLA+ is generated in memory and is not read from any single
artifact JSON file.

Public entry point
------------------
    compile_tla_to_verilog(tla_source: str, module_name: str) -> str

The function raises ``BanlistViolation`` if the generated Verilog contains
any SystemVerilog or non-synthesizable construct from the hard banlist.
That check runs *before* the source is returned to the caller — nothing
banned ever touches disk.

Expected TLA+ input format
--------------------------
The compiler looks for exactly three named sections:

  VARIABLES block
      Every name is classified and emitted based on how it is driven:
        clk, reset         → module input ports (always)
        hw_*               → verification-only tracking vars, dropped entirely
        r_* prefix         → internal registers (never exposed as ports)
        driven by CombinationalLogic only → output port (wire, driven by assign)
        driven by UpdatePipeline only     → output port (reg, driven by always block)
        not driven by either block        → input port (externally driven)

  CombinationalLogic ==
      Conjuncts of the form:
          /\\ wire' = <expr>
      Compiled to continuous assign statements:
          assign wire = <vexpr>;

  UpdatePipeline ==
      Must follow this skeleton (outer IF on its own ELSE line):
          /\\ clk' = ...                   (always skipped)
          /\\ IF reset = 1 THEN
                 /\\ reg' = <literal>
                 ...
             ELSE
                 /\\ reg' = <expr>         (nested IF-THEN-ELSE allowed)
                 ...
      Compiled to:
          always @(posedge clk) begin
              if (reset) begin
                  reg <= literal;
              end else begin
                  reg <= ternary_expr;
              end
          end

TLA+ expression translations
-----------------------------
  /\\   →  &&
  \\/   →  ||
  /=    →  !=
  =     →  ==   (inside expressions only; not on the LHS of a primed assignment)
  IF a THEN b ELSE c  →  (a_v) ? (b_v) : (c_v)   (applied recursively)
  << >>               →  /* FORMAL_ONLY */         (dropped)
  Append(...)         →  /* FORMAL_ONLY */         (dropped)

Output
------
Synthesizable Verilog-2001 only — no SystemVerilog constructs.
  - Full port list inferred from variable declarations and which blocks drive them
  - reg  for internal registers (r_* prefix) and output reg ports
  - wire for output wire ports and undeclared combinational signals
  - always @(posedge clk) for clocked blocks
  - always @(*) for combinational blocks (not used currently; reserved)
  - No logic, no always_ff / always_comb / always_latch, no initial in synth
    modules, no #delay statements
"""

import re
import sys
from typing import Optional


# ---------------------------------------------------------------------------
# Undeclared-identifier defense (BUG-18)
# ---------------------------------------------------------------------------
# After expression translation, an emitted RHS contains only Verilog operators,
# numeric literals, and identifiers. Any identifier that is not a declared
# signal (and not a Verilog reserved word / leaked TLA+ keyword) is an
# externally-driven input that was never declared — e.g. `d` on a hand-written
# DFF spec fed straight to Compiler 2, bypassing the bridge. Emitting a module
# that references such a wire produces un-elaboratable Verilog (iverilog:
# "Unable to bind wire `d'"). This module's defensive pass declares any such
# identifier as a scalar input so the emitted module always elaborates.
#
# The reserved set mirrors the surface keywords the bridge excludes
# (bridge._RESERVED_IDENTIFIERS) plus Verilog primitives that can appear in a
# translated expression (the ternary leaves no keywords, but a partially
# translated nested IF can leak IF/THEN/ELSE — we must not declare those as
# ports). Keep this in sync with the translator's vocabulary.

_RESERVED_VERILOG_IDENTIFIERS: frozenset = frozenset({
    # TLA+ keywords that may leak through a partial translation
    "IF", "THEN", "ELSE", "TRUE", "FALSE", "UNCHANGED",
    "CASE", "OTHER", "LET", "IN",
    # Word-form boolean operators
    "AND", "OR", "NOT",
    # Verilog-2001 reserved words that could appear in an emitted RHS
    "begin", "end", "posedge", "negedge", "assign", "reg", "wire",
    "input", "output", "module", "endmodule", "always",
})

#: Identifier token (optionally primed); numeric literals never match.
_VERILOG_IDENT_RE = re.compile(r"[A-Za-z_]\w*'?")


def _scan_verilog_identifiers(expr: str) -> set:
    """Return the set of (unprimed) identifiers in a (translated) expression."""
    out: set = set()
    for tok in _VERILOG_IDENT_RE.findall(expr or ""):
        out.add(tok[:-1] if tok.endswith("'") else tok)
    return out


# ---------------------------------------------------------------------------
# Verilog-2001 banlist verifier
# ---------------------------------------------------------------------------
# Checks emitted Verilog *code* (not comments) for SystemVerilog and
# non-synthesizable constructs.  Called by compile_tla_to_verilog() before
# returning -- nothing banned ever leaves this module.
#
# Strategy: strip line comments (//...) and block comments (/*...*/) first,
# then apply word-boundary or pattern checks on the stripped code only.
# This avoids false positives from ban-words that appear in comment headers.


class BanlistViolation(ValueError):
    """Raised when emitted Verilog contains a banned construct."""


class MultiDriverError(ValueError):
    """Raised when a variable is sourced by BOTH the combinational and the
    sequential block (G05).

    Such a variable would receive a continuous `assign` (from
    CombinationalLogic) *and* a procedural `<=` (from UpdatePipeline) on the
    same net -- an illegal multi-driver that fails elaboration
    (iverilog: "cannot be driven by a continuous assignment";
    verilator: BLKANDNBLK / MULTIDRIVEN). This is a build-time codegen error,
    not a prompt-retry signal: the input spec is internally inconsistent and
    must be fixed upstream, never emitted as a double-driver.
    """


# Each entry is (label, compiled_regex).  Regex applied to comment-stripped
# source.  Word-boundary anchors prevent matching inside longer identifiers.
_BANLIST: list[tuple[str, re.Pattern]] = [
    ("'logic' (SystemVerilog keyword -- use reg/wire)",
     re.compile(r"\blogic\b")),
    ("'always_ff' (SystemVerilog -- use always @(posedge clk))",
     re.compile(r"\balways_ff\b")),
    ("'always_comb' (SystemVerilog -- use always @(*))",
     re.compile(r"\balways_comb\b")),
    ("'always_latch' (SystemVerilog)",
     re.compile(r"\balways_latch\b")),
    ("'interface' (SystemVerilog)",
     re.compile(r"\binterface\b")),
    ("'modport' (SystemVerilog)",
     re.compile(r"\bmodport\b")),
    ("'typedef' (SystemVerilog)",
     re.compile(r"\btypedef\b")),
    ("'initial' block (not allowed in synthesizable modules)",
     re.compile(r"\binitial\b")),
    ("#delay construct (not synthesizable)",
     re.compile(r"#\s*\d")),
    ("'$' system task (simulation only)",
     re.compile(r"\$\w+")),
    # --- Leaked TLA+ keywords (G04) ---
    # These are CASE-SENSITIVE and uppercase-only: a translated expression
    # never contains uppercase IF/THEN/ELSE/IN/LET/CASE (translate_expr lowers
    # IF-THEN-ELSE into a ternary), so any surviving uppercase token is a
    # non-synthesizable TLA+ construct that leaked past the translator.
    # Lowercase Verilog `if`/`else`/`case`/`casez`/`casex` are legal inside
    # always blocks and MUST NOT match -- hence no re.IGNORECASE.
    ("leaked TLA+ keyword 'IF' (use lowercase if / a ternary)",
     re.compile(r"\bIF\b")),
    ("leaked TLA+ keyword 'THEN' (untranslated IF-THEN-ELSE)",
     re.compile(r"\bTHEN\b")),
    ("leaked TLA+ keyword 'ELSE' (use lowercase else / a ternary)",
     re.compile(r"\bELSE\b")),
    ("leaked TLA+ keyword 'IN' (untranslated LET ... IN)",
     re.compile(r"\bIN\b")),
    ("leaked TLA+ keyword 'LET' (no synthesizable equivalent)",
     re.compile(r"\bLET\b")),
    ("leaked TLA+ keyword 'CASE' (untranslated TLA+ CASE; lowercase case is legal)",
     re.compile(r"\bCASE\b")),
    # Bare FORMAL_ONLY operand: the translator only ever emits FORMAL_ONLY
    # wrapped in a /* ... */ comment, which _strip_comments removes before
    # matching. A bare FORMAL_ONLY surviving comment-stripping means a
    # formal-only construct leaked into synthesizable code as an operand.
    ("bare 'FORMAL_ONLY' operand (formal-only construct leaked into RTL)",
     re.compile(r"\bFORMAL_ONLY\b")),
]


def _strip_comments(verilog: str) -> str:
    """
    Remove Verilog line comments (//) and block comments (/* ... */).

    Preserves newlines so line numbers remain meaningful.
    Does NOT handle string literals (none expected in synthesizable RTL).
    """
    # Remove block comments, preserving embedded newlines
    result = re.sub(
        r"/\*.*?\*/",
        lambda m: "\n" * m.group(0).count("\n"),
        verilog,
        flags=re.DOTALL,
    )
    # Remove line comments
    result = re.sub(r"//[^\n]*", "", result)
    return result


def verify_banlist(verilog: str) -> None:
    """
    Scan *verilog* for banned constructs (comment-stripped).

    Raises BanlistViolation with a precise message on the first hit.
    This is a build-time gate -- not an LLM retry.
    """
    code = _strip_comments(verilog)
    for label, pattern in _BANLIST:
        m = pattern.search(code)
        if m:
            token = m.group(0)
            pre = verilog.find(token)
            line_no = verilog[:pre].count("\n") + 1 if pre != -1 else "?"
            raise BanlistViolation(
                f"Banlist violation -- {label}\n"
                f"  Matched: {token!r}  (approx. line {line_no} in emitted source)"
            )


# ---------------------------------------------------------------------------
# Public entry point (pinned signature)
# ---------------------------------------------------------------------------

def compile_tla_to_verilog(
    tla_source: str, module_name: str, reset_port: str = "reset",
) -> str:
    """
    Compiler 2 public entry point.

    Translate RTL-style TLA+ *tla_source* to a synthesizable Verilog-2001
    string.  Runs the banlist verifier before returning; raises
    BanlistViolation if any banned construct is found in the emitted code.

    Args:
        tla_source:  RTL-style TLA+ text (output of Refinement Engine).
        module_name: Verilog module name for the emitted module.
        reset_port:  the design's reset input name (FIX 1). Must match the
            reset_port passed to ``engine_spec_to_rtl_tla`` so the
            ``IF {reset_port} = 1 THEN`` reset block is recognised and the reset
            port is emitted under the correct name. Defaults to "reset".

    Returns:
        Synthesizable Verilog-2001 source as a string.

    Raises:
        BanlistViolation: if emitted code contains a banned construct.
    """
    verilog = RTLTLACompiler(tla_source, reset_port=reset_port).compile(module_name)
    verify_banlist(verilog)
    return verilog


# ---------------------------------------------------------------------------
# Canonical RTL-style TLA+ specification format
# ---------------------------------------------------------------------------
# This is the template produced by the refinement pipeline (Stage 2) and
# consumed by this compiler (Stage 3).  The SAMPLE_TLA below also serves as
# a runnable self-test fixture.

SAMPLE_TLA = r"""
-------------------- MODULE PipelineProcessor --------------------
EXTENDS Integers, Sequences

\* Formal verification domain (compiler ignores)
CONSTANTS DataDomain

\* ----------------------------------------------------------------
\* VARIABLE DECLARATIONS
\* Every name here becomes a Verilog signal.
\* Exceptions: clk/reset -> ports; hw_* -> verification only.
\* ----------------------------------------------------------------
VARIABLES
    clk, reset,
    \* AXI-S input interface
    in_valid, in_ready, in_a, in_b,
    \* AXI-S output interface
    out_valid, out_ready, out_data,
    \* Pipeline stage 1
    r_stg1_valid, r_stg1_mult,
    \* Pipeline stage 2
    r_stg2_valid, r_stg2_acc,
    \* Verification tracking (compiler drops these)
    hw_in_history, hw_out_history

hw_vars == <<clk, reset, in_valid, in_ready, in_a, in_b,
             out_valid, out_ready, out_data,
             r_stg1_valid, r_stg1_mult, r_stg2_valid, r_stg2_acc,
             hw_in_history, hw_out_history>>

\* ----------------------------------------------------------------
\* INIT (formal only – not compiled)
\* ----------------------------------------------------------------
Init ==
    /\ clk = 0 /\ reset = 1
    /\ in_valid = 0 /\ in_a = 0 /\ in_b = 0
    /\ out_ready = 1
    /\ r_stg1_valid = 0 /\ r_stg1_mult = 0
    /\ r_stg2_valid = 0 /\ r_stg2_acc = 0
    /\ hw_in_history = << >> /\ hw_out_history = << >>

\* ----------------------------------------------------------------
\* COMBINATIONAL LOGIC  →  assign statements
\* ----------------------------------------------------------------
CombinationalLogic ==
    /\ in_ready'  = (r_stg1_valid = 0 \/ out_ready = 1)
    /\ out_valid' = r_stg2_valid
    /\ out_data'  = r_stg2_acc

\* ----------------------------------------------------------------
\* SEQUENTIAL LOGIC  →  always @(posedge clk) block
\* ----------------------------------------------------------------
UpdatePipeline ==
    /\ clk' = 1 - clk
    /\ IF reset = 1 THEN
          /\ r_stg1_valid' = 0
          /\ r_stg1_mult'  = 0
          /\ r_stg2_valid' = 0
          /\ r_stg2_acc'   = 0
          /\ hw_in_history'  = << >>
          /\ hw_out_history' = << >>
       ELSE
          \* Verification shadow (compiler drops hw_*)
          /\ hw_in_history' = IF (in_valid = 1 /\ in_ready = 1)
                              THEN Append(hw_in_history, <<in_a, in_b>>)
                              ELSE hw_in_history

          \* Stage 1: multiply
          /\ r_stg1_valid' = IF (in_valid = 1 /\ in_ready = 1) THEN 1
                             ELSE IF (r_stg2_valid = 0 \/ out_ready = 1) THEN 0
                             ELSE r_stg1_valid
          /\ r_stg1_mult'  = IF (in_valid = 1 /\ in_ready = 1) THEN (in_a * in_b)
                             ELSE r_stg1_mult

          \* Stage 2: accumulate
          /\ r_stg2_valid' = IF (r_stg1_valid = 1 /\ r_stg2_valid = 0) THEN 1
                             ELSE IF (out_ready = 1) THEN 0
                             ELSE r_stg2_valid
          /\ r_stg2_acc'   = IF (r_stg1_valid = 1 /\ r_stg2_valid = 0)
                             THEN (r_stg2_acc + r_stg1_mult)
                             ELSE r_stg2_acc

          /\ hw_out_history' = IF (out_valid = 1 /\ out_ready = 1)
                               THEN Append(hw_out_history, out_data)
                               ELSE hw_out_history

\* ----------------------------------------------------------------
\* FORMAL VERIFICATION (compiler completely ignores Next and Spec)
\* ----------------------------------------------------------------
Next ==
    /\ CombinationalLogic
    /\ UpdatePipeline
    /\ \E va, vb \in DataDomain, vld, rdy \in {0,1} :
        /\ in_a'    = va
        /\ in_b'    = vb
        /\ in_valid' = vld
        /\ out_ready' = rdy

Spec == Init /\ [][Next]_hw_vars
=================================================================
"""


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------

class RTLTLACompiler:
    """Compiles RTL-style TLA+ to synthesizable Verilog-2001."""

    # Variables whose names start with these prefixes are internal registers,
    # never exposed as ports regardless of which blocks drive them.
    _INTERNAL_PREFIXES = ("r_",)

    # Verification-only variable prefix — dropped entirely from Verilog output
    _VERIFY_VAR = re.compile(r"^hw_")

    def __init__(self, tla_code: str, reset_port: str = "reset"):
        self.tla_code = tla_code
        # FIX 1: the reset port name is configurable so it matches the cocotb
        # generator's `dut.<reset_port>`. clk + this reset name are the fixed
        # input ports (formerly the class-level frozenset _FIXED_INPUTS). Made
        # an instance value so the reset name flows into every classification,
        # range, port-decl, and undeclared-input check. Defaults to "reset".
        self.reset_port = reset_port
        self._FIXED_INPUTS = frozenset(["clk", reset_port])
        self.variables: list[str] = []
        # Per-variable bit width, captured from the "\* width: N" comment the
        # bridge attaches to each VARIABLES entry (BUG-17). Defaults to 1.
        self.widths: dict[str, int] = {}
        self.comb_vars: set[str] = set()   # driven by CombinationalLogic
        self.seq_vars: set[str] = set()    # driven by UpdatePipeline

    # ------------------------------------------------------------------
    # Helpers: variable classification
    # ------------------------------------------------------------------

    def _is_verify(self, v: str) -> bool:
        return bool(self._VERIFY_VAR.match(v))

    def _is_internal_reg(self, v: str) -> bool:
        """True for variables that are always internal (r_* naming convention)."""
        return any(v.startswith(p) for p in self._INTERNAL_PREFIXES)

    def _emit(self, v: str) -> bool:
        """True when this variable participates in any Verilog logic."""
        return v not in self._FIXED_INPUTS and not self._is_verify(v)

    # Classification tokens returned by _classify():
    #   'fixed_input'   → clk, reset
    #   'input'         → port, driven externally
    #   'output_wire'   → port, driven by assign (CombinationalLogic)
    #   'output_reg'    → port, driven by always block (UpdatePipeline)
    #   'internal_reg'  → r_* register, never a port
    #   'internal_wire' → internal wire not driven by either block
    #   'drop'          → hw_*, completely omitted

    def _classify(self, v: str) -> str:
        if v in self._FIXED_INPUTS:
            return "fixed_input"
        if self._is_verify(v):
            return "drop"
        if self._is_internal_reg(v):
            return "internal_reg"
        if v in self.seq_vars:
            return "output_reg"
        if v in self.comb_vars:
            return "output_wire"
        return "input"  # not driven by this module → externally supplied

    # ------------------------------------------------------------------
    # Step 1: extract VARIABLES
    # ------------------------------------------------------------------

    def extract_variables(self) -> list[str]:
        # Stop at the next TLA+ definition (word followed by ==), the module
        # terminator (====), or end of string.  Do NOT stop on a bare word
        # followed only by a newline -- that would prematurely truncate the
        # VARIABLES block when the last variable name sits on its own line.
        m = re.search(
            r"VARIABLES\s+([\s\S]*?)(?=\n\s*\w+\s*==|\n={4,}|\Z)",
            self.tla_code,
        )
        if not m:
            return []
        raw = m.group(1)
        # Capture per-line "\* width: N" comments BEFORE stripping comments, so
        # we can associate each width with the variable name on the same line
        # (BUG-17). The bridge emits one variable per line as
        #   "    <name>,  \* width: <N>".
        for line in raw.split("\n"):
            wm = re.search(r"\\\*\s*width:\s*(\d+)", line)
            if not wm:
                continue
            # The variable name is the first \w+ token before the comment.
            code_part = line.split("\\*", 1)[0]
            nm = re.search(r"\b(\w+)\b", code_part)
            if nm:
                self.widths[nm.group(1)] = int(wm.group(1))
        raw = re.sub(r"\\\*[^\n]*", "", raw)          # strip \* comments
        raw = re.sub(r"\(\*[\s\S]*?\*\)", "", raw)    # strip (* *) comments
        self.variables = [t for t in re.split(r"[\s,]+", raw) if re.match(r"^\w+$", t or "")]
        return self.variables

    def _range(self, v: str) -> str:
        """Verilog bit-range prefix for a variable, e.g. '[1:0] '.

        Returns '' for single-bit (width <= 1) signals so scalar declarations
        stay clean. clk/reset are always scalar.
        """
        w = self.widths.get(v, 1)
        if v in self._FIXED_INPUTS or w <= 1:
            return ""
        return f"[{w - 1}:0] "

    # ------------------------------------------------------------------
    # Step 2: expression translator
    # ------------------------------------------------------------------

    def _split_if_then_else(self, text: str) -> Optional[tuple[str, str, str]]:
        """
        Split 'IF cond THEN a ELSE b' at depth-0 THEN/ELSE keywords.
        Returns (cond, then_val, else_val) or None.
        """
        if not text.startswith("IF "):
            return None
        rest = text[3:]

        # Locate ' THEN ' at parenthesis depth 0
        depth = i = 0
        then_pos = None
        while i < len(rest):
            c = rest[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            elif depth == 0 and rest[i : i + 6] == " THEN ":
                then_pos = i
                break
            i += 1
        if then_pos is None:
            return None

        cond = rest[:then_pos].strip()
        after_then = rest[then_pos + 6 :]

        # Locate ' ELSE ' at depth 0
        depth = j = 0
        else_pos = None
        while j < len(after_then):
            c = after_then[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            elif depth == 0 and after_then[j : j + 6] == " ELSE ":
                else_pos = j
                break
            j += 1
        if else_pos is None:
            return None

        then_val = after_then[:else_pos].strip()
        else_val = after_then[else_pos + 6 :].strip()
        return cond, then_val, else_val

    def _translate_basic(self, expr: str) -> str:
        """Translate leaf TLA+ expression (no IF-THEN-ELSE) to Verilog."""
        expr = expr.strip()
        # Drop formal-only constructs
        if expr.startswith("Append(") or expr in ("<< >>", "<<>>"):
            return "/* FORMAL_ONLY */"
        expr = re.sub(r"<<\s*>>", "/* FORMAL_ONLY */", expr)
        # FIX 3: defensive word-form boolean operators in case any leak past the
        # bridge's _translate_bool_words (e.g. hand-written TLA+ fed straight to
        # Compiler 2). Word-boundary anchored so identifiers are untouched. These
        # run BEFORE the symbolic passes so a translated `&&`/`||`/`!` is final.
        expr = re.sub(r"\bAND\b", "&&", expr)
        expr = re.sub(r"\bOR\b", "||", expr)
        expr = re.sub(r"\bNOT\b", "!", expr)
        # Logical operators
        expr = re.sub(r"/\\", "&&", expr)
        expr = re.sub(r"\\/", "||", expr)
        # TLA+ boolean NOT (~) → Verilog logical NOT (!). Must run before the
        # `=`→`==` pass; `~` never collides with the relational operators.
        expr = expr.replace("~", "!")
        # Not-equal before equal
        expr = expr.replace("/=", "!=")
        # = → == (skip already-translated == != <= >=)
        expr = re.sub(r"(?<![!<>=])=(?!=)", "==", expr)
        return expr

    @staticmethod
    def _strip_enclosing_parens(expr: str) -> Optional[str]:
        """If *expr* is fully wrapped in one matching paren pair, return the inner
        text; otherwise None.

        "Fully wrapped" means the leading ``(`` and trailing ``)`` are matched to
        each other (depth returns to 0 only at the very end). This lets
        translate_expr recurse into a parenthesised conditional such as
        ``(IF c THEN a ELSE b)`` — emitted by the bridge when a guarded clocked
        action's expression is itself a conditional placed in a THEN branch
        (FIX 2). Without this, the leading ``(`` hides the ``IF`` from the
        IF-splitter and the keyword leaks past the translator. Pure.
        """
        if len(expr) < 2 or expr[0] != "(" or expr[-1] != ")":
            return None
        depth = 0
        for i, ch in enumerate(expr):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    # Closing paren for the leading '(' must be the final char,
                    # else the parens are not fully enclosing (e.g. "(a)+(b)").
                    return expr[1:-1] if i == len(expr) - 1 else None
        return None

    def translate_expr(self, expr: str) -> str:
        """Recursively translate a TLA+ expression to Verilog."""
        expr = expr.strip()
        if expr.startswith("IF "):
            parts = self._split_if_then_else(expr)
            if parts:
                cond, then_val, else_val = parts
                c = self._translate_basic(cond)
                t = self.translate_expr(then_val)
                e = self.translate_expr(else_val)
                return f"({c}) ? ({t}) : ({e})"
        # A parenthesised conditional `(IF ... )` does not start with `IF `, so the
        # branch above misses it. Strip one fully-enclosing paren pair and recurse
        # when the inner is itself a conditional, preserving the grouping with an
        # outer paren around the resulting ternary (FIX 2). Non-conditional
        # parenthesised exprs are left to _translate_basic unchanged (no regression).
        inner = self._strip_enclosing_parens(expr)
        if inner is not None and inner.strip().startswith("IF "):
            return f"({self.translate_expr(inner)})"
        return self._translate_basic(expr)

    # ------------------------------------------------------------------
    # Step 3: block extraction helpers
    # ------------------------------------------------------------------

    def _extract_block(self, name: str) -> Optional[str]:
        """Return the body text of a TLA+ named definition."""
        pat = rf"{re.escape(name)}\s*==\s*([\s\S]*?)(?=\n\w|\n====|\Z)"
        m = re.search(pat, self.tla_code)
        return m.group(1) if m else None

    def _join_conjuncts(self, text: str) -> list[str]:
        """
        Join multi-line TLA+ conjuncts into single logical strings.

        A new conjunct begins on a line that starts (after stripping) with '/\'.
        All subsequent lines that do NOT start with '/\' are continuations of
        the previous conjunct and are appended to it.
        """
        logical: list[str] = []
        current = ""
        for raw_line in text.split("\n"):
            line = re.sub(r"\\\*[^\n]*", "", raw_line).rstrip()  # drop \* comments
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("/\\"):
                if current:
                    logical.append(current)
                current = stripped
            elif current:
                current = current + " " + stripped
        if current:
            logical.append(current)
        return logical

    # ------------------------------------------------------------------
    # Step 4: parse CombinationalLogic
    # ------------------------------------------------------------------

    def parse_combinational(self) -> list[str]:
        block = self._extract_block("CombinationalLogic")
        if not block:
            return []

        assigns: list[str] = []
        for conj in self._join_conjuncts(block):
            m = re.match(r"/\\\s*(\w+)'\s*=\s*(.*)", conj)
            if not m:
                continue
            var, expr = m.group(1), m.group(2).strip()
            if not self._emit(var):
                continue
            self.comb_vars.add(var)
            assigns.append(f"    assign {var} = {self.translate_expr(expr)};")
        return assigns

    # ------------------------------------------------------------------
    # Step 5: parse UpdatePipeline
    # ------------------------------------------------------------------

    def _split_reset_else(self, block: str) -> tuple[str, str]:
        """
        Split the UpdatePipeline block into (reset_section, else_section).

        Finds the outer  IF {reset_port} = 1 THEN ... ELSE ...  by looking for a
        line whose sole content is 'ELSE' (the standalone ELSE line that follows
        the reset conjuncts at the same indentation level as the IF keyword).
        The reset signal name is configurable (FIX 1) so a design whose reset is
        named e.g. `rst` is still recognised; mismatching it would leave the
        reset IF untranslated and leak `IF`/`ELSE` keywords past the banlist.
        """
        lines = block.split("\n")
        in_then = False
        then_lines: list[str] = []
        else_lines: list[str] = []

        reset_if_re = re.compile(
            rf"/\\\s*IF\s+{re.escape(self.reset_port)}\s*"
        )

        for line in lines:
            clean = re.sub(r"\\\*[^\n]*", "", line).rstrip()
            stripped = clean.strip()
            if not stripped:
                continue

            if not in_then and reset_if_re.match(stripped):
                in_then = True
                continue

            if in_then:
                # A line that is ONLY 'ELSE' marks the boundary
                if re.match(r"^ELSE\s*$", stripped):
                    in_then = False
                    continue
                then_lines.append(clean)
            else:
                else_lines.append(clean)

        return "\n".join(then_lines), "\n".join(else_lines)

    def _parse_assignments(self, text: str) -> list[tuple[str, str]]:
        """Extract [(varname, tla_expr)] from a block of TLA+ conjuncts."""
        result: list[tuple[str, str]] = []
        for conj in self._join_conjuncts(text):
            m = re.match(r"/\\\s*(\w+)'\s*=\s*(.*)", conj)
            if not m:
                continue
            var, expr = m.group(1), m.group(2).strip()
            if self._emit(var) and var != "clk":
                result.append((var, expr))
        return result

    def parse_sequential(self) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """
        Returns (reset_assignments, normal_assignments).
        Each entry is (varname, tla_expr_string).
        """
        block = self._extract_block("UpdatePipeline")
        if not block:
            return [], []

        reset_text, else_text = self._split_reset_else(block)
        reset_assigns = self._parse_assignments(reset_text)
        normal_assigns = self._parse_assignments(else_text)

        for var, _ in reset_assigns + normal_assigns:
            self.seq_vars.add(var)

        return reset_assigns, normal_assigns

    # ------------------------------------------------------------------
    # Step 5b: defensive undeclared-identifier detection (BUG-18)
    # ------------------------------------------------------------------

    def _undeclared_inputs(
        self,
        comb_lines: list[str],
        reset_assigns: list[tuple[str, str]],
        normal_assigns: list[tuple[str, str]],
    ) -> list[str]:
        """Return identifiers referenced in emitted RHS but never declared.

        Scans the *translated* right-hand sides of every assign / clocked
        assignment. Any identifier that is not a declared signal, not clk/reset,
        and not a Verilog/TLA+ reserved word is an externally-driven input that
        nothing in this module declares — so we surface it as a scalar input
        port rather than emit an un-bindable wire (BUG-18). Returns the names
        sorted for deterministic output.

        This is a safety net for hand-written TLA+ fed directly to Compiler 2.
        The primary fix lives in the bridge (free-input injection into
        VARIABLES); this guarantees the property even when the bridge is bypassed.
        """
        # Every name this module declares: all VARIABLES entries (which become
        # ports, internal regs, or are dropped) plus the fixed clk/reset.
        declared: set = set(self.variables) | set(self._FIXED_INPUTS)

        # Collect translated RHS text from both blocks. comb_lines are already
        # translated ("    assign v = <vexpr>;"); the sequential assigns carry
        # raw TLA+ exprs, so translate them the same way compile() will.
        referenced: set = set()
        for line in comb_lines:
            m = re.match(r"\s*assign\s+\w+\s*=\s*(.*);", line)
            if m:
                referenced |= _scan_verilog_identifiers(_strip_comments(m.group(1)))
        for _, expr in reset_assigns + normal_assigns:
            rhs = _strip_comments(self.translate_expr(expr))
            referenced |= _scan_verilog_identifiers(rhs)

        undeclared = {
            ident
            for ident in referenced
            if ident not in declared
            and ident not in _RESERVED_VERILOG_IDENTIFIERS
            and not self._is_verify(ident)   # hw_* are intentionally dropped
        }
        return sorted(undeclared)

    # ------------------------------------------------------------------
    # Step 6: emit Verilog-2001
    # ------------------------------------------------------------------

    def compile(self, module_name: str = "rtl_core") -> str:
        self.extract_variables()
        comb_lines = self.parse_combinational()   # populates self.comb_vars
        reset_assigns, normal_assigns = self.parse_sequential()  # populates self.seq_vars

        # --- Multi-driver conflict detection (G05) ---
        # A variable driven by BOTH blocks would get a continuous `assign`
        # (from CombinationalLogic) and a procedural `<=` (from UpdatePipeline)
        # on the same net -- an illegal double-driver. _classify silently
        # resolves the overlap to "output_reg" (seq wins), but the emit path
        # still writes the assign, producing un-elaboratable Verilog. Refuse at
        # build time rather than emit a multi-driver.
        conflicts = sorted(self.comb_vars & self.seq_vars)
        if conflicts:
            raise MultiDriverError(
                "Multi-driver conflict -- variable(s) "
                f"{', '.join(conflicts)} are driven by BOTH CombinationalLogic "
                "(continuous assign) AND UpdatePipeline (clocked <=). "
                "A net cannot have two drivers; drive each signal from exactly "
                "one block."
            )

        # Classify every variable now that comb_vars and seq_vars are known
        classes = {v: self._classify(v) for v in self.variables}

        input_ports   = [v for v in self.variables if classes[v] == "input"]
        output_wires  = [v for v in self.variables if classes[v] == "output_wire"]
        output_regs   = [v for v in self.variables if classes[v] == "output_reg"]
        internal_regs = [v for v in self.variables if classes[v] == "internal_reg"]

        # --- Defensive undeclared-identifier pass (BUG-18) ---
        # Catch identifiers referenced in the emitted expressions that were
        # never declared in VARIABLES (e.g. hand-written TLA+ fed straight to
        # Compiler 2, bypassing the bridge's free-input injection). Declare each
        # as a scalar input so the module always elaborates rather than emitting
        # an un-bindable wire. Sorted for deterministic output.
        extra_inputs = self._undeclared_inputs(
            comb_lines, reset_assigns, normal_assigns,
        )
        input_ports = input_ports + extra_inputs

        out: list[str] = []

        # --- Timescale directive (D1) ---
        # Synthesis tools ignore `timescale; simulators need it. Without it
        # iverilog defaults to a 1 s time precision and rejects the cocotb
        # generator's 10 ns clock ("Bad period: Unable to accurately represent
        # 10(ns) with precision 1e0"), so no Compiler-2 module could be
        # simulated end-to-end. 1ns/1ps is the conventional RTL sim resolution.
        out.append("`timescale 1ns / 1ps")
        out.append("")

        # --- Module header with full inferred port list ---
        # FIX 1: the reset port is emitted under its configured name so it binds
        # to the cocotb generator's `dut.<reset_port>` (default "reset").
        port_decls: list[str] = (
            ["    input  clk", f"    input  {self.reset_port}"]
            + [f"    input  {self._range(v)}{v}" for v in input_ports]
            + [f"    output {self._range(v)}{v}" for v in output_wires]
            + [f"    output reg {self._range(v)}{v}" for v in output_regs]
        )
        out.append(f"module {module_name} (")
        for i, decl in enumerate(port_decls):
            out.append(decl + ("," if i < len(port_decls) - 1 else ""))
        out += [");", ""]

        # --- Internal register declarations ---
        if internal_regs:
            out.append("    // Internal registers")
            for v in internal_regs:
                out.append(f"    reg  {self._range(v)}{v};")
            out.append("")

        # --- Combinational block ---
        if comb_lines:
            out.append("    // Combinational logic")
            out += comb_lines
            out.append("")

        # --- Sequential block ---
        if reset_assigns or normal_assigns:
            out.append("    // Clocked pipeline evolution")
            out.append("    always @(posedge clk) begin")
            if reset_assigns:
                out.append(f"        if ({self.reset_port}) begin")
                for var, expr in reset_assigns:
                    out.append(f"            {var} <= {self.translate_expr(expr)};")
                out.append("        end else begin")
                for var, expr in normal_assigns:
                    out.append(f"            {var} <= {self.translate_expr(expr)};")
                out.append("        end")
            else:
                for var, expr in normal_assigns:
                    out.append(f"        {var} <= {self.translate_expr(expr)};")
            out.append("    end")
            out.append("")

        out.append("endmodule")
        return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python compiler2.py <spec.tla> [module_name]", file=sys.stderr)
        print("       python compiler2.py --sample", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "--sample":
        tla = SAMPLE_TLA
        module_name = "pipeline_processor"
    else:
        with open(sys.argv[1]) as f:
            tla = f.read()
        module_name = sys.argv[2] if len(sys.argv) > 2 else "rtl_core"

    compiler = RTLTLACompiler(tla)
    print(compiler.compile(module_name))


if __name__ == "__main__":
    main()
