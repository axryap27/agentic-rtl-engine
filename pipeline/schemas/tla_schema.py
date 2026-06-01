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


class Transition(BaseModel):
    label: str                  # action name; matches TLA+ action label for TLC error tracing
    condition: str              # enabling condition using plain English operators (AND, OR, NOT)
    updates: dict[str, str]     # maps variable name to next-value expression; all variables must be listed


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
