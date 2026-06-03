"""
ArtifactEnvelope — the routing contract every stage artifact must satisfy.

LangGraph routes the entire pipeline on the ``status`` field of each artifact
JSON. Before BUG-13 there was no schema for that outer wrapper: each stage
patched ``artifact["status"] = "..."`` by hand, so a typo like ``"sucess"``
silently routed to the ``error`` branch with no error surfaced anywhere.

``ArtifactEnvelope`` pins ``status`` to a closed ``Literal`` set so an invalid
value raises ``pydantic.ValidationError`` at *write* time (the moment a node
emits its artifact) instead of misrouting at read time. Stage nodes write
their artifacts through :func:`write_artifact`, which validates the status
field through this model before the JSON ever lands on disk.

Note: the envelope validates only the routing-critical fields (``status`` and
``error``); it does not constrain the stage-specific payload, which keeps it a
thin guard layered over the existing per-stage dicts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel


# The closed set LangGraph's routers understand. Any other string is a typo.
ArtifactStatus = Literal["success", "error", "partial"]


class ArtifactEnvelope(BaseModel):
    """Routing wrapper shared by every stage artifact.

    ``model_config`` allows extra keys so a full artifact dict (payload plus
    status) validates without enumerating every stage's payload fields here.
    """

    model_config = {"extra": "allow"}

    status: ArtifactStatus
    error: Optional[str] = None


def validate_status(data: dict[str, Any]) -> dict[str, Any]:
    """Validate the routing fields of an artifact dict in place.

    Returns the same dict if ``status`` is one of the allowed literals;
    raises ``pydantic.ValidationError`` otherwise. Use this when a node has
    already assembled its artifact dict and just needs the status checked
    before serialization.
    """
    ArtifactEnvelope.model_validate(data)
    return data


def write_artifact(path: Path, data: dict[str, Any], *, indent: int = 2) -> None:
    """Validate the status envelope, then write the artifact JSON to disk.

    Every stage node must write a status-bearing artifact before returning so
    the router can act on it (CLAUDE.md artifact-write contract). Routing every
    write through this helper guarantees the status field is one of the three
    legal values; a typo fails here rather than silently misrouting later.
    """
    validate_status(data)
    path.write_text(json.dumps(data, indent=indent))


def write_error(path: Path, message: str, *, indent: int = 2) -> None:
    """Write a validated ``error`` artifact. Convenience for failure paths."""
    write_artifact(path, {"status": "error", "error": message}, indent=indent)
