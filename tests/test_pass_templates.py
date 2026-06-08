"""
Pass-template <-> rule-registry consistency tests (G09 / G10 regressions).

These tests are deterministic and OFFLINE — pure import + string introspection,
no LLM calls. They pin two audit findings (see docs/refinement.md and git history):

  G09 — Pass templates instructed the LLM to emit UNREGISTERED rule names.
        pass2_handshake previously said `StrengthenDuring | PipingComposition`;
        pass3_datapath said `DataRefinement` — none of which exist in the rule
        registry, so those passes stalled on every pick (_validate_pick rejected).
        The fix: every rule name in a pass template's "ALLOWED RULES" section AND
        its `rule_used` JSON enum must be a registered rule AND a member of that
        pass's `allowed` set in stage3._PASS_CONFIGS.

  G10 — pass5_mapping & pass6_checker were described as dead code. The resolution
        wired into the current source is:
          * pass5_mapping IS an engine pass (5th entry in _PASS_CONFIGS).
          * pass6_checker is NOT an engine pass — it is a direct, one-shot
            Agent-3 refinement-correctness critic GATE. It is wired as
            pipeline.agents.agent3.critique_refinement, invoked through
            pipeline.nodes.stage3._run_refinement_critic before Compiler 2.
        So _PASS_CONFIGS has EXACTLY 5 entries and NO `pass6*` entry, while the
        critic gate exists as a direct call.

Per Wave-2 rules: these tests do not modify production code. Any genuine
inconsistency is marked xfail with a reason rather than patched.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.nodes import stage3
from pipeline.refinement.engine import RULE_REGISTRY


# The six Tier-1 rule class names. This is the authoritative anti-hallucination
# set: a pass template may only ever name these.
TIER1_RULE_NAMES = {
    "Assignment",
    "Alternation",
    "Iteration",
    "SequentialComposition",
    "IntroduceVariable",
    "Initialization",
}

EXPECTED_PASS_NAMES = [
    "pass1_fsm",
    "pass2_handshake",
    "pass3_datapath",
    "pass4_reset",
    "pass5_mapping",
]


# ---------------------------------------------------------------------------
# Helpers — pure string parsing of a pass template's SYSTEM prompt
# ---------------------------------------------------------------------------

def _registry_names() -> set[str]:
    """The set of registered rule class names from the live rule registry."""
    return {r.__class__.__name__ for r in RULE_REGISTRY}


def _parse_allowed_rules_section(system_prompt: str) -> list[str]:
    """
    Extract the rule names listed under the 'ALLOWED RULES' bullet section of a
    SYSTEM prompt. Each bullet looks like '- SequentialComposition' (optionally
    with a trailing parenthetical, e.g. '- IntroduceVariable (only to ...)').
    """
    m = re.search(r"ALLOWED RULES\n(.*?)\n\n", system_prompt, re.DOTALL)
    if not m:
        return []
    block = m.group(1)
    return re.findall(r"^-\s*([A-Za-z]+)", block, re.MULTILINE)


def _parse_rule_used_enum(system_prompt: str) -> list[str]:
    """
    Extract the rule names from the `rule_used` field of the output-schema JSON
    embedded in a SYSTEM prompt. Handles both forms:
        "rule_used": "<SequentialComposition | Iteration>"   (enum)
        "rule_used": "Initialization"                        (single literal)
    Returns [] if the field is absent.
    """
    m = re.search(r'"rule_used":\s*"([^"]*)"', system_prompt)
    if not m:
        return []
    raw = m.group(1).strip().strip("<>").strip()
    # Split an enum like "A | B" into its members; a single literal yields one.
    return [tok.strip() for tok in raw.split("|") if tok.strip()]


def _all_rule_tokens(system_prompt: str) -> set[str]:
    """
    Every token in the SYSTEM prompt that EQUALS one of the six Tier-1 rule
    names, found as a standalone word anywhere in the text. This is the broad
    catch-all that would have flagged a leaked `StrengthenDuring` / `DataRefinement`
    only if it happened to match a known name — so we ALSO scan for any token that
    *looks* like a rule name but is not registered (see the dedicated test below).
    """
    found = set()
    for name in TIER1_RULE_NAMES:
        if re.search(rf"\b{re.escape(name)}\b", system_prompt):
            found.add(name)
    return found


# A conservative pattern for "things that look like a refinement-rule name":
# CamelCase identifiers ending in a refinement-ish verb/noun. Used to catch
# unregistered names that the G09 fix removed (StrengthenDuring, PipingComposition,
# DataRefinement, StrengthenPostcondition, etc.) if they ever creep back in.
_KNOWN_BAD_NAMES = {
    "StrengthenDuring",
    "PipingComposition",
    "DataRefinement",
    "StrengthenPostcondition",
    "WeakenPrecondition",
    "ParallelComposition",
    "ExpandFrame",
    "ContractFrame",
}


# ---------------------------------------------------------------------------
# (1) Registry == the six Tier-1 names
# ---------------------------------------------------------------------------

def test_rule_registry_is_exactly_the_six_tier1_names():
    """The live rule registry equals the six Tier-1 rule class names — nothing
    more (no hallucinated rules) and nothing less."""
    assert _registry_names() == TIER1_RULE_NAMES


# ---------------------------------------------------------------------------
# (3) _PASS_CONFIGS shape: exactly 5 engine passes, no pass6, critic gate wired
# ---------------------------------------------------------------------------

def test_pass_configs_has_exactly_five_entries():
    """G10 resolution: pass5_mapping IS an engine pass and pass6_checker is NOT,
    so there are exactly five engine-pass configs."""
    assert len(stage3._PASS_CONFIGS) == 5


def test_pass_configs_names_match_expected_order():
    names = [c["name"] for c in stage3._PASS_CONFIGS]
    assert names == EXPECTED_PASS_NAMES


def test_no_pass6_entry_in_pass_configs():
    """G10: pass6_checker must NOT be an engine pass (it has no rule to pick)."""
    pass6_entries = [
        c for c in stage3._PASS_CONFIGS if c["name"].startswith("pass6")
    ]
    assert pass6_entries == []


def test_pass5_mapping_is_an_engine_pass():
    """G10: pass5_mapping is no longer dead code — it is the 5th engine pass."""
    names = [c["name"] for c in stage3._PASS_CONFIGS]
    assert "pass5_mapping" in names


def test_critic_gate_is_wired_as_direct_agent3_call():
    """
    G10 resolution: the pass6 refinement-correctness critic is wired as a DIRECT
    Agent-3 call, not an engine pass. Both the agent entry point and the stage3
    gate function must exist.
    """
    import pipeline.agents.agent3 as agent3

    assert hasattr(agent3, "critique_refinement"), (
        "agent3.critique_refinement (the pass6 critic backing) must exist"
    )
    assert callable(agent3.critique_refinement)
    assert hasattr(stage3, "_run_refinement_critic"), (
        "stage3._run_refinement_critic (the critic GATE) must exist"
    )
    assert callable(stage3._run_refinement_critic)


# ---------------------------------------------------------------------------
# (2) Per-pass: allowed ⊆ registry, and every named rule ∈ registry ∩ allowed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "pass_cfg", stage3._PASS_CONFIGS, ids=[c["name"] for c in stage3._PASS_CONFIGS]
)
def test_pass_allowed_set_is_subset_of_registry(pass_cfg):
    """Each pass's `allowed` set must be a subset of the registered rules."""
    allowed = pass_cfg["allowed"]
    registry = _registry_names()
    assert allowed, f"{pass_cfg['name']} has an empty allowed set"
    assert allowed <= registry, (
        f"{pass_cfg['name']} allows non-registered rules: {allowed - registry}"
    )


