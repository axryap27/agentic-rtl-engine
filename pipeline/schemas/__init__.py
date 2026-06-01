# pipeline/schemas — Pydantic v2 models for all pipeline artifacts.
#
# Use these for:
#   from pipeline.schemas import SpecSummary, FormalSpec
# or the per-module imports:
#   from pipeline.schemas.summary_schema import SpecSummary
#   from pipeline.schemas.tla_schema import FormalSpec
#
# CLAUDE.md refers to "pipeline/schemas.py" (a single file). The actual
# layout is a package: pipeline/schemas/. Do not collapse to a single file
# — the package is load-bearing for the two-key (proxy vs Anthropic) import
# pattern and the per-schema ownership split.

from pipeline.schemas.summary_schema import SpecSummary, Port, TestVector
from pipeline.schemas.tla_schema import FormalSpec, Variable, Transition

__all__ = [
    # JSON(S) — produced by Agent 1, consumed by Agent 2 and Agent 3
    "SpecSummary",
    "Port",
    "TestVector",
    # JSON(TLA) — produced by Agent 3, consumed by Compiler 1 and Refinement Engine
    "FormalSpec",
    "Variable",
    "Transition",
]
