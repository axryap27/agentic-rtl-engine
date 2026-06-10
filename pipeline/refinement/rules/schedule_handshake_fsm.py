"""
ScheduleHandshakeFSM — mechanically schedule a verified bare loop into a hardened
IDLE/BUSY/DONE start/done handshake FSMD.

This is the SECOND half of the verified-derivation chain. LoopIntroduction takes an
abstract spec statement (e.g. ``product' = a * b``) and, AFTER discharging the
iteration-rule proof obligations, installs a verified bare loop on the target action
plus a ``loop`` marker recording the scheduling inputs (init / body / variant / guard).
This rule consumes that marker and turns the bare loop into a synthesizable multi-cycle
datapath behind a start/done handshake:

    state: IDLE(0) --start--> BUSY(1) ...iterate... --> DONE(2) --start--> BUSY(1) / IDLE(0)
    on load (start while NOT busy, i.e. in IDLE *or* DONE): each loop register := init
    each BUSY cycle: each loop register := body step
    done = COMBINATIONAL (state == DONE)

It is a DETERMINISTIC transform of (init, body, variant, guard) — no proof, no LLM.
The soundness already lives in LoopIntroduction's obligation kernel; this rule only
SCHEDULES the already-verified loop onto a control FSM. Ported faithfully from the
proven PoC (poc_schedule.py), which compiles + passes real cocotb end to end.

HARDENED HANDSHAKE (load-bearing!)
----------------------------------
The load fires on start while NOT BUSY — IDLE *or* DONE: ``(state=0 OR state=2) AND
start=1``. Accepting start in DONE is what makes back-to-back work: a start pulse that
coincides with a previous run's 1-cycle DONE reloads immediately instead of being
dropped (the exact live-run bug an IDLE-only load caused).

FLATTENING (load-bearing!)
--------------------------
A loop-register body may ITSELF be a conditional (``IF (mplier % 2)=1 THEN product +
mcand ELSE product``). A nested IF spliced verbatim into a THEN position leaks past
Compiler 2's depth-0 IF-splitter (which recurses only into ELSE, not THEN) — leaving
untranslated IF/THEN/ELSE keywords in the Verilog. So we FLATTEN: parse the body's own
IF-chain and splice each (guard, value) into the ELSE-IF chain, ANDing the BUSY
condition (``state = 1``) into each body-branch guard. The naive (un-flattened) version
failed; the flatten fixed it.

PURITY
------
apply() deepcopies first and never mutates its inputs. No randomness, no I/O, no LLM.
"""

import copy
import re

from .base import RefinementRule


# ---------------------------------------------------------------------------
# Proven scheduling primitives (ported verbatim from poc_schedule.py)
# ---------------------------------------------------------------------------

def _subst(expr: str, mapping: dict) -> str:
    """Word-boundary substitute each var->expr in *expr* (single pass).

    Each identifier token in *expr* that is a key of *mapping* is replaced by the
    mapped expression, parenthesised. Identifiers not in *mapping* are left as-is.
    Numeric literals and operators never match (the regex requires a leading
    letter/underscore). Pure.
    """
    def repl(m):
        return "(" + mapping[m.group(0)] + ")" if m.group(0) in mapping else m.group(0)
    pat = re.compile(r"[A-Za-z_]\w*")
    return pat.sub(repl, expr)


