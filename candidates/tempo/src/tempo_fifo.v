`default_nettype none
// Four-entry byte FIFO. No empty bypass; a simultaneous pop makes full writable.
module tempo_fifo (
    input wire clk, input wire rst_n, input wire flush,
    input wire write_en, input wire [7:0] write_data,
    output wire write_ready,
    input wire read_en, output wire [7:0] read_data,
    output wire read_valid, output reg [2:0] level
);
    reg [7:0] memory [0:3];
    reg [1:0] read_pointer, write_pointer;
    wire pop = read_en && read_valid;
    wire push = write_en && write_ready;
    assign read_valid = (level != 0);
    assign write_ready = (level != 4) || pop;
    assign read_data = read_valid ? memory[read_pointer] : 8'b0;
    always @(posedge clk) begin
        if (!rst_n) begin
            level <= 0;
            read_pointer <= 0;
            write_pointer <= 0;
        end else if (flush) begin
            level <= 0;
            read_pointer <= 0;
            write_pointer <= 0;
        end else begin
            if (push) begin
                memory[write_pointer] <= write_data;
                write_pointer <= write_pointer + 1'b1;
            end
            if (pop) read_pointer <= read_pointer + 1'b1;
            case ({push, pop})
                2'b10: level <= level + 1'b1;
                2'b01: level <= level - 1'b1;
                default: level <= level;
            endcase
        end
    end
endmodule
`default_nettype wire
