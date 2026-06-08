"""
Live refinement-engine convergence test — the central thesis.

This is the deferred "live refinement-engine convergence" item from
agentic_tests/README.md. The deterministic suite already proves the engine
converges with a *scripted* picker; this proves the LIVE Agent-3 `pick_rule`
*chooses* a converging path through the bounded action space.

COST + GATING: this makes REAL Anthropic API calls and costs money. It is
triple-gated like the rest of agentic_tests/ — auto-stamped `live_llm` marker
(conftest) + RUN_LIVE_LLM=1 + a real ANTHROPIC_API_KEY (require_anthropic). A
plain `pytest` run collects but never executes it (deselected by the marker).

Kept SMALL on purpose: a single 2-bit counter, ONE engine.run (so a bounded
number of pick_rule round-trips), with convergence AND chain-replay soundness
both asserted in that one test to avoid paying for extra LLM runs.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline.agents import agent3
from pipeline.refinement import bridge, engine
from pipeline.schemas.tla_schema import FormalSpec


def _counter_formal_spec() -> FormalSpec:
    """A clean, already-update-carrying 2-bit counter FormalSpec.

    This mirrors realistic Agent-3 output: the Tick transition already names its
    next-state update (`count' = (count + 1) % 4`). What is still MISSING — and
    what refinement must add — is the synchronous reset (Initialization) and the
    clock domain (Iteration). That gap is exactly what is_rtl_style checks for.
    """
    return FormalSpec.model_validate(
        {
            "module_name": "counter",
            "description": "2-bit up counter incremented every clock cycle",
            "variables": {"count": {"type": "Nat", "width": 2}},
            "initial": {"count": "0"},
            "transitions": [
                {
                    "label": "Tick",
                    "condition": "TRUE",
                    "updates": {"count": "(count + 1) % 4"},
                }
            ],
            "invariants": ["count \\in 0..3"],
        }
    )


def test_live_pick_rule_drives_engine_to_rtl_style(require_anthropic, tmp_path, monkeypatch):
    """The LIVE Agent-3 pick_rule drives the deterministic engine to RTL-style,
    and the on-disk chain replays exactly back to the converged spec.

    Two claims, one engine.run (one batch of live pick_rule calls):
      1. Convergence — engine.is_rtl_style(final) is True.
      2. Proof-trail soundness — replaying refinement_chain.json from a fresh
         initial spec reconstructs `final` byte-for-byte (apply() purity), and
         the replayed spec is itself RTL-style.
    """
    spec = _counter_formal_spec()
    initial_engine_spec = bridge.formal_spec_to_engine_spec(spec)

    # engine.run deepcopies its input internally, but keep our own pristine
    # baseline for the independent replay below so this test never depends on
    # the engine leaving its argument untouched.
    replay_baseline = copy.deepcopy(initial_engine_spec)

    # The engine writes artifacts/<run_id>/refinement_chain.json relative to CWD.
    monkeypatch.chdir(tmp_path)

    # Bounded action space for this pass. We restrict to the MINIMAL converging
    # set {Initialization, Iteration} rather than "everything except
    # IntroduceVariable", for two reasons:
    #   - IntroduceVariable, on an already-complete counter, would add a fresh
    #     abstract variable that is_rtl_style then requires concretized, which
    #     can block convergence (the hazard the design note called out).
    #   - Alternation / SequentialComposition are ALWAYS applicable to a plain
    #     action (no branches/steps yet) and demand complex structured params; a
    #     live model that picks one of those instead of Iteration adds structure
    #     without advancing toward RTL-style, so a naive run can wander and only
    #     reach RTL-style via backtracking — flaky and more expensive in tokens.
    # {Initialization, Iteration} is still a genuine MULTI-rule bounded menu (the
    # model must choose which to apply first and supply correct params), faithful
    # to the bounded-action-space thesis — each real Stage-3 pass offers exactly a
    # filtered applicable set like this — while guaranteeing a reachable RTL-style
    # path: Initialization adds the reset action + reset_value, Iteration marks
    # Tick clocked and its variable concrete. (Verified against the Tier-1 rules.)
    allowed = {"Initialization", "Iteration"}

    # Pass agent3.pick_rule directly: its signature is pick_rule(applicable, spec,
    # *, system_prompt=None), so the engine's positional call pick_rule(descs,
    # spec) works and system_prompt defaults to the shared persona. No tlc_check
    # (this test stops at the engine — no TLC, no Verilog, no cocotb needed).
    try:
        final = engine.run(
            initial_engine_spec,
            agent3.pick_rule,
            run_id="conv_counter",
            allowed_rule_names=allowed,
            max_steps=20,
        )
    except engine.RefinementStall as exc:
        pytest.fail(
            "live pick_rule failed to converge the 2-bit counter to RTL-style "
            f"within the {sorted(allowed)} bounded menu: {exc}"
        )

    # --- Claim 1: convergence ---
    assert engine.is_rtl_style(final) is True, (
        "live refinement did not reach RTL-style; final spec="
        f"{json.dumps(final, indent=2, default=str)}"
    )

    # The chain that produced it must be non-empty (something was actually done).
    chain_path = Path("artifacts") / "conv_counter" / "refinement_chain.json"
    assert chain_path.exists(), "engine must persist refinement_chain.json"
    chain = json.loads(chain_path.read_text())
    assert isinstance(chain, list) and chain, "recorded refinement chain is empty"
    # Every recorded step must name a rule from the offered bounded menu — no
    # rule appeared in the chain that was outside the allowed action space.
    for step in chain:
        assert step["rule_name"] in allowed, (
            f"chain step used out-of-menu rule {step['rule_name']!r}; "
            f"allowed={sorted(allowed)}"
        )

    # --- Claim 2: chain-replay soundness (no extra LLM cost — pure replay) ---
    # Replaying the persisted (rule_name, params) steps from a fresh initial spec
    # must reconstruct `final` exactly. This is the on-disk proof trail backing
    # the engine's backtracking guarantee.
    replayed = engine._replay_chain(replay_baseline, chain)
    assert replayed == final, (
        "replaying refinement_chain.json did not reproduce the converged spec — "
        "the on-disk proof trail is not faithful (apply() purity / chain bug)"
    )
    assert engine.is_rtl_style(replayed) is True, (
        "replayed spec is not RTL-style even though `final` was"
    )