def _top_kw(expr: str, kw: str) -> int:
    """Index of the first depth-0 occurrence of `` kw `` (space-delimited), or -1.

    Tracks parenthesis depth so a keyword inside a nested ``(...)`` is skipped —
    only top-level IF/THEN/ELSE structure is matched by the chain parser. Pure.
    """
    depth = 0
    i, target = 0, f" {kw} "
    while i < len(expr):
        c = expr[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and expr[i:i + len(target)] == target:
            return i
        i += 1
    return -1


def _parse_if_chain(expr: str):
    """Parse a flat IF/THEN/ELSE-IF chain into ``([(guard, value), ...], default)``.

    A non-conditional expr yields ``([], expr)``. The chain is walked at depth 0
    (via _top_kw) so a conditional nested in a value is treated atomically. Pure.
    """
    e = expr.strip()
    pairs = []
    while e.startswith("IF "):
        t = _top_kw(e, "THEN")
        el = _top_kw(e, "ELSE")
        if t == -1 or el == -1 or el < t:
            break
        guard = e[3:t].strip()
        value = e[t + 6:el].strip()
        pairs.append((guard, value))
        e = e[el + 6:].strip()           # recurse into the ELSE remainder
    return pairs, e


# Params the proposer (pick_rule) may supply for this rule. Only action_name is
# required; the FSM control-signal names default to the canonical handshake names.
_REQUIRED_PARAMS = ("action_name",)


class ScheduleHandshakeFSM(RefinementRule):
    """
    Schedule a verified bare loop (carrying LoopIntroduction's ``loop`` marker) into
    a hardened IDLE/BUSY/DONE start/done handshake FSMD.

    Hardware role: turn the verified iterative datapath into a synthesizable
    multi-cycle controller — a control FSM sequencing the loop body behind a
    start/done handshake (the canonical "real hardware" FSMD pattern).

    Required params:
        action_name (str): the loop action to schedule (must carry ``loop``).
    Optional params (canonical handshake names by default):
        state_var (str, default "state"): the 2-bit control register.
        done_var  (str, default "done"):  the combinational completion flag.
        start     (str, default "start"): the free start input.
    """

    # Marker field on an action set by LoopIntroduction: a verified bare loop
    # awaiting scheduling. Its presence is the applicability trigger; this rule
    # CLEARS it once the loop has been scheduled onto a control FSM. The introduced
    # control register (params["state_var"], default "state") is what records that
    # an action has ALREADY been scheduled — so is_applicable also checks it is
    # absent to stay inert after the schedule.
    LOOP_MARKER = "loop"

    def is_applicable(self, spec: dict) -> bool:
        """True iff some non-reset action carries the ``loop`` marker (a verified
        bare loop installed by LoopIntroduction) AND has not yet been scheduled.

        "Not yet scheduled" means the control state register named by the default
        ``state`` is not already a declared variable: ScheduleHandshakeFSM introduces
        that register, so its presence signals the action was already scheduled. (A
        custom state_var would still re-fire by name, but the marker is cleared on
        schedule, so the primary guard is the marker itself.)
        """
        reset_name = spec.get("reset_action")
        var_names = {v.get("name") for v in spec.get("variables", [])}
        for action in spec.get("actions", []):
            if action.get("name") == reset_name:
                continue
            if not action.get(self.LOOP_MARKER):
                continue
            # The default control register present => this loop was already
            # scheduled (the marker should already be cleared, but be defensive).
            if "state" in var_names:
                continue
            return True
        return False

    def apply(self, spec: dict, params: dict) -> dict:
        # --- validate params (malformed -> ValueError, engine excludes) ---
        missing = [k for k in _REQUIRED_PARAMS if k not in params]
        if missing:
            raise ValueError(
                f"ScheduleHandshakeFSM: missing required params: {missing}"
            )
        action_name = params["action_name"]
        state_var = params.get("state_var", "state")
        done_var = params.get("done_var", "done")
        start = params.get("start", "start")
        for key, val in (("state_var", state_var), ("done_var", done_var),
                         ("start", start), ("action_name", action_name)):
            if not isinstance(val, str) or not val:
                raise ValueError(
                    f"ScheduleHandshakeFSM: {key} must be a non-empty string."
                )

        # --- locate the target loop action ---
        result = copy.deepcopy(spec)
        act = None
        for a in result.get("actions", []):
            if a.get("name") == action_name:
                act = a
                break
        # Missing/absent action OR no loop marker -> nothing to schedule; no-op
        # (an unchanged deepcopy is the engine's backtrack signal, same contract as
        # LoopIntroduction). PURE: never mutate inputs.
        if act is None or not act.get(self.LOOP_MARKER):
            return copy.deepcopy(spec)

        loop = act[self.LOOP_MARKER]
        init = loop["init"]
        # The body installed on the action is authoritative; the marker's body is
        # the same (recorded for the scheduler). Read it from the action's updates
        # so the schedule reflects exactly what LoopIntroduction installed.
        body = {u["variable"]: u["expression"] for u in act.get("updates", [])}
        guard = act.get("guard", loop.get("guard"))  # loop-continuation, e.g. count>0
        loop_vars = list(body)

        LOAD = f"(({state_var} = 0) OR ({state_var} = 2)) AND {start} = 1"
        # stay-BUSY iff the loop guard still holds AFTER this step (body subst'd in)
        stay_busy = _subst(guard, body)

        # state next-state (hardened handshake: accept start in IDLE or DONE)
        state_next = (
            f"IF {LOAD} THEN 1 "
            f"ELSE IF {state_var} = 1 AND ({stay_busy}) THEN 1 "
            f"ELSE IF {state_var} = 1 THEN 2 "
            f"ELSE IF {state_var} = 2 THEN 0 ELSE 0"
        )

        # each loop register: LOAD on start(not busy) / STEP in BUSY / HOLD otherwise.
        # The body may itself be conditional; FLATTEN it into the ELSE-IF chain by
        # ANDing the BUSY condition into each body-branch guard (a nested IF in a
        # THEN would leak past Compiler 2's splitter).
        updates = [{"variable": state_var, "expression": state_next}]
        for v in loop_vars:
            body_pairs, body_default = _parse_if_chain(body[v])
            segs = [(LOAD, init[v])]
            for g, val in body_pairs:
                segs.append((f"{state_var} = 1 AND ({g})", val))
            segs.append((f"{state_var} = 1", body_default))   # body's else, in BUSY
            chain = v                                          # HOLD (the final else)
            for g, val in reversed(segs):
                chain = f"IF {g} THEN {val} ELSE {chain}"
            updates.append({"variable": v, "expression": chain})

        act["updates"] = updates
        act["guard"] = "TRUE"
        act["clocked"] = True
        # The loop is now scheduled onto the control FSM; clear the marker so this
        # rule is inert henceforth (and LoopIntroduction stays inert too — the
        # spec_statement marker was already cleared by LoopIntroduction).
        act.pop(self.LOOP_MARKER, None)
        act.pop("postcondition", None)

        # introduce the control state register (concrete, clocked, reset to 0).
        # Reuse the IntroduceVariable variable shape. Skip if already present.
        variables = result.setdefault("variables", [])
        if not any(v.get("name") == state_var for v in variables):
            variables.append({
                "name": state_var, "type": "0..2", "width": 2,
                "abstract": False, "reset_value": "0", "clocked": True,
            })

        # combinational done = (state == DONE). A combinational action whose target
        # is a born-concrete wire (abstract=False, combinational=True, never reset,
        # never clocked) — symmetric to the FIFO full/empty flags. is_rtl_style and
        # the bridge both honour the combinational carve-out.
        result.setdefault("actions", []).append({
            "name": "DoneFlag", "guard": "TRUE", "combinational": True,
            "updates": [{"variable": done_var, "expression": f"{state_var} = 2"}],
        })
        if not any(v.get("name") == done_var for v in variables):
            variables.append({
                "name": done_var, "type": "0..1", "width": 1,
                "abstract": False, "combinational": True,
                "reset_value": None, "clocked": False,
            })

        return result

    def describe(self) -> str:
        return (
            "ScheduleHandshakeFSM: schedule a verified bare loop (carrying "
            "LoopIntroduction's `loop` marker) into a hardened IDLE/BUSY/DONE "
            "start/done handshake FSMD. Reads the recorded loop (init/body/variant/"
            "guard) and emits the load-on-start / step-in-BUSY / hold register "
            "chains plus a combinational done flag, introducing a 2-bit control "
            "state register. The body's own conditionals are FLATTENED into the "
            "ELSE-IF chain so nothing leaks past Compiler 2's splitter. Deterministic "
            "(no proof, no LLM); the soundness already lives in LoopIntroduction. "
            "Params: action_name (str); optional state_var/done_var/start (str)."
        )
