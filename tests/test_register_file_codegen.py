"""
Regression tests for register-file / memory-array codegen.

The register file is the first design with a MEMORY ARRAY (`reg [w-1:0] mem
[0:K-1]`) and indexed state (`mem[waddr] <= wdata`, `rdata <= mem[raddr]`). This
file pins every layer of that path so a future change that breaks any link fails
here with a precise message:

  schema     — Variable.depth marks a memory array.
  Compiler 1 — an indexed-write update key emits the VALID TLA+ EXCEPT form
               (mem[i]' = e is illegal) and never throws (a throw would set
               tlc_errors and halt Stage 3); memories carry no range constraint.
  bridge     — an indexed update KEY (`mem[waddr]`) parses into an engine-spec
               update {variable, index, expression}; the write ADDRESS (in the
               index, not the RHS) is still detected as a free input.
  engine     — is_rtl_style carves a memory out of the reset-value requirement
               but does NOT relax it for ordinary registers; Initialization stops
               being applicable once non-memory state is reset.
  Compiler 2 — emits the memory array decl, the indexed write, the registered
               read, classifies the memory as internal (never a port), and lints.
  convergence— Init + Iteration(Write) + Iteration(Read) reaches RTL-style with
               only the existing Tier-1 rules (registered read => both clocked).
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from pipeline.schemas.tla_schema import FormalSpec, Variable
from pipeline.compilers import compiler1
from pipeline.compilers.compiler2 import RTLTLACompiler, verify_banlist, MultiDriverError
from pipeline.refinement.bridge import (
    formal_spec_to_engine_spec,
    engine_spec_to_rtl_tla,
    _build_update,
    _update_lhs,
    _tla_primed_update,
)
from pipeline.refinement.engine import _replay_chain, is_rtl_style, run as engine_run
from pipeline.refinement.rules.initialization import Initialization
from tests.fixtures.medium_designs import (
    register_file_formal_spec,
    register_file_picker_sequence,
    register_file_summary,
    _RF_DEPTH,
    _RF_AW,
    _RF_DW,
)

_PORT_WIDTHS = {"clk": 1, "reset": 1, "we": 1, "waddr": _RF_AW, "wdata": _RF_DW, "raddr": _RF_AW}
_CHAIN = [{"rule_name": n, "params": p} for n, p in register_file_picker_sequence()]


def _build_verilog() -> str:
    eng = formal_spec_to_engine_spec(register_file_formal_spec())
    refined = _replay_chain(eng, _CHAIN)
    rtl = engine_spec_to_rtl_tla(
        refined, "register_file", port_widths=_PORT_WIDTHS,
        reset_port="reset", reset_active_low=False,
    )
    return RTLTLACompiler(rtl, reset_port="reset", reset_active_low=False).compile(
        module_name="register_file"
    )


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

def test_variable_depth_field():
    mem = Variable(type="Nat", width=8, depth=8)
    assert mem.depth == 8
    scalar = Variable(type="Nat", width=8)
    assert scalar.depth is None  # backward compatible: scalars omit depth


# ---------------------------------------------------------------------------
# bridge — indexed update parsing + free-input detection
# ---------------------------------------------------------------------------

def test_build_update_parses_indexed_key():
    assert _build_update("mem[waddr]", "wdata") == {
        "variable": "mem", "index": "waddr", "expression": "wdata"
    }
    assert _build_update("rdata", "mem[raddr]") == {
        "variable": "rdata", "expression": "mem[raddr]"
    }
    # word operators fold to symbolic in BOTH index and value
    assert _build_update("mem[i mod 8]", "a AND b") == {
        "variable": "mem", "index": "i % 8", "expression": "a /\\ b"
    }


def test_update_lhs_renders_index():
    assert _update_lhs({"variable": "mem", "index": "waddr", "expression": "wdata"}) == "mem[waddr]"
    assert _update_lhs({"variable": "rdata", "expression": "x"}) == "rdata"


def test_engine_spec_carries_depth_and_index():
    eng = formal_spec_to_engine_spec(register_file_formal_spec())
    mem = next(v for v in eng["variables"] if v["name"] == "mem")
    assert mem["depth"] == _RF_DEPTH
    rdata = next(v for v in eng["variables"] if v["name"] == "rdata")
    assert rdata["depth"] is None
    write = next(a for a in eng["actions"] if a["name"] == "Write")
    assert write["updates"] == [{"variable": "mem", "index": "waddr", "expression": "wdata"}]


def test_write_address_detected_as_free_input():
    """`waddr` lives only in the update index, not its RHS — it must still be a port."""
    eng = formal_spec_to_engine_spec(register_file_formal_spec())
    refined = _replay_chain(eng, _CHAIN)
    rtl = engine_spec_to_rtl_tla(
        refined, "register_file", port_widths=_PORT_WIDTHS,
        reset_port="reset", reset_active_low=False,
    )
    # All four address/data/enable inputs reach the VARIABLES block (so Compiler 2
    # declares them as ports). waddr is the load-bearing one (index-only).
    for sig in ("we", "waddr", "wdata", "raddr"):
        assert f"{sig}  \\* width:" in rtl or f"{sig},  \\* width:" in rtl, (
            f"free input {sig} missing from VARIABLES:\n{rtl}"
        )
    # waddr sized from the port hint (3-bit), not defaulted to 1.
    assert "waddr,  \\* width: 3" in rtl or "waddr  \\* width: 3" in rtl


# ---------------------------------------------------------------------------
# Compiler 1 — indexed write -> EXCEPT, no throw, no memory range constraint
# ---------------------------------------------------------------------------

def test_compiler1_indexed_write_emits_except_form():
    tla, cfg = compiler1.compile(register_file_formal_spec())  # must NOT raise
    assert "mem' = [mem EXCEPT ![waddr] = wdata]" in tla
    # mem is written by Write, so it must NOT also be UNCHANGED there.
    assert "UNCHANGED <<mem>>" not in _action_block(tla, "Write")
    # but the Read action leaves mem unchanged.
    assert "UNCHANGED <<mem>>" in _action_block(tla, "Read")
    # a memory carries no `mem \in 0..N` range constraint (it is a function).
    assert "mem \\in" not in tla
    # the scalar read register still gets its range.
    assert "rdata \\in 0..255" in tla


def _action_block(tla: str, action: str) -> str:
    """Slice the text of one named TLA+ action definition."""
    lines = tla.splitlines()
    out, capturing = [], False
    for ln in lines:
        if ln.startswith(f"{action} =="):
            capturing = True
            continue
        if capturing:
            if ln and not ln.startswith(" ") and "==" in ln:
                break
            out.append(ln)
    return "\n".join(out)


def test_tla_primed_update_helper():
    assert _tla_primed_update("rdata", "mem[raddr]") == "rdata' = mem[raddr]"
    assert _tla_primed_update("mem[waddr]", "wdata") == "mem' = [mem EXCEPT ![waddr] = wdata]"


# ---------------------------------------------------------------------------
# engine — is_rtl_style memory carve-out (and NO over-relax)
# ---------------------------------------------------------------------------

def test_is_rtl_style_memory_needs_no_reset():
    eng = formal_spec_to_engine_spec(register_file_formal_spec())
    refined = _replay_chain(eng, _CHAIN)
    mem = next(v for v in refined["variables"] if v["name"] == "mem")
    assert mem["reset_value"] is None and mem["depth"] == _RF_DEPTH  # un-reset memory
    assert mem["clocked"] is True and mem["abstract"] is False        # but concrete
    assert is_rtl_style(refined) is True


def test_is_rtl_style_still_requires_reset_for_ordinary_register():
    """The carve-out is for memories ONLY — a scalar register without a reset
    value must STILL fail is_rtl_style (no accidental global relaxation)."""
    eng = formal_spec_to_engine_spec(register_file_formal_spec())
    refined = _replay_chain(eng, _CHAIN)
    # Strip rdata's reset value: it is an ordinary register (no depth), so the
    # spec must no longer be RTL-style.
    for v in refined["variables"]:
        if v["name"] == "rdata":
            v["reset_value"] = None
    assert is_rtl_style(refined) is False


def test_initialization_not_applicable_after_reset_despite_memory():
    """Initialization must stop firing once non-memory state is reset, even though
    the memory's reset_value stays None (else it fires forever -> stall)."""
    eng = formal_spec_to_engine_spec(register_file_formal_spec())
    init = Initialization()
    assert init.is_applicable(eng) is True               # nothing reset yet
    after_init = _replay_chain(eng, _CHAIN[:1])           # apply Initialization
    assert init.is_applicable(after_init) is False, (
        "Initialization still applicable after reset — the un-reset memory wrongly "
        "keeps it firing"
    )


