"""
Tests for the verified loop-introduction refinement:

  - the obligation KERNEL (pipeline.refinement.obligations): O1/O2/O3 hold for the
    correct shift-add multiplier params (exhaustive PROOF over a small width), the
    NON-VACUOUS negative control (a broken body / wrong invariant makes O2/O3 FAIL
    with a counterexample), the exhaustive-vs-sampled mode boundary, and an
    end-to-end equivalence check (derived loop result == a*b);

  - the RULE (pipeline.refinement.rules.LoopIntroduction): is_applicable gating,
    a verified apply() that transforms the spec (fresh vars concrete, body installed,
    obligations recorded, abstraction_mapping set) and is PURE (inputs unmutated),
    a no-op/backtrack apply() when the obligations fail, and ValueError on malformed
    params.

These pin both the soundness gate and the transform without any LLM.
"""

import copy

import pytest

from pipeline.refinement.obligations import (
    discharge_loop_obligations,
    ObligationResult,
)
from pipeline.refinement.rules.loop_introduction import LoopIntroduction
from pipeline.cocotb.spec_sim import _eval


# ---------------------------------------------------------------------------
# The correct shift-add derivation of product = a * b.
# The accumulator IS the output `product` (no separate `acc`); the proposer uses
# product as the accumulator and the mapping is the identity on product.
# ---------------------------------------------------------------------------

def _good_params(width: int = 6):
    return dict(
        post="product = a * b",
        invariant="product + mplier * mcand = a * b",
        variant="count",
        guard="count > 0",
        init={"product": "0", "mcand": "a", "mplier": "b", "count": "8"},
        body={
            "product": "IF (mplier % 2) = 1 THEN product + mcand ELSE product",
            "mcand": "mcand * 2",
            "mplier": "mplier / 2",
            "count": "count - 1",
        },
        mapping={"product": "product"},  # identity: product accumulates the result
        input_widths={"a": width, "b": width},
    )


# ===========================================================================
# KERNEL
# ===========================================================================

def test_kernel_obligations_hold_exhaustive_proof():
    """O1/O2/O3 all hold for the correct multiplier params, and a small width is a
    real EXHAUSTIVE PROOF over the input space."""
    r = discharge_loop_obligations(**_good_params(width=6))
    assert isinstance(r, ObligationResult)
    assert r.ok is True
    assert r.obligations == {"O1": True, "O2": True, "O3": True}
    assert r.mode == "exhaustive-proof"     # 64*64 = 4096 <= 65536
    assert r.cases_checked == 4096
    assert r.counterexample is None


def test_kernel_mode_boundary_exhaustive_vs_sampled():
    """8-bit (256*256 == threshold) is exhaustive-proof; lowering the threshold
    forces honest 'sampled' mode."""
    r_exh = discharge_loop_obligations(**_good_params(width=8))
    assert r_exh.ok is True
    assert r_exh.mode == "exhaustive-proof"
    assert r_exh.cases_checked == 65536

    r_smp = discharge_loop_obligations(
        exhaustive_threshold=4096, **_good_params(width=8)
    )
    assert r_smp.ok is True
    assert r_smp.mode == "sampled"
    assert r_smp.cases_checked < 65536      # only a battery, not the full space


def test_kernel_nonvacuous_broken_body_caught_by_O2():
    """NON-VACUOUS negative control: drop the `mcand*2` left-shift and O2 must FAIL
    with a concrete counterexample (the checker is not a rubber stamp)."""
    bad = _good_params(width=6)
    bad["body"] = dict(bad["body"], mcand="mcand")   # forget the shift
    r = discharge_loop_obligations(**bad)
    assert r.ok is False
    assert r.obligations["O1"] is True
    assert r.obligations["O2"] is False
    assert r.counterexample is not None
    assert r.counterexample["obligation"] == "O2"
    assert "inputs" in r.counterexample and "state" in r.counterexample


def test_kernel_nonvacuous_wrong_invariant_caught():
    """A wrong invariant (off by one) is caught (O1 fails after init)."""
    bad = _good_params(width=6)
    bad["invariant"] = "product + mplier * mcand = a * b + 1"
    r = discharge_loop_obligations(**bad)
    assert r.ok is False
    assert r.obligations["O1"] is False
    assert r.counterexample["obligation"] == "O1"


def test_kernel_end_to_end_equivalence_equals_product():
    """Independent witness: run the DERIVED loop to completion via the real
    evaluator; the accumulator equals a*b for every pair in a small window."""
    p = _good_params(width=6)
    for a in range(0, 64):
        for b in range(0, 64):
            env = {"a": a, "b": b}
            for var, expr in p["init"].items():
                env[var] = _eval(expr, env)
            iters = 0
            while _eval(p["guard"], env) == 1 and iters < 64:
                nxt = dict(env)
                for var, expr in p["body"].items():
                    nxt[var] = _eval(expr, env)   # read-before-write
                env = nxt
                iters += 1
            assert env["product"] == a * b, (a, b, env["product"])


# ===========================================================================
# RULE FIXTURE — a minimal abstract multiplier engine-spec
# ===========================================================================

