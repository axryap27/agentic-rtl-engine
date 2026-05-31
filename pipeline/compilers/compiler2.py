"""
RTL-style TLA+ to Verilog-2001 compiler.

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
Synthesizable Verilog-2001 (no SystemVerilog keywords).
  - Full port list inferred from variable declarations and which blocks drive them
  - reg  for internal registers (r_* prefix) and output reg ports
  - wire for output wire ports and undeclared combinational signals
  - always @(posedge clk) for clocked blocks
"""

import re
import sys
from typing import Optional


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
    /\ in_ready'  = (r_stg1_valid = 0 \/ r_stg2_ready)
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

    # Always emitted as module input ports
    _FIXED_INPUTS = frozenset(["clk", "reset"])

    # Variables whose names start with these prefixes are internal registers,
    # never exposed as ports regardless of which blocks drive them.
    _INTERNAL_PREFIXES = ("r_",)

    # Verification-only variable prefix — dropped entirely from Verilog output
    _VERIFY_VAR = re.compile(r"^hw_")

    def __init__(self, tla_code: str):
        self.tla_code = tla_code
        self.variables: list[str] = []
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
        m = re.search(
            r"VARIABLES\s+([\s\S]*?)(?=\n\s*\w+\s*(?:==|\n)|\Z)",
            self.tla_code,
        )
        if not m:
            return []
        raw = m.group(1)
        raw = re.sub(r"\\\*[^\n]*", "", raw)          # strip \* comments
        raw = re.sub(r"\(\*[\s\S]*?\*\)", "", raw)    # strip (* *) comments
        self.variables = [t for t in re.split(r"[\s,]+", raw) if re.match(r"^\w+$", t or "")]
        return self.variables

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
        # Logical operators
        expr = re.sub(r"/\\", "&&", expr)
        expr = re.sub(r"\\/", "||", expr)
        # Not-equal before equal
        expr = expr.replace("/=", "!=")
        # = → == (skip already-translated == != <= >=)
        expr = re.sub(r"(?<![!<>=])=(?!=)", "==", expr)
        return expr

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

        Finds the outer  IF reset = 1 THEN ... ELSE ...  by looking for a line
        whose sole content is 'ELSE' (the standalone ELSE line that follows the
        reset conjuncts at the same indentation level as the IF keyword).
        """
        lines = block.split("\n")
        in_then = False
        then_lines: list[str] = []
        else_lines: list[str] = []

        for line in lines:
            clean = re.sub(r"\\\*[^\n]*", "", line).rstrip()
            stripped = clean.strip()
            if not stripped:
                continue

            if not in_then and re.match(r"/\\\s*IF\s+reset\s*", stripped):
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
    # Step 6: emit Verilog-2001
    # ------------------------------------------------------------------

    def compile(self, module_name: str = "rtl_core") -> str:
        self.extract_variables()
        comb_lines = self.parse_combinational()   # populates self.comb_vars
        reset_assigns, normal_assigns = self.parse_sequential()  # populates self.seq_vars

        # Classify every variable now that comb_vars and seq_vars are known
        classes = {v: self._classify(v) for v in self.variables}

        input_ports   = [v for v in self.variables if classes[v] == "input"]
        output_wires  = [v for v in self.variables if classes[v] == "output_wire"]
        output_regs   = [v for v in self.variables if classes[v] == "output_reg"]
        internal_regs = [v for v in self.variables if classes[v] == "internal_reg"]

        out: list[str] = []

        # --- Module header with full inferred port list ---
        port_decls: list[str] = (
            ["    input  clk", "    input  reset"]
            + [f"    input  {v}" for v in input_ports]
            + [f"    output {v}" for v in output_wires]
            + [f"    output reg {v}" for v in output_regs]
        )
        out.append(f"module {module_name} (")
        for i, decl in enumerate(port_decls):
            out.append(decl + ("," if i < len(port_decls) - 1 else ""))
        out += [");", ""]

        # --- Internal register declarations ---
        if internal_regs:
            out.append("    // Internal registers")
            for v in internal_regs:
                out.append(f"    reg  {v};")
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
                out.append("        if (reset) begin")
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
        print("Usage: python compiler1.py <spec.tla> [module_name]", file=sys.stderr)
        print("       python compiler1.py --sample", file=sys.stderr)
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
