import copy
from .base import RefinementRule


class IntroduceVariable(RefinementRule):
    """
    Table 2 — Introduce Variable.

    Formal: ss ⊑ Var x; w, x : [pre, dur, post]
    Hardware role: add a new register or wire to the spec — needed when
    intermediate storage (pipeline register, flag, counter) must be named
    before it can be assigned or iterated.

    Required params:
        name (str): name of the new variable.
        type (str): TLA+ type expression, e.g. "BOOLEAN", "0..255",
            "StateEnum".
        abstract (bool): True if still an abstract value; False if concrete
            storage (register/wire).
        reset_value (str | None): optional; if provided, set as the reset
            value immediately (combines well with Initialization).
    """

    def is_applicable(self, spec: dict) -> bool:
        # Always applicable provided params supply a name not already in spec.
        # The engine will validate the name uniqueness before calling apply().
        return True

    def apply(self, spec: dict, params: dict) -> dict:
        name: str = params["name"]
        var_type: str = params["type"]
        abstract: bool = params.get("abstract", True)
        reset_value = params.get("reset_value", None)
        # Default to single-bit; carried through to Compiler 2 so introduced
        # signals are sized correctly rather than truncating to 1 bit (BUG-17).
        width: int = params.get("width", 1)

        existing_names = {v["name"] for v in spec.get("variables", [])}
        if name in existing_names:
            raise ValueError(
                f"IntroduceVariable: variable '{name}' already exists in spec."
            )

        result = copy.deepcopy(spec)
        new_var = {
            "name": name,
            "type": var_type,
            "width": width,
            "abstract": abstract,
            "reset_value": reset_value,
            "clocked": False,
        }
        result.setdefault("variables", []).append(new_var)
        return result

    def describe(self) -> str:
        return (
            "IntroduceVariable: add a new variable (register or wire) to the "
            "spec so it can be assigned or iterated in subsequent refinement "
            "steps. "
            "Params: name (str), type (str), abstract (bool), "
            "reset_value (str | None)."
        )