def _abstract_multiplier_spec():
    """One spec_statement action establishing product' = a*b; product abstract."""
    return {
        "variables": [
            {"name": "a", "type": "0..255", "width": 8, "abstract": False,
             "reset_value": None, "clocked": False},
            {"name": "b", "type": "0..255", "width": 8, "abstract": False,
             "reset_value": None, "clocked": False},
            {"name": "product", "type": "0..65535", "width": 16,
             "abstract": True, "reset_value": None, "clocked": False},
        ],
        "actions": [
            {
                "name": "Multiply",
                "guard": "TRUE",
                "updates": [{"variable": "product", "expression": "a * b"}],
                "is_rtl_style": False,
                "spec_statement": True,
                "postcondition": "product = a * b",
            }
        ],
        "init": "product = 0",
        "invariants": [],
        "abstraction_mapping": {},
        "reset_action": None,
        "properties": [],
    }


def _rule_params():
    p = _good_params(width=6)
    p["action_name"] = "Multiply"
    p["postcondition"] = p.pop("post")
    p["fresh_vars"] = [
        {"name": "mcand", "width": 16, "type": "0..65535"},
        {"name": "mplier", "width": 8, "type": "0..255"},
        {"name": "count", "width": 5, "type": "0..16"},
    ]
    return p


# ===========================================================================
# RULE
# ===========================================================================

def test_rule_is_applicable_true_on_marked_abstract_spec():
    rule = LoopIntroduction()
    assert rule.is_applicable(_abstract_multiplier_spec()) is True


def test_rule_is_applicable_false_without_marker():
    """No spec_statement marker -> not applicable (inert)."""
    rule = LoopIntroduction()
    spec = _abstract_multiplier_spec()
    del spec["actions"][0]["spec_statement"]
    assert rule.is_applicable(spec) is False


def test_rule_is_applicable_false_when_target_concrete():
    """Marker present but the target var is already concrete -> not applicable."""
    rule = LoopIntroduction()
    spec = _abstract_multiplier_spec()
    for v in spec["variables"]:
        if v["name"] == "product":
            v["abstract"] = False
    assert rule.is_applicable(spec) is False


def test_rule_apply_transforms_and_is_pure():
    """A verified apply(): fresh vars become concrete registers, the verified body
    is installed, the abstract var/markers are cleared, abstraction_mapping is set,
    and the discharged obligations are recorded. And it is PURE (inputs unmutated)."""
    rule = LoopIntroduction()
    spec = _abstract_multiplier_spec()
    params = _rule_params()

    spec_before = copy.deepcopy(spec)
    params_before = copy.deepcopy(params)

    out = rule.apply(spec, params)

    # purity: inputs untouched
    assert spec == spec_before
    assert params == params_before
    assert out is not spec

    by_name = {v["name"]: v for v in out["variables"]}
    # fresh vars introduced as concrete clocked registers
    for fv in ("mcand", "mplier", "count"):
        assert fv in by_name, fv
        assert by_name[fv]["abstract"] is False
        assert by_name[fv]["clocked"] is True
    # the abstract target is now concrete
    assert by_name["product"]["abstract"] is False

    action = next(a for a in out["actions"] if a["name"] == "Multiply")
    # marker cleared, loop guarded + clocked
    assert "spec_statement" not in action
    assert action.get("clocked") is True
    assert action["guard"] == "count > 0"
    # the verified body replaced the abstract update
    installed = {u["variable"]: u["expression"] for u in action["updates"]}
    assert installed == params["body"]
    # obligations recorded for the audit trail / critic
    rec = action["refinement"]
    assert rec["obligations"] == {"O1": True, "O2": True, "O3": True}
    assert rec["mode"] == "exhaustive-proof"
    assert rec["invariant"] == params["invariant"]
    assert rec["variant"] == params["variant"]
    # abstraction mapping recorded on the spec
    assert out["abstraction_mapping"]["product"] == "product"


def test_rule_apply_noop_on_wrong_invariant():
    """A WRONG invariant fails the obligations -> apply() returns the spec UNCHANGED
    (the no-op/backtrack signal the engine relies on)."""
    rule = LoopIntroduction()
    spec = _abstract_multiplier_spec()
    bad = _rule_params()
    bad["invariant"] = "product + mplier * mcand = a * b + 1"   # off by one

    out = rule.apply(spec, bad)
    assert out == spec        # unchanged deepcopy -> engine no-op guard fires
    assert out is not spec


def test_rule_apply_noop_on_broken_body():
    """A broken body (no shift) fails O2 -> apply() is a no-op."""
    rule = LoopIntroduction()
    spec = _abstract_multiplier_spec()
    bad = _rule_params()
    bad["body"] = dict(bad["body"], mcand="mcand")   # drop the left-shift

    out = rule.apply(spec, bad)
    assert out == spec
    assert out is not spec


def test_rule_apply_raises_on_missing_param():
    """Malformed params (missing key) raise ValueError -> engine excludes (not a
    no-op): a structurally broken proposal, not a failed obligation."""
    rule = LoopIntroduction()
    spec = _abstract_multiplier_spec()
    bad = _rule_params()
    del bad["invariant"]
    with pytest.raises(ValueError):
        rule.apply(spec, bad)


def test_rule_describe_is_one_line_string():
    rule = LoopIntroduction()
    d = rule.describe()
    assert isinstance(d, str) and "LoopIntroduction" in d
