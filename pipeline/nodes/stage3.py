import json
from pathlib import Path

from pipeline.schemas import PlusCalImpl, PortEntry, RTLOutput
from pipeline.state import PipelineState

_VERILOG_CONTENT = """\
module counter_2bit (
    input  wire       clk,
    input  wire       rst_n,
    output reg  [1:0] count
);
    always @(posedge clk) begin
        if (!rst_n)
            count <= 2'b00;
        else
            count <= count + 2'b01;
    end
endmodule
"""


def stage3_node(state: PipelineState) -> PipelineState:
    run_id = state["run_id"]
    artifacts_dir = Path("artifacts") / run_id

    with open(artifacts_dir / "02_pluscal_impl.json") as f:
        PlusCalImpl.model_validate(json.load(f))

    rtl_dir = artifacts_dir / "rtl"
    rtl_dir.mkdir(exist_ok=True)

    verilog_path = rtl_dir / "counter_2bit.v"
    verilog_path.write_text(_VERILOG_CONTENT)

    output = RTLOutput(
        run_id=run_id,
        status="success",
        design_name="counter_2bit",
        compilation_path="direct_structural",
        bsv_source_path=None,
        verilog_path=str(verilog_path),
        top_module_name="counter_2bit",
        port_list=[
            PortEntry(name="clk",   direction="input",  width=1, description="Clock"),
            PortEntry(name="rst_n", direction="input",  width=1, description="Synchronous active-low reset"),
            PortEntry(name="count", direction="output", width=2, description="2-bit counter value"),
        ],
        lint_passed=True,
        lint_tool="none",
        compilation_log=["Stub: hand-written Verilog, no compilation performed"],
        assumptions_made=["synchronous active-low reset", "2-bit output width"],
        error_log=[],
    )

    output_path = artifacts_dir / "03_rtl_output.json"
    output_path.write_text(output.model_dump_json(indent=2))
    print(f"[Stage 3] {output_path}")
    return state
