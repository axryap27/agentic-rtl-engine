"""
Deterministic reference simulator for a refined engine spec.

WHY THIS EXISTS
---------------
The cocotb golden test vectors come from Agent 1 (Stage 1), which HAND-COMPUTES
the expected outputs from the natural-language prompt. A one-shot LLM's arithmetic
is fragile on deep sequential designs: the live FIFO run (`181016`) produced a
perfectly correct RTL that nonetheless FAILED cocotb because ONE of Agent 1's 19
golden vectors miscounted the occupancy at the drain-to-empty boundary — a "false
red" (correct RTL failed by a wrong test), the inverse of a false green.

This module removes that failure class. The `FormalSpec` Agent 3 produces is an
EXECUTABLE model: this simulator evaluates the refined engine spec cycle-by-cycle —
with the SAME semantics the generated Verilog + cocotb harness have (a reset pulse,
exactly one rising edge per vector, nonblocking read-before-write register updates,
continuous combinational outputs, and X for an unwritten memory cell) — to derive
the arithmetically-correct expected outputs from Agent 1's INPUT stimulus. A caller
can then run cocotb against spec-derived expecteds (no false reds) while separately
surfacing any disagreement with Agent 1's expecteds (so a genuine spec/intent bug is
never silently masked).

INDEPENDENCE (scoped)
---------------------
The independence is at the EXPRESSION-EVALUATION leaf only: the simulator's
recursive-descent interpreter (`_Interp`/`_eval`) is wholly separate from Compiler
2's `translate_expr` + iverilog, so when the sim and the generated RTL agree, that
cross-validates Compiler 2's TRANSLATION/EMISSION — the part where Verilog-specific
codegen bugs live. It is NOT independent of the upstream COMPOSITION: the simulator
reuses the reverse bridge's `_compose_clocked_actions` / `_action_update_exprs`, the
same functions that feed Compiler 2, so a guard-priority / branch / sequential-step
composition bug would corrupt the reference and the RTL identically (the cross-check
would report "agreed" on a wrong design). Composition correctness is instead pinned
by the 5 fixture-trace regression tests, not by per-run agreement. The simulator is
validated against every in-repo design class: run on each fixture's input stimulus
it must reproduce that fixture's hand-derived, real-cocotb-proven `cocotb_trace`
exactly (see tests/test_spec_sim.py).
"""

from __future__ import annotations

import re

from pipeline.refinement.bridge import (
    _compose_clocked_actions,
    _action_update_exprs,
    _lhs_base,
    _INDEXED_LHS_RE,
)

# ---------------------------------------------------------------------------
# Expression interpreter for the engine-spec expression language
# ---------------------------------------------------------------------------
# Grammar (TLA+-ish, the form the bridge produces after _translate_bool_words):
#   expr    := "IF" expr "THEN" expr "ELSE" expr | or_expr
#   or_expr := and_expr ("\/" and_expr)*
#   and_expr:= cmp ("/\" cmp)*
#   cmp     := add (("="|"/="|"<="|">="|"<"|">") add)?
#   add     := mul (("+"|"-") mul)*
#   mul     := unary (("*"|"/"|"%") unary)*
#   unary   := ("~"|"!"|"-") unary | primary
#   primary := number | TRUE | FALSE | ident ("[" expr "]")? | "(" expr ")"
#
# Values are Python ints, or None for X (undefined). Any operation with a None
# operand yields None (X-propagation), matching hardware. Defensive AND/OR/NOT/mod
# word forms are accepted in case a hand-built spec bypasses _translate_bool_words.

# Verilog evaluates integer expressions in an unsigned, context-determined width
# (an integer literal contributes a 32-bit context), so e.g. `count - 1` at count==0
# does NOT yield a signed -1 — it wraps to the all-ones value, which a relational
# operator / index / modulo then sees. We model this by masking every arithmetic
# RESULT to 32 bits unsigned (the default integer context), so an underflow wraps
# like the hardware instead of going negative. Register/wire COMMITs additionally
# re-mask to the signal's own declared width (see _mask). (A genuinely narrow, all-
# register subtraction with no literal would use a narrower context; that rare case
# is not modelled — documented limitation.)
_U32 = (1 << 32) - 1