@pytest.mark.parametrize(
    "pass_cfg", stage3._PASS_CONFIGS, ids=[c["name"] for c in stage3._PASS_CONFIGS]
)
def test_allowed_rules_section_matches_registry_and_allowed(pass_cfg):
    """
    G09 direct regression: every rule named in the 'ALLOWED RULES' bullet section
    of the pass SYSTEM prompt must be (a) registered AND (b) in the pass's
    `allowed` set. pass2 previously listed StrengthenDuring/PipingComposition and
    pass3 listed DataRefinement — none registered, none in `allowed`.
    """
    registry = _registry_names()
    allowed = pass_cfg["allowed"]
    section_names = _parse_allowed_rules_section(pass_cfg["system"])

    assert section_names, (
        f"{pass_cfg['name']} SYSTEM prompt has no parseable ALLOWED RULES section"
    )
    for name in section_names:
        assert name in registry, (
            f"{pass_cfg['name']} ALLOWED RULES names unregistered rule {name!r} "
            f"(registry={sorted(registry)})"
        )
        assert name in allowed, (
            f"{pass_cfg['name']} ALLOWED RULES names {name!r} but it is not in "
            f"the pass's allowed set {sorted(allowed)}"
        )


@pytest.mark.parametrize(
    "pass_cfg", stage3._PASS_CONFIGS, ids=[c["name"] for c in stage3._PASS_CONFIGS]
)
def test_rule_used_enum_matches_registry_and_allowed(pass_cfg):
    """
    G09 direct regression: every rule named in the `rule_used` JSON enum of the
    pass SYSTEM prompt must be (a) registered AND (b) in the pass's `allowed` set.
    All five current passes carry a `rule_used` field, so we require it present.
    """
    registry = _registry_names()
    allowed = pass_cfg["allowed"]
    enum_names = _parse_rule_used_enum(pass_cfg["system"])

    assert enum_names, (
        f"{pass_cfg['name']} SYSTEM prompt has no parseable `rule_used` enum"
    )
    for name in enum_names:
        assert name in registry, (
            f"{pass_cfg['name']} rule_used enum names unregistered rule {name!r} "
            f"(registry={sorted(registry)})"
        )
        assert name in allowed, (
            f"{pass_cfg['name']} rule_used enum names {name!r} but it is not in "
            f"the pass's allowed set {sorted(allowed)}"
        )


