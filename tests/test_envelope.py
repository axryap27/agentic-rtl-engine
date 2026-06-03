"""Tests for ArtifactEnvelope (BUG-13): status-typo guard at write time."""

import json
import os
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.schemas.envelope import (
    ArtifactEnvelope,
    validate_status,
    write_artifact,
    write_error,
)


def test_valid_statuses_pass():
    for s in ("success", "error", "partial"):
        ArtifactEnvelope.model_validate({"status": s})


def test_status_typo_raises():
    with pytest.raises(ValidationError):
        ArtifactEnvelope.model_validate({"status": "sucess"})


def test_extra_payload_allowed():
    # Real artifacts carry stage-specific payload alongside status.
    ArtifactEnvelope.model_validate(
        {"status": "success", "module_name": "counter", "verilog": "..."}
    )


def test_validate_status_returns_same_dict():
    d = {"status": "success", "x": 1}
    assert validate_status(d) is d


def test_write_artifact_validates_and_writes(tmp_path):
    p = tmp_path / "art.json"
    write_artifact(p, {"status": "partial", "k": "v"})
    loaded = json.loads(p.read_text())
    assert loaded["status"] == "partial"
    assert loaded["k"] == "v"


def test_write_artifact_rejects_typo_no_file(tmp_path):
    p = tmp_path / "bad.json"
    with pytest.raises(ValidationError):
        write_artifact(p, {"status": "succes"})
    assert not p.exists(), "invalid artifact must not be written"


def test_write_error_helper(tmp_path):
    p = tmp_path / "err.json"
    write_error(p, "boom")
    loaded = json.loads(p.read_text())
    assert loaded == {"status": "error", "error": "boom"}
