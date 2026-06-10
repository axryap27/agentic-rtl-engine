# JSON(TLA) schema: 
# 
# Description: the formal design plan produced by Agent 3 from JSON(S).
# Consumed by Compiler 1 to mechanically emit TLA+.
# Use FormalSpec.model_validate(data) to validate a loaded JSON(TLA) dict.
#
# Conditions and update expressions use plain English boolean operators (AND, OR, NOT)
# so Agent 3 can generate them reliably. Compiler 1 translates to TLA+ syntax (/\, \/, ~).

from typing import Optional

from pydantic import BaseModel


class Variable(BaseModel):
    type: str   # "Nat" or "Bit"
    width: int  # bit width; used by Compiler 1 to add range constraints e.g. a \in 0..255
    depth: Optional[int] = None  # if set, this variable is a MEMORY ARRAY of `depth`
    #                              words each `width` bits (a register file / RAM).
    #                              A memory is emitted as `reg [width-1:0] name [0:depth-1]`,
    #                              is never a port, and is not reset (synthesis-canonical:
    #                              memories carry no reset). Scalars leave this None.


class Transition(BaseModel):
    label: str                  # action name; matches TLA+ action label for TLC error tracing
    condition: str              # enabling condition using plain English operators (AND, OR, NOT)
    updates: dict[str, str]     # maps variable name to next-value expression; all variables must be listed
    combinational: bool = False # if True, this transition is CONTINUOUS (combinational) logic, not a
    #                             clocked register update: its target signals are wires driven by an
    #                             `assign` (e.g. a FIFO `full = count == DEPTH` flag), born concrete,
    #                             never clocked (Iteration), and never reset. Clocked transitions (the
    #                             default) become `always @(posedge clk)` register updates.
    spec_statement: bool = False  # if True, this transition is an ABSTRACT specification statement (a
    #                             Morgan spec statement, e.g. product' = a*b): it states a POSTcondition
    #                             over still-abstract target variable(s), NOT a concrete clocked update.
    #                             The bridge marks its target variable(s) abstract so LoopIntroduction
    #                             fires; that rule DERIVES a verified concrete loop (then the scheduler
    #                             turns it into a clocked FSMD). `updates` may carry a placeholder RHS
    #                             (the abstract relation) for documentation; refinement replaces it.
    postcondition: Optional[str] = None  # the abstract post the loop must establish (e.g. "product = a*b").
    #                             Used only when spec_statement=True; consumed by LoopIntroduction.


# Produced by Agent 3 from JSON(S).
# Consumed by Compiler 1 to emit TLA+.
# On TLC failure, Agent 3 revises specific transitions or invariants and re-runs Compiler 1.
class FormalSpec(BaseModel):
    module_name: str
    description: str
    variables: dict[str, Variable]      # variable name -> type info
    initial: dict[str, str]             # variable name -> initial value expression
    transitions: list[Transition]
    invariants: list[str]               # TLA+ invariant expressions; all must hold in every state
    raw_tla: Optional[str] = None       # if set, Compiler 1 passes this through verbatim instead of templating
