"""
Regression for the catch-all-as-SOLE-driver change (2026-06-08).

Background — the bug this pins dead
-----------------------------------
Stage 3 used to run FIVE structured refinement passes (pipeline/refinement_templates/
pass1_fsm..pass5_mapping), each its own engine.run() with a restricted allowed-rule
set, THEN a final catch-all engine.run() with all rules. On the live 2-bit counter
(artifacts/3f7e08d09b4b) that was badly behaved: 16 of 26 pick_rule calls were junk,
and — critically — passes 3 (datapath) and 5 (mapping) BOTH committed an
IntroduceVariable named 'count_concrete'. The engine concatenates each same-run_id
pass's steps onto the on-disk chain (G13), but IntroduceVariable.apply's name-uniqueness
check only sees the LIVE in-memory spec, not the cross-pass committed prefix. So the
persisted refinement_chain.json was NON-REPLAYABLE: replaying it from scratch raised
"IntroduceVariable: variable 'count_concrete' already exists in spec." That violates the
engine's documented replay invariant and would crash the cocotb-failure backtrack path.

The fix (pipeline/nodes/stage3.py): gate the structured-pass loop off
(_RUN_STRUCTURED_PASSES = False) and make the catch-all the SOLE driver. With a single
engine.run(), committed_prefix is empty, so the on-disk chain is exactly one run's chain
and a duplicate IntroduceVariable name can never be committed (apply() raises -> the
engine excludes the choice and never appends it).

These tests pin:
  (a) with the structured passes gated off, the captured counter + a competent
      applicability-driven picker converges to `success` AND the persisted
      refinement_chain.json REPLAYS cleanly via engine._replay_chain (no exception),
      with a small chain and NO duplicate IntroduceVariable names — i.e. the
      non-replayable-chain defect is gone;
  (b) the catch-all alone reaches is_rtl_style for the counter spec.

Fixtures (CAPTURED_SPEC / CAPTURED_SUMMARY / _competent_picker) are reused from
tests/test_live_counter_repro.py so this stays coupled to the exact live-run shape.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

from pipeline.refinement.engine import (
    run as engine_run,
    is_rtl_style,
    _replay_chain,
)
from pipeline.refinement.bridge import formal_spec_to_engine_spec
from pipeline.schemas.tla_schema import FormalSpec

from tests.test_live_counter_repro import (
    CAPTURED_SPEC,
    CAPTURED_SUMMARY,
    _competent_picker,
)


def _introduce_variable_names(chain: list[dict]) -> list[str]:
    """Variable names introduced by every IntroduceVariable step in *chain*."""
    names: list[str] = []
    for step in chain:
        if step.get("rule_name") == "IntroduceVariable":
            params = step.get("params", {})
            # IntroduceVariable params carry the new variable's name; tolerate the
            # couple of key spellings the rule/bridge may use.
            name = params.get("name") or params.get("var_name") or params.get("variable")
            names.append(name)
    return names


def test_structured_passes_are_gated_off():
    """The schedule is defined (test_pass_templates pins its shape) but NOT run."""
    from pipeline.nodes import stage3

    assert stage3._RUN_STRUCTURED_PASSES is False, (
        "structured passes must be gated off — the catch-all is the sole driver"
    )
    # _PASS_CONFIGS must remain populated so test_pass_templates.py stays green.
    assert len(stage3._PASS_CONFIGS) == 5


def test_catchall_alone_reaches_rtl_style():
    """(b) The catch-all engine.run() with ALL rules drives the captured counter
    spec to is_rtl_style on its own — no structured passes needed."""
    spec = formal_spec_to_engine_spec(FormalSpec.model_validate(CAPTURED_SPEC))

    # The competent picker takes a keyword system_prompt in the stage3 wrapper, but
    # the engine calls pick_rule(applicable_descs, spec) positionally — adapt it.
    def pick(applicable_rules, s):
        return _competent_picker(applicable_rules, s)

    final = engine_run(
        formal_spec=spec,
        pick_rule=pick,
        run_id="test_catchall_alone_counter",
        max_steps=16,
    )
    assert is_rtl_style(final), "catch-all alone must reach RTL-style for the counter"


def test_full_stage3_catchall_only_chain_is_replayable(tmp_path, monkeypatch):
    """(a) End-to-end: with the structured passes gated off, the captured counter +
    the competent picker converges to `success`, and the persisted
    refinement_chain.json replays cleanly with no duplicate IntroduceVariable names.

    This is the exact defect the sole-driver change kills: the old multi-pass chain
    re-introduced 'count_concrete' across passes and raised on replay.
    """
    from pipeline.nodes import stage3
    from pipeline.agents import agent3

    run_id = "catchall_counter"
    art = tmp_path / "artifacts" / run_id
    art.mkdir(parents=True, exist_ok=True)
    (art / "01_summary.json").write_text(json.dumps(CAPTURED_SUMMARY))
    monkeypatch.chdir(tmp_path)

    spec = FormalSpec.model_validate(CAPTURED_SPEC)
    monkeypatch.setattr(agent3, "generate_formal_spec", lambda summary: spec)
    monkeypatch.setattr(agent3, "pick_rule", _competent_picker)
    # Critic gate accepts (single mockable boundary — see stage3 doc).
    monkeypatch.setattr(
        stage3, "_run_refinement_critic",
        lambda abstract, refined: {"verdict": "accept", "issues": [], "reasoning": ""},
    )

    state = {"run_id": run_id, "retry_counts": {}, "halt": False, "last_diagnosis": None}
    stage3.run_stage3(state)

    rtl = json.loads((art / "03_rtl_output.json").read_text())
    assert rtl["status"] == "success", (
        f"catch-all-only stage3 must converge to success, got {rtl.get('status')}: "
        f"{rtl.get('error', '')[:300]}"
    )

    # The persisted chain must REPLAY cleanly from the initial engine spec.
    chain = json.loads((art / "refinement_chain.json").read_text())
    assert chain, "a refinement chain must have been written"

    # Small chain — the catch-all does the real work in a handful of steps, NOT the
    # 20-step junk-laden multi-pass chain.
    assert len(chain) <= 6, f"catch-all chain should be small; got {len(chain)}: {chain}"

    # NO duplicate IntroduceVariable names (the cross-pass collision that broke replay).
    iv_names = _introduce_variable_names(chain)
    dupes = [name for name, n in Counter(iv_names).items() if n > 1]
    assert not dupes, f"duplicate IntroduceVariable names in chain: {dupes}"

    # The actual replay invariant: replaying from scratch must NOT raise (this is
    # the exact call that raised ValueError on the old non-replayable live chain).
    initial = formal_spec_to_engine_spec(spec)
    replayed = _replay_chain(initial, chain)  # must not raise
    assert is_rtl_style(replayed), (
        "the replayed chain must reach the same RTL-style endpoint as the live run"
    )