def test_register_file_converges_via_engine_run():
    """The engine reaches RTL-style with ONLY existing Tier-1 rules (Init +
    Iteration on each clocked port), via an applicability-driven idempotent picker."""
    from pipeline.refinement.engine import RULE_REGISTRY
    rule_by_name = {r.__class__.__name__: r for r in RULE_REGISTRY}
    seq = register_file_picker_sequence()

    def picker(applicable_rules, spec):
        names = {r["name"] for r in applicable_rules}
        for name, params in seq:
            if name not in names:
                continue
            if rule_by_name[name].apply(spec, params) == spec:
                continue  # no-op -> advance
            return {"rule_name": name, "params": params}
        return {"rule_name": applicable_rules[0]["name"], "params": {}}

    eng = formal_spec_to_engine_spec(register_file_formal_spec())
    final = engine_run(eng, picker, run_id="rf_converge_test", max_steps=12)
    assert is_rtl_style(final) is True
    assert {a["name"] for a in final["actions"] if a.get("clocked")} == {"Write", "Read"}


# ---------------------------------------------------------------------------
# Compiler 2 — array decl, indexed write, registered read, classification
# ---------------------------------------------------------------------------

def test_compiler2_emits_memory_array_and_indexed_access():
    v = _build_verilog()
    assert "reg  [7:0] mem [0:7];" in v                     # memory array decl
    assert "mem[waddr] <=" in v                              # indexed write LHS
    assert "rdata <= mem[raddr];" in v                       # registered read
    assert "output reg [7:0] rdata" in v                     # read-port output reg
    assert "always @(posedge clk)" in v
    assert "if (reset)" in v and "rdata <= 0;" in v          # rdata resets
    assert "mem[waddr]'" not in v                            # no leaked primed LHS