@pytest.mark.parametrize(
    "pass_cfg", stage3._PASS_CONFIGS, ids=[c["name"] for c in stage3._PASS_CONFIGS]
)
def test_rule_used_enum_equals_allowed_set(pass_cfg):
    """
    Stronger consistency: the set of rules offered in the `rule_used` enum should
    be EXACTLY the pass's allowed set — no advertised rule the engine forbids, and
    no allowed rule the prompt hides from the LLM. (Both directions of G09.)
    """
    allowed = pass_cfg["allowed"]
    enum_names = set(_parse_rule_used_enum(pass_cfg["system"]))
    assert enum_names == allowed, (
        f"{pass_cfg['name']} rule_used enum {sorted(enum_names)} != allowed "
        f"{sorted(allowed)}"
    )


@pytest.mark.parametrize(
    "pass_cfg", stage3._PASS_CONFIGS, ids=[c["name"] for c in stage3._PASS_CONFIGS]
)
def test_no_known_bad_rule_names_leak_into_template(pass_cfg):
    """
    G09 belt-and-suspenders: the specific unregistered names that the fix removed
    (and the Tier-2 names that are not in RULE_REGISTRY) must not appear anywhere
    in the pass SYSTEM prompt.
    """
    system_prompt = pass_cfg["system"]
    leaked = {
        bad for bad in _KNOWN_BAD_NAMES
        if re.search(rf"\b{re.escape(bad)}\b", system_prompt)
    }
    assert not leaked, (
        f"{pass_cfg['name']} SYSTEM prompt leaks unregistered rule name(s): "
        f"{sorted(leaked)}"
    )


@pytest.mark.parametrize(
    "pass_cfg", stage3._PASS_CONFIGS, ids=[c["name"] for c in stage3._PASS_CONFIGS]
)
def test_every_recognised_rule_token_is_in_allowed(pass_cfg):
    """
    Broad catch: any of the six registered rule names that appears as a standalone
    token in the SYSTEM prompt must be in this pass's allowed set. (A prompt that
    name-drops a rule it is not permitted to use would mislead the LLM.)
    """
    allowed = pass_cfg["allowed"]
    tokens = _all_rule_tokens(pass_cfg["system"])
    stray = tokens - allowed
    assert not stray, (
        f"{pass_cfg['name']} SYSTEM prompt references registered rule(s) "
        f"{sorted(stray)} that are not in its allowed set {sorted(allowed)}"
    )


# ---------------------------------------------------------------------------
# (4) Every pass SYSTEM prompt is non-empty
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "pass_cfg", stage3._PASS_CONFIGS, ids=[c["name"] for c in stage3._PASS_CONFIGS]
)
def test_pass_system_prompt_non_empty(pass_cfg):
    system_prompt = pass_cfg["system"]
    assert isinstance(system_prompt, str)
    assert system_prompt.strip(), f"{pass_cfg['name']} has an empty SYSTEM prompt"
