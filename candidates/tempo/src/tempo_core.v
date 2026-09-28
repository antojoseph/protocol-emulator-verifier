`default_nettype none

// A single deterministic Tempo protocol engine. All timing uses clock enables;
// the wrapper supplies synchronized pins, instruction storage, and byte FIFOs.
module tempo_core (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        ena,
    input  wire        run,
    input  wire        restart,
    input  wire        external_fault,
    input  wire [15:0] divider,
    input  wire [15:0] timeout_limit,
    input  wire [7:0]  pins,
    input  wire [1:0]  events,
    input  wire [23:0] instruction,
    input  wire        instruction_valid,
    input  wire [7:0]  tx_data,
    input  wire        tx_valid,
    input  wire        rx_ready,
    output reg  [5:0]  pc,
    output reg  [7:0]  pin_out,
    output reg  [7:0]  pin_dir,
    output wire        tx_pop,
    output wire        rx_push,
    output wire [7:0]  rx_data,
    output reg  [1:0]  event_set,
    output reg  [1:0]  event_clear,
    output wire        active,
    output reg         halted,
    output reg         fault,
    output reg  [3:0]  fault_code,
    output reg  [5:0]  fault_pc,
    output reg  [15:0] fault_time,
    output reg  [23:0] capture,
    output reg         capture_valid,
    output reg  [15:0] cycle_count,
    output wire [7:0]  debug_x,
    output wire [7:0]  debug_y,
    output wire [7:0]  debug_shift
);
    reg [7:0] x;
    reg [7:0] y;
    reg [7:0] shift_reg;
    reg [15:0] divider_phase;
    reg [7:0] delay_remaining;
    reg [15:0] failed_observations;

    wire [3:0] opcode = instruction[23:20];
    wire [3:0] argument = instruction[19:16];
    wire [7:0] operand = instruction[15:8];
    wire [7:0] delay_ticks = instruction[7:0];

    // Do not gate active with external_fault: the wrapper derives its ownership
    // conflict from active. Gating it here would create a combinational loop.
    assign active = ena && run && !halted && !fault;
    wire tick = active && (divider_phase == 16'd0);
    wire execute = rst_n && tick && !restart && !external_fault &&
                   (delay_remaining == 8'd0) && instruction_valid;

    wire illegal = ((opcode >= 4'hb) && (opcode <= 4'he)) ||
                   ((opcode == 4'h1) && (argument > 4'd8)) ||
                   ((opcode == 4'h4) && (argument > 4'd8)) ||
                   ((opcode == 4'h8) && (argument > 4'd8)) ||
                   ((opcode == 4'h9) && !((argument <= 4'd3) ||
                                         (argument == 4'd8) ||
                                         (argument == 4'd9)));

    wire stalled = ((opcode == 4'h5) &&
                    (pins[argument[2:0]] != argument[3])) ||
                   ((opcode == 4'h6) && !tx_valid) ||
                   ((opcode == 4'h7) && !rx_ready) ||
                   ((opcode == 4'h9) && (argument == 4'd8) && !events[0]) ||
                   ((opcode == 4'h9) && (argument == 4'd9) && !events[1]);
    wire timeout_now = (timeout_limit != 16'd0) &&
                       (failed_observations >= (timeout_limit - 16'd1));

    assign tx_pop = execute && (opcode == 4'h6) && tx_valid;
    assign rx_push = execute && (opcode == 4'h7) && rx_ready;
    assign rx_data = shift_reg;
    assign debug_x = x;
    assign debug_y = y;
    assign debug_shift = shift_reg;

    // Requests are sampled by shared event/FIFO storage at the same edge that
    // executes the instruction. They cannot repeat throughout a delay or pause.
    always @* begin
        event_set = 2'b00;
        event_clear = 2'b00;
        if (execute && (opcode == 4'h9)) begin
            case (argument)
                4'd0: event_set = 2'b01;
                4'd1: event_set = 2'b10;
                4'd2: event_clear = 2'b01;
                4'd3: event_clear = 2'b10;
                4'd8: if (events[0]) event_clear = 2'b01;
                4'd9: if (events[1]) event_clear = 2'b10;
                default: begin end
            endcase
        end
    end

    always @(posedge clk) begin
        if (!rst_n || restart) begin
            pc <= 6'd0;
            pin_out <= 8'd0;
            pin_dir <= 8'd0;
            x <= 8'd0;
            y <= 8'd0;
            shift_reg <= 8'd0;
            divider_phase <= 16'd0;
            delay_remaining <= 8'd0;
            failed_observations <= 16'd0;
            halted <= 1'b0;
            fault <= 1'b0;
            fault_code <= 4'd0;
            fault_pc <= 6'd0;
            fault_time <= 16'd0;
            capture <= 24'd0;
            capture_valid <= 1'b0;
            cycle_count <= 16'd0;
        end else if (ena) begin
            // Timestamps always refer to the counter value before this edge.
            cycle_count <= cycle_count + 16'd1;
            if (active) begin
                if (external_fault) begin
                    fault <= 1'b1;
                    fault_code <= 4'd4;
                    fault_pc <= pc;
                    fault_time <= cycle_count;
                    pin_dir <= 8'd0;
                end else if (divider_phase != 16'd0) begin
                    divider_phase <= divider_phase - 16'd1;
                end else begin
                    divider_phase <= divider;
                    if (delay_remaining != 8'd0) begin
                        delay_remaining <= delay_remaining - 8'd1;
                    end else if (!instruction_valid || illegal ||
                                 (stalled && timeout_now)) begin
                        fault <= 1'b1;
                        fault_code <= !instruction_valid ? 4'd1 :
                                      illegal ? 4'd3 : 4'd2;
                        fault_pc <= pc;
                        fault_time <= cycle_count;
                        pin_dir <= 8'd0;
                    end else if (stalled) begin
                        // Saturation avoids wrap even for an unbounded wait.
                        if (failed_observations != 16'hffff)
                            failed_observations <= failed_observations + 16'd1;
                    end else begin
                        failed_observations <= 16'd0;
                        delay_remaining <= delay_ticks;
                        pc <= pc + 6'd1;
                        case (opcode)
                            4'h0: begin end
                            4'h1: begin
                                case (argument)
                                    4'd0: pin_out <= operand;
                                    4'd1: pin_dir <= operand;
                                    4'd2: x <= operand;
                                    4'd3: y <= operand;
                                    4'd4: shift_reg <= operand;
                                    4'd5: pin_out <= pin_out | operand;
                                    4'd6: pin_out <= pin_out & ~operand;
                                    4'd7: pin_dir <= pin_dir | operand;
                                    4'd8: pin_dir <= pin_dir & ~operand;
                                    default: begin end
                                endcase
                            end
                            4'h2: begin
                                pin_out[argument[2:0]] <= argument[3] ?
                                                         shift_reg[7] : shift_reg[0];
                                shift_reg <= argument[3] ? {shift_reg[6:0], 1'b0} :
                                                           {1'b0, shift_reg[7:1]};
                            end
                            4'h3: begin
                                // Merge mode fills the endpoint vacated by OUT
                                // without a second shift (full-duplex exchange).
                                if (operand[0]) begin
                                    if (argument[3])
                                        shift_reg[0] <= pins[argument[2:0]];
                                    else
                                        shift_reg[7] <= pins[argument[2:0]];
                                end else begin
                                    shift_reg <= argument[3] ?
                                        {shift_reg[6:0], pins[argument[2:0]]} :
                                        {pins[argument[2:0]], shift_reg[7:1]};
                                end
                            end
                            4'h4: begin
                                case (argument)
                                    4'd0: pc <= operand[5:0];
                                    4'd1: if (x != 8'd0) begin
                                        pc <= operand[5:0];
                                        x <= x - 8'd1;
                                    end
                                    4'd2: if (y != 8'd0) begin
                                        pc <= operand[5:0];
                                        y <= y - 8'd1;
                                    end
                                    4'd3: if (shift_reg == 8'd0) pc <= operand[5:0];
                                    4'd4: if (shift_reg != 8'd0) pc <= operand[5:0];
                                    4'd5: if (x == 8'd0) pc <= operand[5:0];
                                    4'd6: if (y == 8'd0) pc <= operand[5:0];
                                    4'd7: if (events[0]) pc <= operand[5:0];
                                    4'd8: if (events[1]) pc <= operand[5:0];
                                    default: begin end
                                endcase
                            end
                            4'h5: begin end
                            4'h6: shift_reg <= tx_data;
                            4'h7: begin end
                            4'h8: begin
                                case (argument)
                                    4'd0: shift_reg <= x;
                                    4'd1: shift_reg <= y;
                                    4'd2: x <= shift_reg;
                                    4'd3: y <= shift_reg;
                                    4'd4: shift_reg <= shift_reg ^ operand;
                                    4'd5: shift_reg <= shift_reg & operand;
                                    4'd6: shift_reg <= shift_reg | operand;
                                    4'd7: shift_reg <= shift_reg + operand;
                                    4'd8: shift_reg <= ~shift_reg;
                                    default: begin end
                                endcase
                            end
                            4'h9: begin end
                            4'ha: begin
                                capture <= {cycle_count, pins};
                                capture_valid <= 1'b1;
                            end
                            4'hf: begin
                                halted <= 1'b1;
                                pin_dir <= 8'd0;
                            end
                            default: begin end
                        endcase
                    end
                end
            end
        end
    end
endmodule

`default_nettype wire
