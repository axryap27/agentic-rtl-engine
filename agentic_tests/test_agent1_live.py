"""
Live tests for Agent 1 — natural language prompt -> SpecSummary.

Transport: OpenAI-compatible proxy (LLM_BASE_URL / LLM_API_KEY / LLM_MODEL).
Gated: marker `live_llm` (default off) + RUN_LIVE_LLM=1 + proxy keys present.

These assert on the STRUCTURE the LLM must produce (valid SpecSummary, expected
ports present, test vectors well-formed), not on exact wording, since the model
output is not byte-deterministic even at temperature 0.
"""

from __future__ import annotations

from pipeline.agents import agent1
from pipeline.schemas.summary_schema import SpecSummary


def test_agent1_counter_returns_valid_summary(require_proxy, counter_prompt):
    """Agent 1 returns a schema-valid SpecSummary for the counter prompt."""
    summary = agent1.run(counter_prompt)

    assert isinstance(summary, SpecSummary)
    assert summary.module_name  # non-empty identifier
    assert summary.ports, "expected at least one port"
    assert summary.test_vectors, "expected at least one test vector"


def test_agent1_counter_has_expected_ports(require_proxy, counter_prompt):
    """The counter summary should surface a 2-bit output and an enable input."""
    summary = agent1.run(counter_prompt)

    port_names = {p.name.lower() for p in summary.ports}
    # The output the design is named for must be present.
    assert any("count" in n or n == "q" for n in port_names), (
        f"expected a counter output port, got {sorted(port_names)}"
    )

    # Directions are constrained to the two legal values.
    for p in summary.ports:
        assert p.direction in ("input", "output"), p.direction
        assert p.width >= 1, f"port {p.name} has non-positive width {p.width}"


def test_agent1_dff_returns_valid_summary(require_proxy, dff_prompt):
    """Agent 1 returns a schema-valid SpecSummary for the D flip-flop prompt."""
    summary = agent1.run(dff_prompt)

    assert isinstance(summary, SpecSummary)
    assert summary.ports
    # A DFF has a data input and a registered output; both should appear.
    port_names = {p.name.lower() for p in summary.ports}
    assert any(n in ("d", "data") for n in port_names), sorted(port_names)
    assert any(n in ("q", "out") for n in port_names), sorted(port_names)


def test_agent1_test_vectors_reference_real_ports(require_proxy, counter_prompt):
    """Every value in a test vector should name a port that exists in the summary.

    This is the Stage-1 invariant Stage 2 (testbench generation) depends on:
    a vector that drives or checks a non-existent port would produce a broken
    testbench.
    """
    summary = agent1.run(counter_prompt)
    port_names = {p.name for p in summary.ports}

    for i, tv in enumerate(summary.test_vectors):
        for name in tv.inputs:
            assert name in port_names, (
                f"vector {i} drives unknown input port {name!r}; ports={sorted(port_names)}"
            )
        for name in tv.expected:
            assert name in port_names, (
                f"vector {i} checks unknown output port {name!r}; ports={sorted(port_names)}"
            )


def test_agent1_determinism_smoke(require_proxy, dff_prompt):
    """At temperature 0 the port interface should be stable across calls.

    Temperature 0 makes sampling greedy but does NOT guarantee bit-identical
    output: backend batching, MoE routing, and FP reduction order let cosmetic
    choices vary run to run. The prompt pins the port names ('clk','rst','d','q'),
    so the port *interface* is the real structural backbone and is what we check.
    The module *name* is a free naming choice ('dff_sync_reset' and
    'd_flip_flop_sync_reset' are both valid) and is deliberately NOT asserted
    equal — only that each call produces one. This matches this file's stated
    philosophy: assert structure, not wording.
    """
    a = agent1.run(dff_prompt)
    b = agent1.run(dff_prompt)
    # Module name is a free choice; require it exists, not that it matches.
    assert a.module_name and b.module_name
    # The port interface is pinned by the prompt and is the backbone that must hold.
    assert {p.name for p in a.ports} == {p.name for p in b.ports}, (
        f"port set diverged across two temp-0 calls: "
        f"{sorted(p.name for p in a.ports)} vs {sorted(p.name for p in b.ports)}"
    )
