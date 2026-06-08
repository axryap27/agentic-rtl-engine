"""
Shared gating + fixtures for the LIVE-LLM test suite.

These tests make real API calls and cost money. They are triple-gated:

  1. marker      — every test here is marked `live_llm`; pyproject `addopts`
                   deselects that marker by default.
  2. env opt-in  — even when selected, a test is skipped unless RUN_LIVE_LLM=1.
  3. keys        — even when opted in, a test is skipped unless the specific
                   credentials it needs are present and non-placeholder.

Net effect: a normal `pytest` run collects nothing here. Running the live suite
must be deliberate:

    RUN_LIVE_LLM=1 pytest agentic_tests -m live_llm

Scope note (2026-06-03): this suite currently covers only the individual LLM
calls (Agent 1, Agent 3's four entry points, the diagnoser) and the LLM-driven
stage nodes (Stage 1, Stage 3). Full-pipeline runs and live refinement-engine
tests are intentionally deferred until the Agent 3 key is wired up — see
agentic_tests/README.md.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def pytest_collection_modifyitems(config, items):
    """Stamp the `live_llm` marker onto every test collected under this dir.

    A module-level `pytestmark` in a conftest.py does NOT propagate to sibling
    test modules, so we attach the marker here at collection time. This is what
    makes gate #1 (marker deselection via `addopts = -m 'not live_llm'`) actually
    apply to these tests — without it they'd run on a plain `pytest` invocation.
    """
    this_dir = Path(__file__).resolve().parent
    for item in items:
        try:
            item_path = Path(str(item.fspath)).resolve()
        except Exception:
            continue
        if this_dir in item_path.parents or item_path == this_dir:
            item.add_marker(pytest.mark.live_llm)


# ---------------------------------------------------------------------------
# .env loading — load it if python-dotenv is available, else rely on the
# ambient environment. We never hard-require dotenv so the suite still gates
# cleanly when it isn't installed.
# ---------------------------------------------------------------------------

def _load_dotenv_if_present() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(env_path)
        return
    except Exception:
        pass
    # Minimal fallback parser (no dependency): only sets keys that aren't
    # already in the environment, and never overrides an explicit export.
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv_if_present()


# ---------------------------------------------------------------------------
# Gate predicates
# ---------------------------------------------------------------------------

_PLACEHOLDER_MARKERS = ("NOT_CONFIGURED", "replace-with", "__AGENT3_")


def _is_real(value: str | None) -> bool:
    """True only if value is a present, non-placeholder credential."""
    if not value:
        return False
    return not any(m in value for m in _PLACEHOLDER_MARKERS)


def _opted_in() -> bool:
    return os.environ.get("RUN_LIVE_LLM") == "1"


def _proxy_keys_present() -> bool:
    """Agent 1 + diagnoser transport: the OpenAI-compatible proxy."""
    return (
        _is_real(os.environ.get("LLM_BASE_URL"))
        and _is_real(os.environ.get("LLM_API_KEY"))
        and _is_real(os.environ.get("LLM_MODEL"))
    )


def _anthropic_key_present() -> bool:
    """Agent 3 transport: a direct Anthropic key (its own account)."""
    return _is_real(os.environ.get("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------------------
# Gate fixtures — depend on these to enforce gates 2 (opt-in) and 3 (keys).
# Tests should request `require_proxy` or `require_anthropic` (or both).
# ---------------------------------------------------------------------------

@pytest.fixture
def require_opt_in() -> None:
    if not _opted_in():
        pytest.skip("live LLM tests are opt-in: set RUN_LIVE_LLM=1 to run")


@pytest.fixture
def require_proxy(require_opt_in: None) -> None:
    if not _proxy_keys_present():
        pytest.skip("proxy creds (LLM_BASE_URL/LLM_API_KEY/LLM_MODEL) not configured")


@pytest.fixture
def require_anthropic(require_opt_in: None) -> None:
    if not _anthropic_key_present():
        pytest.skip("ANTHROPIC_API_KEY (Agent 3) not configured")


# ---------------------------------------------------------------------------
# Shared input fixtures — small, well-understood designs whose correct shape
# we can assert on without re-deriving the LLM's exact wording.
# ---------------------------------------------------------------------------

@pytest.fixture
def counter_prompt() -> str:
    return (
        "A 2-bit counter that increments by one every clock cycle when an "
        "enable input 'en' is high, and resets to zero on a synchronous reset. "
        "It has a clock 'clk', a reset 'rst', an enable 'en', and a 2-bit "
        "output 'count'."
    )


@pytest.fixture
def dff_prompt() -> str:
    return (
        "A positive-edge-triggered D flip-flop with a synchronous reset. "
        "Inputs: clock 'clk', reset 'rst', data 'd'. Output: 'q'. On each rising "
        "clock edge q takes the value of d, unless rst is high, in which case q "
        "becomes 0."
    )


@pytest.fixture
def counter_summary():
    """A hand-built SpecSummary for the 2-bit counter.

    Lets the Agent 3 / FormalSpec tests run without first paying for an Agent 1
    call — they only need a valid SpecSummary as input, not a live Stage 1.
    """
    from pipeline.schemas.summary_schema import SpecSummary

    return SpecSummary.model_validate(
        {
            "module_name": "counter",
            "description": "2-bit up counter with enable and synchronous reset",
            "ports": [
                {"name": "clk", "direction": "input", "width": 1},
                {"name": "rst", "direction": "input", "width": 1},
                {"name": "en", "direction": "input", "width": 1},
                {"name": "count", "direction": "output", "width": 2},
            ],
            "test_vectors": [
                {"inputs": {"en": 1}, "expected": {"count": 1}},
                {"inputs": {"en": 1}, "expected": {"count": 2}},
            ],
            "reset_port": "rst",
            "reset_active_low": False,
        }
    )
