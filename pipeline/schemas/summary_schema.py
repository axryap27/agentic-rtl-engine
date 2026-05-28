# JSON(Summary) schema:
# 
# Description: the design spec interpretation of the user's natural language prompt.
# Produced by Agent 1. Use SpecSummary.model_validate(data) to validate a loaded JSON(S) dict.

from pydantic import BaseModel
from typing import Any


class Port(BaseModel):
    name: str
    direction: str  # "input" or "output"
    width: int      # bit width, e.g. 1 for a single-bit signal


class TestVector(BaseModel):
    inputs: dict[str, Any]   # port name → value
    expected: dict[str, Any] # port name → expected output value


# Produced by Agent 1 from the natural language prompt.
# Agent 2 reads ports + test_vectors to build the cocotb testbench.
# Agent 3 reads ports + description to build the formal spec (tla_schema).
class SpecSummary(BaseModel):
    module_name: str
    description: str          # plain-English behavior, used by Agent 3
    ports: list[Port]
    test_vectors: list[TestVector]