def _coerce_input(value):
    """Normalise a test-vector input value to an int or None (X), matching the
    cocotb generator's value handling (G14).

    bool -> 0/1; int -> itself; an int-convertible string (decimal or `0x..` hex)
    -> its int; anything else (a 4-state literal like `'1z'`, the don't-care `'x'`,
    or an unparseable token) -> None (X). Used so the simulator does not crash on —
    or silently mis-simulate — the string/hex/bool inputs the generator accepts.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)  # handles "5", "0xff", "0b101"
        except ValueError:
            return None
    return None


_TOKEN_RE = re.compile(
    r"\s*(/\\|\\/|/=|<=|>=|[=<>]|[-+*/%]|~|!|\[|\]|\(|\)|\d+|[A-Za-z_]\w*|')"
)


def _tokenize(expr: str) -> list[str]:
    toks: list[str] = []
    pos = 0
    s = expr or ""
    while pos < len(s):
        m = _TOKEN_RE.match(s, pos)
        if not m:
            # Skip an unrecognised char rather than crash (keeps the sim robust).
            pos += 1
            continue
        tok = m.group(1)
        pos = m.end()
        if tok != "'":  # a stray primed-suffix marker is not a value token
            toks.append(tok)
    return toks


_RESET_WORDS = {"AND": "/\\", "OR": "\\/", "NOT": "~", "mod": "%"}


class _Interp:
    """Recursive-descent evaluator over a token list against a `state` dict."""

    def __init__(self, tokens: list[str], state: dict):
        self.toks = tokens
        self.i = 0
        self.state = state

    def _peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _take(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    # ---- grammar ----
    def expr(self):
        if self._peek() == "IF":
            self._take()
            cond = self.expr()
            if self._peek() == "THEN":
                self._take()
            then_v = self.expr()
            if self._peek() == "ELSE":
                self._take()
            else_v = self.expr()
            if cond is None:
                return None
            return then_v if cond != 0 else else_v
        return self._or()

    def _or(self):
        v = self._and()
        while self._peek() in ("\\/", "OR"):
            self._take()
            r = self._and()
            v = None if (v is None or r is None) else (1 if (v != 0 or r != 0) else 0)
        return v

    def _and(self):
        v = self._cmp()
        while self._peek() in ("/\\", "AND"):
            self._take()
            r = self._cmp()
            v = None if (v is None or r is None) else (1 if (v != 0 and r != 0) else 0)
        return v

    def _cmp(self):
        v = self._add()
        op = self._peek()
        if op in ("=", "/=", "<=", ">=", "<", ">"):
            self._take()
            r = self._add()
            if v is None or r is None:
                return None
            return 1 if {
                "=": v == r, "/=": v != r, "<=": v <= r,
                ">=": v >= r, "<": v < r, ">": v > r,
            }[op] else 0
        return v

    def _add(self):
        v = self._mul()
        while self._peek() in ("+", "-"):
            op = self._take()
            r = self._mul()
            if v is None or r is None:
                v = None
            else:
                # Mask to 32-bit unsigned so an underflow (e.g. count-1 at 0)
                # wraps like Verilog rather than going negative.
                v = ((v + r) if op == "+" else (v - r)) & _U32
        return v

    def _mul(self):
        v = self._unary()
        while self._peek() in ("*", "/", "%", "mod"):
            op = self._take()
            r = self._unary()
            if v is None or r is None:
                v = None
            elif op == "*":
                v = (v * r) & _U32
            elif r == 0:
                v = None  # div/mod by zero -> X
            elif op == "/":
                v = int(v / r) & _U32  # Verilog truncates toward zero
            else:
                v = (v % r) & _U32
        return v

    def _unary(self):
        t = self._peek()
        if t in ("~", "!", "NOT"):
            self._take()
            v = self._unary()
            return None if v is None else (1 if v == 0 else 0)
        if t == "-":
            self._take()
            v = self._unary()
            return None if v is None else ((-v) & _U32)
        return self._primary()

    def _primary(self):
        t = self._take()
        if t == "(":
            v = self.expr()
            if self._peek() == ")":
                self._take()
            return v
        if t is not None and t.isdigit():
            return int(t)
        if t in ("TRUE", "FALSE"):
            return 1 if t == "TRUE" else 0
        # identifier, optionally indexed (memory read)
        if self._peek() == "[":
            self._take()
            idx = self.expr()
            if self._peek() == "]":
                self._take()
            arr = self.state.get(t)
            if arr is None or idx is None or not (0 <= idx < len(arr)):
                return None
            return arr[idx]
        return self.state.get(t)


def _eval(expr: str, state: dict):
    """Evaluate one engine-spec expression against *state*; None means X."""
    if expr is None:
        return None
    return _Interp(_tokenize(str(expr)), state).expr()


# ---------------------------------------------------------------------------
# Cycle-accurate simulator
# ---------------------------------------------------------------------------

def _mask(value, width):
    """Truncate an int to *width* bits, replicating a Verilog reg's width."""
    if value is None or width is None or width <= 0:
        return value
    return value & ((1 << width) - 1)