def test_compiler2_memory_is_internal_not_a_port():
    v = _build_verilog()
    header = v.split("(", 1)[1].split(");", 1)[0]
    assert "mem" not in header, f"memory leaked into ports:\n{header}"
    # the scalar interface is exactly the address/data/enable/result ports.
    assert "input  [2:0] waddr" in v
    assert "input  [2:0] raddr" in v
    assert "input  [7:0] wdata" in v
    assert "input  we" in v


def test_compiler2_no_multidriver_for_memory():
    """mem is driven by exactly one block (UpdatePipeline); building must not raise."""
    try:
        _build_verilog()
    except MultiDriverError as exc:  # pragma: no cover - failure path
        pytest.fail(f"unexpected MultiDriverError for the register file: {exc}")


def test_compiler2_register_file_banlist_clean():
    verify_banlist(_build_verilog())  # raises BanlistViolation on a banned construct


@pytest.mark.skipif(not shutil.which("iverilog"), reason="iverilog not installed")
def test_register_file_lints_clean(tmp_path):
    p = tmp_path / "rf.v"
    p.write_text(_build_verilog())
    r = subprocess.run(
        ["iverilog", "-Wall", "-t", "null", str(p)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"lint failed:\n{r.stdout}\n{r.stderr}\n\n{p.read_text()}"


# ---------------------------------------------------------------------------
# Hardening (from the adversarial review): the index must survive EVERY
# refinement path a live picker might take, a whitespace key must parse, and
# Initialization must never reset the memory.
# ---------------------------------------------------------------------------

# A live picker could model the we-gated write as a mux (Alternation) rather than
# leaving it a flat update. Before the fix, the composition paths dropped the
# array index -> a whole-array `mem <= ...` that iverilog rejects, AND `waddr`
# vanished from the ports — yet is_rtl_style still returned True (the engine
# committed the derail). This chain Alternation-splits Write, then clocks both.
_ALT_CHAIN = [
    {"rule_name": "Initialization",
     "params": {"reset_values": {"rdata": "0"}, "reset_action_name": "Reset"}},
    {"rule_name": "Alternation", "params": {
        "action_name": "Write",
        "branches": [{"guard": "we = 1",
                      "updates": [{"variable": "mem", "index": "waddr", "expression": "wdata"}]}],
    }},
    {"rule_name": "Iteration", "params": {"action_name": "Write"}},
    {"rule_name": "Iteration", "params": {"action_name": "Read"}},
]


def _build_verilog_from_chain(chain):
    eng = formal_spec_to_engine_spec(register_file_formal_spec())
    refined = _replay_chain(eng, chain)
    assert is_rtl_style(refined), "chain did not reach RTL-style"
    rtl = engine_spec_to_rtl_tla(
        refined, "register_file", port_widths=_PORT_WIDTHS,
        reset_port="reset", reset_active_low=False,
    )
    return RTLTLACompiler(rtl, reset_port="reset", reset_active_low=False).compile(
        module_name="register_file"
    )


def test_alternation_on_write_keeps_the_index():
    """Splitting the we-gated write via Alternation must STILL emit an indexed
    write and keep waddr a port — not a whole-array assignment."""
    v = _build_verilog_from_chain(_ALT_CHAIN)
    assert "mem[waddr] <=" in v, f"index dropped by Alternation path:\n{v}"
    # never a whole-array write to the unpacked memory.
    assert "mem <=" not in v
    assert "input  [2:0] waddr" in v, f"waddr vanished from the ports:\n{v}"
    verify_banlist(v)


@pytest.mark.skipif(not shutil.which("iverilog"), reason="iverilog not installed")
def test_alternation_on_write_lints_clean(tmp_path):
    p = tmp_path / "rf_alt.v"
    p.write_text(_build_verilog_from_chain(_ALT_CHAIN))
    r = subprocess.run(["iverilog", "-Wall", "-t", "null", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, (
        "Alternation-split register file failed lint (whole-array assignment?):\n"
        f"{r.stdout}\n{r.stderr}\n\n{p.read_text()}"
    )


def test_whitespace_index_key_parses_consistently():
    """A key written with a space (`mem [waddr]`) must parse the same in the bridge
    and Compiler 1 (a divergence drops the write on one path only)."""
    assert _build_update("mem [waddr]", "wdata") == {
        "variable": "mem", "index": "waddr", "expression": "wdata"
    }
    # Compiler 1 also accepts it and emits the EXCEPT form (no throw, no scalar mis-parse).
    spec = FormalSpec(
        module_name="rf_ws", description="whitespace key",
        variables={"mem": {"type": "Nat", "width": 8, "depth": 8},
                   "rdata": {"type": "Nat", "width": 8}},
        initial={"rdata": "0"},
        transitions=[
            {"label": "Write", "condition": "we = 1", "updates": {"mem [waddr]": "wdata"}},
            {"label": "Read", "condition": "TRUE", "updates": {"rdata": "mem[raddr]"}},
        ],
        invariants=[],
    )
    tla, _ = compiler1.compile(spec)
    assert "mem' = [mem EXCEPT ![waddr] = wdata]" in tla


def test_initialization_never_resets_the_memory():
    """Even if a (mis-behaving) picker puts the memory in reset_values, Initialization
    must NOT give it a reset value — a whole-array `mem <= 0` is illegal Verilog."""
    eng = formal_spec_to_engine_spec(register_file_formal_spec())
    bad_init = [{"rule_name": "Initialization", "params": {
        "reset_values": {"rdata": "0", "mem": "0"},  # mem wrongly included
        "reset_action_name": "Reset"}}]
    after = _replay_chain(eng, bad_init)
    mem = next(v for v in after["variables"] if v["name"] == "mem")
    assert mem["reset_value"] is None, "memory was given a reset value"
    reset = next(a for a in after["actions"] if a["name"] == "Reset")
    assert all(u["variable"] != "mem" for u in reset["updates"]), "memory in reset updates"
    # And the full build still emits no whole-array reset.
    full = _build_verilog_from_chain([
        bad_init[0],
        {"rule_name": "Iteration", "params": {"action_name": "Write"}},
        {"rule_name": "Iteration", "params": {"action_name": "Read"}},
    ])
    assert "mem <= 0" not in full
