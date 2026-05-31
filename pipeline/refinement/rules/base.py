from abc import ABC, abstractmethod


class RefinementRule(ABC):
    """
    Base class for all refinement calculus rules.

    Spec dict structure (all rules read and write this format):

    {
        "variables": [
            {
                "name": str,         # e.g. "state", "counter"
                "type": str,         # e.g. "BOOLEAN", "0..7", "StateEnum"
                "abstract": bool,    # True = not yet concrete hardware storage
                "reset_value": str | None,  # set by Initialization
                "clocked": bool      # set by Iteration
            }
        ],
        "actions": [
            {
                "name": str,
                "guard": str,        # TLA+ guard expression
                "updates": [         # ordered list of explicit assignments
                    {"variable": str, "expression": str}
                ],
                "is_rtl_style": bool,
                # Optional fields added by specific rules:
                "branches": [        # added by Alternation
                    {"guard": str, "updates": [...]}
                ],
                "sequential_steps": [  # added by SequentialComposition
                    {"name": str, "guard": str, "updates": [...]}
                ],
                "clocked": bool      # set by Iteration
            }
        ],
        "init": str,                 # TLA+ Init predicate
        "invariants": [str],
        "abstraction_mapping": {str: str},
        "reset_action": str | None,  # name of the reset action
        "properties": [str]
    }

    apply() MUST be pure: same (spec, params) always produces the same output.
    Never mutate the input spec — always deepcopy first.
    """

    @abstractmethod
    def is_applicable(self, spec: dict) -> bool:
        """Return True if this rule can fire on the current spec."""

    @abstractmethod
    def apply(self, spec: dict, params: dict) -> dict:
        """Apply the rule deterministically. Returns the refined spec."""

    @abstractmethod
    def describe(self) -> str:
        """One-line human description shown to the Rule Picker LLM."""
