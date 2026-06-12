`timescale 1ns / 1ps

module shift_add_multiplier (
    input  clk,
    input  reset,
    input  [7:0] a,
    input  [7:0] b,
    input  start,
    output done,
    output reg [15:0] product,
    output reg [15:0] mcand,
    output reg [7:0] mplier,
    output reg [3:0] count,
    output reg [1:0] state
);

    // Combinational logic
    assign done = state == 2;

    // Clocked pipeline evolution
    always @(posedge clk) begin
        if (reset) begin
            product <= 0;
            mcand <= 0;
            mplier <= 0;
            count <= 0;
            state <= 0;
        end else begin
            state <= (((state == 0) || (state == 2)) && start == 1) ? (1) : ((state == 1 && ((count - 1) > 0)) ? (1) : ((state == 1) ? (2) : ((state == 2) ? (0) : (0))));
            product <= (((state == 0) || (state == 2)) && start == 1) ? (0) : ((state == 1 && ((mplier % 2) == 1)) ? (product + mcand) : ((state == 1) ? (product) : (product)));
            mcand <= (((state == 0) || (state == 2)) && start == 1) ? (a) : ((state == 1) ? (mcand * 2) : (mcand));
            mplier <= (((state == 0) || (state == 2)) && start == 1) ? (b) : ((state == 1) ? (mplier / 2) : (mplier));
            count <= (((state == 0) || (state == 2)) && start == 1) ? (8) : ((state == 1) ? (count - 1) : (count));
        end
    end

endmodule