class SpecSimulator:
    """Cycle-accurate reference model of a refined engine spec.

    Matches the generated Verilog + the cocotb generator's harness: registers
    update on a rising edge (reset branch when reset is asserted, else the composed
    clocked next-state, evaluated with nonblocking read-before-write semantics);
    combinational outputs are continuous (recomputed from settled state); a memory
    is an array (X until written); every register/memory element is width-masked.
    """

    def __init__(self, engine_spec: dict, reset_port: str = "reset",
                 reset_active_low: bool = False):
        self.reset_port = reset_port
        self.reset_active_low = reset_active_low

        variables = engine_spec.get("variables", [])
        # Drop reset/clk if Agent 3 modelled them as variables (the reverse bridge
        # does the same); they are ports, not simulated state.
        variables = [v for v in variables if v.get("name") not in (reset_port, "clk")]
        self.widths = {v["name"]: int(v.get("width") or 1) for v in variables}
        self.depths = {v["name"]: v.get("depth") for v in variables if v.get("depth")}
        self.reg_vars = [
            v["name"] for v in variables
            if not v.get("depth") and not v.get("combinational")
        ]
        self.mem_vars = [v["name"] for v in variables if v.get("depth")]
        self.comb_vars = [v["name"] for v in variables if v.get("combinational")]

        actions = engine_spec.get("actions", [])
        reset_name = engine_spec.get("reset_action")
        self.reset_action = next((a for a in actions if a["name"] == reset_name), None)
        clocked = [a for a in actions if a.get("clocked") and a["name"] != reset_name]
        comb = [a for a in actions
                if a.get("combinational") and a["name"] != reset_name]
        # Composed per-target clocked next-state (exactly what Compiler 2 emits).
        self.clocked_updates = _compose_clocked_actions(clocked)
        # Combinational definitions: var -> expr.
        self.comb_updates: list[tuple[str, str]] = []
        for a in comb:
            self.comb_updates.extend(_action_update_exprs(a))
        self.reset_updates = (
            _action_update_exprs(self.reset_action) if self.reset_action else []
        )

        # state: register/comb scalars (int|None) + memories (list[int|None]).
        self.state: dict = {}
        for name in self.reg_vars + self.comb_vars:
            self.state[name] = None
        for name in self.mem_vars:
            self.state[name] = [None] * int(self.depths[name])

    # ------------------------------------------------------------------
    def _recompute_comb(self):
        """Settle combinational outputs from the current registers/inputs.

        Iterate to a fixpoint so a flag that depends on another flag converges
        (bounded by the count of combinational signals; a cyclic combinational
        loop — a codegen bug — simply stops without converging)."""
        for _ in range(len(self.comb_updates) + 1):
            changed = False
            for var, expr in self.comb_updates:
                new = _mask(_eval(expr, self.state), self.widths.get(var))
                if new != self.state.get(var):
                    self.state[var] = new
                    changed = True
            if not changed:
                break

    def _set_inputs(self, inputs: dict):
        for k, v in inputs.items():
            if k in ("clk", self.reset_port):
                continue
            self.state[k] = _coerce_input(v)

    def _edge(self, inputs: dict, is_reset: bool):
        """Advance one rising clock edge."""
        self._set_inputs(inputs)
        self._recompute_comb()  # guards/combinational seen by the edge use OLD regs

        if is_reset:
            # Reset branch: only the reset action's targets are driven; memory and
            # any unlisted register hold. (Matches `if (reset) <reset_assigns>`.)
            new_scalars = {}
            for lhs, expr in self.reset_updates:
                base = _lhs_base(lhs)
                if base in self.reg_vars:
                    new_scalars[base] = _mask(_eval(expr, self.state), self.widths.get(base))
            self.state.update(new_scalars)
        else:
            # Normal branch: evaluate every composed next-state against the OLD
            # state (nonblocking / read-before-write), then commit simultaneously.
            new_scalars: dict = {}
            mem_writes: list[tuple[str, int, object]] = []
            for lhs, expr in self.clocked_updates:
                m = _INDEXED_LHS_RE.match(lhs.strip())
                if m:
                    base, idx_expr = m.group(1), m.group(2)
                    idx = _eval(idx_expr, self.state)
                    val = _mask(_eval(expr, self.state), self.widths.get(base))
                    if idx is not None:
                        mem_writes.append((base, idx, val))
                else:
                    new_scalars[lhs] = _mask(_eval(expr, self.state), self.widths.get(lhs))
            self.state.update(new_scalars)
            for base, idx, val in mem_writes:
                arr = self.state.get(base)
                if arr is not None and 0 <= idx < len(arr):
                    arr[idx] = val

        self._recompute_comb()  # settle combinational outputs for observation

    def _is_reset_asserted(self, inputs: dict) -> bool:
        # Coerce first: a string reset like "0" must compare numerically, not as a
        # truthy non-empty string (which would mis-detect the reset level).
        val = _coerce_input(inputs.get(self.reset_port))
        if val is None:
            return False
        return (val == 0) if self.reset_active_low else (val != 0)

    def run(self, stimulus: list[dict], output_ports: list[str]) -> list[dict]:
        """Simulate the generator's harness on *stimulus* and return per-vector
        expected outputs (only `output_ports`; an X output is omitted, like a
        cocotb don't-assert).

        The harness pulses reset (assert edge, deassert edge, inputs at 0) before
        vector 0, then drives exactly one edge per vector.
        """
        # The cocotb generator pre-initialises every (int) input to 0 BEFORE the
        # reset pulse, so the reset/deassert edges see inputs at 0 (e.g. an
        # enable-gated register holds, rather than latching X). Drive every input
        # name from the stimulus to 0 across the reset pulse.
        input_names = set()
        for s in stimulus:
            input_names |= set(s.keys())
        input_names.discard("clk")
        input_names.discard(self.reset_port)
        zero_inputs = {n: 0 for n in input_names}
        # Reset pulse: assert (registers <- reset values), then deassert (one
        # normal step with inputs 0). Memory stays X across both.
        self._edge({**zero_inputs, self.reset_port: (0 if self.reset_active_low else 1)}, is_reset=True)
        self._edge({**zero_inputs}, is_reset=False)

        out: list[dict] = []
        for inputs in stimulus:
            self._edge(inputs, is_reset=self._is_reset_asserted(inputs))
            row = {}
            for port in output_ports:
                val = self.state.get(port)
                if val is not None:
                    row[port] = val
            out.append(row)
        return out


def derive_expected(
    engine_spec: dict,
    stimulus: list[dict],
    output_ports: list[str],
    reset_port: str = "reset",
    reset_active_low: bool = False,
) -> list[dict]:
    """Spec-derived expected outputs for each input vector (see SpecSimulator)."""
    return SpecSimulator(engine_spec, reset_port, reset_active_low).run(
        stimulus, output_ports
    )
