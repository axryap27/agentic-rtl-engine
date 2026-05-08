from typing import Literal, Optional
from pydantic import BaseModel


# ── 00_nl_spec.json ──────────────────────────────────────────────────────────

class PPATargets(BaseModel):
    max_freq_mhz: Optional[float] = None
    max_area_gates: Optional[float] = None
    max_power_mw: Optional[float] = None


class NLSpec(BaseModel):
    schema_version: str
    run_id: str
    created_at: str
    design_name: str
    nl_description: str
    design_class: Literal["combinational", "fsm", "datapath", "memory", "protocol", "pipeline"]
    target_benchmarks: list[str]
    ppa_targets: PPATargets
    additional_constraints: Optional[str] = None


# ── 01_formal_spec.json ───────────────────────────────────────────────────────

class FormalStateVariable(BaseModel):
    name: str
    type: str
    domain: str
    hardware_mapping: str


class Invariant(BaseModel):
    name: str
    formula: str
    property_class: Literal["safety", "mutual_exclusion", "no_deadlock", "data_integrity", "other"]


class LivenessProperty(BaseModel):
    name: str
    formula: str
    property_class: Literal["progress", "response", "fairness"]


class TimingConstraint(BaseModel):
    name: str
    type: str
    value: str
    unit: str
    source_requirement: str


class AreaConstraint(BaseModel):
    name: str
    budget: str
    unit: str
    source_requirement: str


class PowerConstraint(BaseModel):
    name: str
    budget: str
    unit: str
    mode: str
    source_requirement: str


class NFCConstraints(BaseModel):
    timing: list[TimingConstraint] = []
    area: list[AreaConstraint] = []
    power: list[PowerConstraint] = []


class FormalSpec(BaseModel):
    schema_version: str = "1.0"
    run_id: str
    stage: str = "formalization"
    status: Literal["success", "partial", "failed"]
    design_name: str
    tla_module_name: str
    tla_spec_path: str
    tla_cfg_path: str
    tlc_verified: bool
    tla_syntax_valid: bool
    state_variables: list[FormalStateVariable]
    invariants: list[Invariant]
    liveness_properties: list[LivenessProperty]
    nfc_constraints: NFCConstraints
    abstractions_applied: list[str]
    open_ambiguities: list[str]
    error_log: list[str]
    notes: Optional[str] = None


# ── 02_pluscal_impl.json ──────────────────────────────────────────────────────

class PPAImpact(BaseModel):
    power_delta: Optional[str] = None
    performance_delta: Optional[str] = None
    area_delta: Optional[str] = None


class RuleApplied(BaseModel):
    rule_name: str
    design_decision: str
    proof_status: Literal["verified", "pending_tlc", "failed"]
    ppa_impact: PPAImpact


class ConcreteStateVariable(BaseModel):
    name: str
    concrete_type: str
    bsv_mapping: str
    abstract_variable: str


class Process(BaseModel):
    name: str
    description: str
    bsv_mapping: str


class PPAEstimate(BaseModel):
    power_mw: Optional[float] = None
    performance_mhz: Optional[float] = None
    area_gates: Optional[float] = None


class PlusCalImpl(BaseModel):
    schema_version: str = "1.0"
    run_id: str
    stage: str = "refinement"
    status: Literal["success", "partial", "failed"]
    design_name: str
    pluscal_path: str
    refinement_depth: int
    rules_applied: list[RuleApplied]
    refinement_mapping: str
    state_variables: list[ConcreteStateVariable]
    processes: list[Process]
    preserved_invariants: list[str]
    preserved_liveness: list[str]
    backtracks_performed: int
    ppa_estimate: PPAEstimate
    open_issues: list[str]
    error_log: list[str]


# ── 03_rtl_output.json ────────────────────────────────────────────────────────

class PortEntry(BaseModel):
    name: str
    direction: Literal["input", "output", "inout"]
    width: int
    description: str


class RTLOutput(BaseModel):
    schema_version: str = "1.0"
    run_id: str
    stage: str = "codegen"
    status: Literal["success", "partial", "failed"]
    design_name: str
    compilation_path: Literal["bsv", "direct_structural"]
    bsv_source_path: Optional[str] = None
    verilog_path: str
    top_module_name: str
    port_list: list[PortEntry]
    lint_passed: bool
    lint_tool: Literal["verilator", "iverilog", "none"]
    compilation_log: list[str]
    assumptions_made: list[str]
    error_log: list[str]


# ── 04_eval_report.json ───────────────────────────────────────────────────────

class BenchmarkResult(BaseModel):
    ran: bool
    tests_total: Optional[int] = None
    tests_passed: Optional[int] = None
    pass_rate: Optional[float] = None
    failure_signatures: list[str] = []


class CVDPResult(BenchmarkResult):
    line_coverage: Optional[float] = None
    branch_coverage: Optional[float] = None


class FunctionalResults(BaseModel):
    verilogeval: BenchmarkResult
    rtllm: BenchmarkResult
    cvdp: CVDPResult


class PPAReport(BaseModel):
    tool: Literal["yosys", "openroad", "genus", "none"]
    process_node: str
    area_cell_equiv: Optional[float] = None
    area_um2: Optional[float] = None
    max_freq_mhz: Optional[float] = None
    power_mw: Optional[float] = None
    flip_flop_count: Optional[int] = None
    lut_gate_count: Optional[int] = None
    critical_path_ns: Optional[float] = None
    critical_path_desc: Optional[str] = None
    synthesis_warnings: list[str] = []


class PPAVsTargets(BaseModel):
    freq_met: Optional[bool] = None
    area_met: Optional[bool] = None
    power_met: Optional[bool] = None


class EvalReport(BaseModel):
    schema_version: str = "1.0"
    run_id: str
    stage: str = "evaluation"
    status: Literal["success", "partial", "failed"]
    design_name: str
    functional_results: FunctionalResults
    ppa_report: PPAReport
    ppa_vs_targets: PPAVsTargets
    issue_log: list[str]
    demo_summary: str
