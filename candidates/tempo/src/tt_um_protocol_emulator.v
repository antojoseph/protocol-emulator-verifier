`default_nettype none
// Tempo: two programmable timing engines. See docs/architecture.md.
module tt_um_protocol_emulator (
    input wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input wire ena,
    input wire clk,
    input wire rst_n
);
    (* async_reg = "true" *) reg request_meta, request_sync;
    (* async_reg = "true" *) reg [7:0] pin_meta, pin_sync;
    reg acknowledgment;
    reg [3:0] reply, write_low;
    reg [7:0] host_address;
    reg [3:0] read_snapshot_high;
    reg write_low_valid, read_snapshot_valid;
    reg host_error, ownership_error;
    reg [1:0] run_request, restart;
    reg [1:0] events;
    reg [7:0] ownership [0:1];
    reg [7:0] open_drain [0:1];
    reg [15:0] divider [0:1];
    reg [15:0] timeout_limit [0:1];
    reg [5:0] program_address [0:1];
    reg [15:0] program_stage [0:1];
    reg [1:0] program_stage_valid [0:1];
    reg [63:0] program_valid [0:1];
    reg [23:0] program0 [0:63];
    reg [23:0] program1 [0:63];
    reg [15:0] capture_snapshot [0:1];
    reg [7:0] cycle_high_snapshot [0:1];

    wire engine = host_address[7];
    wire [6:0] address = host_address[6:0];
    wire [2:0] command = ui_in[6:4];
    wire transaction = ena && (request_sync != acknowledgment);
    wire write_request = transaction && command == 3 && write_low_valid;
    wire read_request = transaction && command == 4;
    wire [7:0] write_data = {ui_in[3:0], write_low};
    reg [7:0] read_data;
    reg read_allowed, write_allowed;
    wire write_commit = rst_n && write_request && write_allowed;

    wire [5:0] pc [0:1];
    wire [7:0] pin_out [0:1], pin_dir [0:1];
    wire [1:0] event_set [0:1], event_clear [0:1];
    wire [1:0] active, halted, fault, capture_valid;
    wire [3:0] fault_code [0:1];
    wire [5:0] fault_pc [0:1];
    wire [15:0] fault_time [0:1], cycle_count [0:1];
    wire [23:0] capture [0:1];
    wire [7:0] debug_x [0:1], debug_y [0:1], debug_shift [0:1];
    wire [1:0] tx_pop, tx_valid, tx_ready, rx_push, rx_valid, rx_ready;
    wire [7:0] tx_data [0:1], rx_data [0:1], rx_head [0:1];
    wire [2:0] tx_level [0:1], rx_level [0:1];
    wire [23:0] selected_program = engine ? program1[program_address[1]] : program0[program_address[0]];
    wire selected_program_valid = program_valid[engine][program_address[engine]];

    wire collision = active[0] && active[1] && (|(ownership[0] & ownership[1]));
    wire [7:0] eligible0 = {8{active[0] && !restart[0]}} & ownership[0] & pin_dir[0];
    wire [7:0] eligible1 = {8{active[1] && !restart[1]}} & ownership[1] & pin_dir[1];
    wire [7:0] drive0 = eligible0 & ~(open_drain[0] & pin_out[0]);
    wire [7:0] drive1 = eligible1 & ~(open_drain[1] & pin_out[1]);
    assign uio_out = (pin_out[0] & ~open_drain[0] & eligible0) |
                     (pin_out[1] & ~open_drain[1] & eligible1);
    assign uio_oe = (drive0 | drive1) & {8{rst_n && ena && !collision}};
    assign uo_out = {(|fault) | host_error | ownership_error,
                     rx_valid[engine], tx_ready[engine], acknowledgment, reply};

    genvar g;
    generate for (g=0; g<2; g=g+1) begin: engines
        wire [23:0] instruction;
        if (g == 0) begin: bank0
            assign instruction = program0[pc[g]];
        end else begin: bank1
            assign instruction = program1[pc[g]];
        end
        tempo_core core (
            .clk(clk), .rst_n(rst_n), .ena(ena), .run(run_request[g]), .restart(restart[g]),
            .external_fault(collision), .divider(divider[g]), .timeout_limit(timeout_limit[g]),
            .pins(pin_sync), .events(events), .instruction(instruction),
            .instruction_valid(program_valid[g][pc[g]]),
            .tx_data(tx_data[g]), .tx_valid(tx_valid[g]), .rx_ready(rx_ready[g]),
            .pc(pc[g]), .pin_out(pin_out[g]), .pin_dir(pin_dir[g]),
            .tx_pop(tx_pop[g]), .rx_push(rx_push[g]), .rx_data(rx_data[g]),
            .event_set(event_set[g]), .event_clear(event_clear[g]),
            .active(active[g]), .halted(halted[g]), .fault(fault[g]),
            .fault_code(fault_code[g]), .fault_pc(fault_pc[g]), .fault_time(fault_time[g]),
            .capture(capture[g]), .capture_valid(capture_valid[g]), .cycle_count(cycle_count[g]),
            .debug_x(debug_x[g]), .debug_y(debug_y[g]), .debug_shift(debug_shift[g])
        );
        tempo_fifo tx_fifo (
            .clk(clk), .rst_n(rst_n), .flush(restart[g]),
            .write_en(write_commit && engine == g && address == 7'h06), .write_data(write_data),
            .write_ready(tx_ready[g]), .read_en(tx_pop[g]), .read_data(tx_data[g]),
            .read_valid(tx_valid[g]), .level(tx_level[g])
        );
        tempo_fifo rx_fifo (
            .clk(clk), .rst_n(rst_n), .flush(restart[g]),
            .write_en(rx_push[g]), .write_data(rx_data[g]), .write_ready(rx_ready[g]),
            .read_en(read_request && engine == g && address == 7'h07 && !restart[g]),
            .read_data(rx_head[g]), .read_valid(rx_valid[g]), .level(rx_level[g])
        );
    end endgenerate

    // Memory contents deliberately have no reset. Validity gates every execution.
    always @(posedge clk) begin
        if (write_commit && address == 7'h05) begin
            if (engine) program1[program_address[1]] <= {write_data, program_stage[1]};
            else program0[program_address[0]] <= {write_data, program_stage[0]};
        end
    end

    always @* begin
        read_data = 0;
        read_allowed = 1;
        write_allowed = 0;
        case (address)
            7'h00: begin read_data = {7'b0,run_request[engine]}; write_allowed = 1; end
            7'h01: read_data = {ownership_error,host_error,capture_valid[engine],rx_valid[engine],tx_ready[engine],fault[engine],halted[engine],active[engine]};
            7'h02: begin read_data = {2'b0,program_address[engine]}; write_allowed = !run_request[engine] && write_data[7:6] == 0; end
            7'h03: begin read_data = selected_program_valid ? selected_program[7:0] : 8'b0; write_allowed = !run_request[engine]; end
            7'h04: begin read_data = selected_program_valid ? selected_program[15:8] : 8'b0; write_allowed = !run_request[engine]; end
            7'h05: begin read_data = selected_program_valid ? selected_program[23:16] : 8'b0; write_allowed = !run_request[engine] && program_stage_valid[engine] == 2'b11; end
            7'h06: begin read_data = {5'b0,tx_level[engine]}; write_allowed = tx_ready[engine] && !restart[engine]; end
            7'h07: begin read_data = rx_head[engine]; read_allowed = rx_valid[engine] && !restart[engine]; end
            7'h08: begin read_data = ownership[engine]; write_allowed = !run_request[engine]; end
            7'h09: begin read_data = open_drain[engine]; write_allowed = !run_request[engine]; end
            7'h0a: begin read_data = divider[engine][7:0]; write_allowed = !run_request[engine]; end
            7'h0b: begin read_data = divider[engine][15:8]; write_allowed = !run_request[engine]; end
            7'h0c: begin read_data = timeout_limit[engine][7:0]; write_allowed = !run_request[engine]; end
            7'h0d: begin read_data = timeout_limit[engine][15:8]; write_allowed = !run_request[engine]; end
            7'h0e: read_data = {2'b0,pc[engine]};
            7'h0f: read_data = {4'b0,fault_code[engine]};
            7'h10: read_data = capture[engine][7:0];
            7'h11: read_data = capture_snapshot[engine][7:0];
            7'h12: read_data = capture_snapshot[engine][15:8];
            7'h13: read_data = cycle_count[engine][7:0];
            7'h14: read_data = cycle_high_snapshot[engine];
            7'h15: read_data = debug_x[engine];
            7'h16: read_data = debug_y[engine];
            7'h17: read_data = debug_shift[engine];
            7'h18: read_data = {2'b0,fault_pc[engine]};
            7'h19: read_data = fault_time[engine][7:0];
            7'h1a: read_data = fault_time[engine][15:8];
            7'h1b: read_data = {5'b0,rx_level[engine]};
            7'h7e: begin read_data = {6'b0,run_request}; write_allowed = 1; end
            7'h7f: read_data = {2'b0,host_error,ownership_error,events,active};
            default: begin read_data = 0; read_allowed = 0; end
        endcase
    end

    integer i;
    always @(posedge clk) begin
        if (!rst_n) begin
            request_meta <= 0; request_sync <= 0;
            pin_meta <= 0; pin_sync <= 0;
            acknowledgment <= 0; reply <= 0; write_low <= 0;
            host_address <= 0; read_snapshot_high <= 0;
            write_low_valid <= 0; read_snapshot_valid <= 0;
            host_error <= 0; ownership_error <= 0;
            run_request <= 0; restart <= 0; events <= 0;
            for (i=0; i<2; i=i+1) begin
                ownership[i] <= 0; open_drain[i] <= 0;
                divider[i] <= 0; timeout_limit[i] <= 0;
                program_address[i] <= 0; program_stage[i] <= 0;
                program_stage_valid[i] <= 0; program_valid[i] <= 0;
                capture_snapshot[i] <= 0; cycle_high_snapshot[i] <= 0;
            end
        end else begin
            request_meta <= ui_in[7]; request_sync <= request_meta;
            pin_meta <= uio_in; pin_sync <= pin_meta;
            restart <= 0;
            if (ena) begin
                events <= (events & ~(event_clear[0] | event_clear[1])) | event_set[0] | event_set[1];
                if (transaction) begin
                    acknowledgment <= request_sync;
                    case (command)
                        0: begin host_address[3:0] <= ui_in[3:0]; write_low_valid <= 0; read_snapshot_valid <= 0; end
                        1: begin host_address[7:4] <= ui_in[3:0]; write_low_valid <= 0; read_snapshot_valid <= 0; end
                        2: begin write_low <= ui_in[3:0]; write_low_valid <= 1; end
                        3: begin
                            write_low_valid <= 0;
                            if (!write_low_valid || !write_allowed) host_error <= 1;
                        end
                        4: begin
                            read_snapshot_high <= read_data[7:4]; reply <= read_data[3:0]; read_snapshot_valid <= 1;
                            if (!read_allowed) host_error <= 1;
                            if (address == 7'h10) capture_snapshot[engine] <= capture[engine][23:8];
                            if (address == 7'h13) cycle_high_snapshot[engine] <= cycle_count[engine][15:8];
                        end
                        5: begin
                            reply <= read_snapshot_high;
                            if (!read_snapshot_valid) host_error <= 1;
                        end
                        default: host_error <= 1;
                    endcase
                end
                if (write_commit) begin
                    case (address)
                        7'h00: begin
                            run_request[engine] <= write_data[0];
                            restart[engine] <= write_data[1];
                            if (write_data[2]) begin host_error <= 0; ownership_error <= 0; end
                        end
                        7'h02: begin program_address[engine] <= write_data[5:0]; program_stage_valid[engine] <= 0; end
                        7'h03: begin program_stage[engine][7:0] <= write_data; program_stage_valid[engine][0] <= 1; end
                        7'h04: begin program_stage[engine][15:8] <= write_data; program_stage_valid[engine][1] <= 1; end
                        7'h05: begin
                            program_valid[engine][program_address[engine]] <= 1;
                            program_address[engine] <= program_address[engine] + 1'b1;
                            program_stage_valid[engine] <= 0;
                        end
                        7'h08: ownership[engine] <= write_data;
                        7'h09: open_drain[engine] <= write_data;
                        7'h0a: divider[engine][7:0] <= write_data;
                        7'h0b: divider[engine][15:8] <= write_data;
                        7'h0c: timeout_limit[engine][7:0] <= write_data;
                        7'h0d: timeout_limit[engine][15:8] <= write_data;
                        7'h7e: begin
                            run_request <= write_data[1:0]; restart <= write_data[5:4];
                            if (write_data[6]) events <= 0;
                            if (write_data[7]) begin host_error <= 0; ownership_error <= 0; end
                        end
                        default: begin end
                    endcase
                end
                if (collision) ownership_error <= 1;
            end
        end
    end
endmodule
`default_nettype wire
