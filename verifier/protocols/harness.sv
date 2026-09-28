`timescale 1ns/1ps
// Organizer-owned external-pin peers. The candidate contributes no testbench code.
module challenge_tb;
    reg clk = 0;
    always #10 clk = ~clk;
    reg rst_n = 0, ena = 1;
    reg [7:0] ui_in = 0;
    wire [7:0] uo_out, uio_out, uio_oe;
    tri [7:0] pads;
    reg [7:0] peer_enable = 0, peer_value = 255;
    reg [7:0] captured [0:15];
    integer checks = 0, cycles = 0;
    integer i2c_starts = 0, i2c_stops = 0, i2c_rises = 0;
    integer i2c_low_at = 0, i2c_high_at = 0;
    integer i2c_sda_at = 0, i2c_start_at = 0, i2c_previous_rise = 0;
    integer spi_rises = 0;
    integer stage_started = 0, stage_ended = 0, case_number = 0;
    integer case_started = 0;
    reg i2c_monitor = 0;
    reg spi_monitor = 0;
    reg programmable_quiet = 0;
    reg [7:0] input_only = 0;
    genvar p;
    generate for (p = 0; p < 8; p = p + 1) begin : wires
        assign pads[p] = uio_oe[p] ? uio_out[p] : 1'bz;
        assign pads[p] = peer_enable[p] ? peer_value[p] : 1'bz;
        pullup(pads[p]);
    end endgenerate
    @TOP@ dut (
        .ui_in(ui_in), .uo_out(uo_out), .uio_in(pads),
        .uio_out(uio_out), .uio_oe(uio_oe), .ena(ena),
        .clk(clk), .rst_n(rst_n)
    );
    always @(posedge clk) begin
        cycles = cycles + 1;
        if(cycles-case_started>200000)
            $fatal(1, "FAIL case=%0d bounded per-case watchdog expired", case_number);
        if (rst_n) begin
            if ((uio_oe & input_only) !== 0)
                $fatal(1, "FAIL case=%0d candidate drove an input-only protocol pin", case_number);
            if (i2c_monitor && ((uio_oe & uio_out & 3) !== 0))
                $fatal(1, "FAIL case=%0d I2C actively drove a high level", case_number);
        end
    end
    always @(pads[0]) if (i2c_monitor && pads[1] === 1) begin
        if (pads[0] === 0) begin
            i2c_starts = i2c_starts + 1; i2c_start_at=cycles;
        end else if (pads[0] === 1) begin
            if(cycles-i2c_high_at<200) $fatal(1, "FAIL I2C STOP setup below 4.0us");
            i2c_stops = i2c_stops + 1;
        end
        else $fatal(1, "FAIL I2C unknown/contended SDA");
    end
    always @(pads[0]) if(i2c_monitor && pads[1] === 0) i2c_sda_at=cycles;
    always @(uio_oe or uio_out) if(i2c_monitor && ((uio_oe & uio_out & 3) !== 0))
        $fatal(1, "FAIL I2C controller high drive between clock samples");
    always @(posedge pads[1]) if (i2c_monitor) begin
        i2c_rises = i2c_rises + 1;
        // Standard-mode minimum low time: 4.7 us at the fixed 50 MHz clock.
        if (cycles - i2c_low_at < 235)
            $fatal(1, "FAIL I2C tLOW below 4.7us");
        if(cycles-i2c_sda_at<13) $fatal(1, "FAIL I2C data setup below 250ns");
        if(i2c_previous_rise>0 && cycles-i2c_previous_rise<500)
            $fatal(1, "FAIL I2C frequency exceeds Standard-mode 100kHz");
        i2c_previous_rise=cycles;
        i2c_high_at = cycles;
    end
    always @(negedge pads[1]) if (i2c_monitor) begin
        // Initial bus idle also counts as high time.
        if (cycles - i2c_high_at < 200)
            $fatal(1, "FAIL I2C tHIGH below 4.0us");
        if(i2c_rises==0 && cycles-i2c_start_at<200)
            $fatal(1, "FAIL I2C START hold below 4.0us");
        i2c_low_at = cycles;
    end
    always @(posedge pads[1]) if(spi_monitor && pads[3] === 0) spi_rises=spi_rises+1;
    always @(pads[0]) if(spi_monitor && pads[3] === 0 && pads[1] === 1)
        $fatal(1, "FAIL SPI MOSI changed while mode0 clock high");
    always @(pads[7]) if(programmable_quiet && pads[7] !== 0)
        $fatal(1, "FAIL unsolicited programmable output outside the triggered pulse train");
    task expect_true(input condition, input [8*160-1:0] message);
        begin
            checks = checks + 1;
            if (condition !== 1'b1)
                $fatal(1, "FAIL case=%0d %0s cycle=%0d", case_number, message, cycles);
        end
    endtask
    task reset_chip;
        integer k;
        begin
            @(negedge clk);
            i2c_monitor = 0; spi_monitor=0; programmable_quiet=0; input_only = 0;
            i2c_starts = 0; i2c_stops = 0; i2c_rises = 0;
            rst_n = 0; ui_in = 0; peer_enable = 0; peer_value = 255;
            for (k=0;k<16;k=k+1) captured[k]=0;
            repeat (8) @(negedge clk);
            expect_true(uio_oe === 0, "reset releases protocol pads");
            rst_n = 1;
            repeat (8) @(negedge clk);
        end
    endtask
    task host_drive(input [7:0] value, input integer clocks);
        begin
            @(negedge clk); ui_in = value;
            repeat (clocks) @(negedge clk);
        end
    endtask
    task host_wait(input [7:0] mask, input [7:0] value, input integer timeout);
        integer k;
        begin
            k = 0;
            while ((uo_out & mask) !== value && k < timeout) begin
                @(posedge clk); #1; k=k+1;
            end
            expect_true((uo_out & mask) === value, "host transaction timed out");
        end
    endtask
    task host_capture(input integer dest, input integer src_lsb,
                      input integer width, input integer dst_lsb);
        reg [7:0] bits;
        begin
            bits = (uo_out >> src_lsb) & ((1 << width)-1);
            expect_true((^bits) !== 1'bx, "host read returned unknown bits");
            captured[dest] = captured[dest] | (bits << dst_lsb);
        end
    endtask
    task uart_receive(input [7:0] expected, input integer period);
        integer frame_at, offset, symbol, bit_offset;
        reg expected_level;
        begin
            @(negedge pads[0]);
            frame_at=cycles; offset=0;
            // Check the full bit windows, not just samples at their centers.
            // Two clock margins tolerate synchronizer/edge placement only.
            while(offset < 10*period-2) begin
                @(negedge clk); offset=cycles-frame_at;
                symbol=offset/period; bit_offset=offset%period;
                if(symbol==0) expected_level=0;
                else if(symbol==9) expected_level=1;
                else expected_level=expected[symbol-1];
                if(bit_offset>=2 && bit_offset<period-2)
                    expect_true(pads[0] === expected_level, "UART complete 8N1 bit-window stability");
            end
        end
    endtask
    task uart_send(input [7:0] value, input integer period_ns);
        integer b;
        begin
            peer_value[0] = 0; #(period_ns);
            for (b=0;b<8;b=b+1) begin
                peer_value[0] = value[b]; #(period_ns);
            end
            peer_value[0] = 1; #(period_ns);
        end
    endtask
    task spi_peer(input [7:0] transmitted, input [7:0] response,
                  input integer period);
        integer b, rise_at, previous_rise;
        begin
            @(negedge pads[3]);
            spi_rises=0; spi_monitor=1;
            expect_true(pads[1] === 0, "SPI CPOL=0 at chip select assertion");
            peer_enable[2] = 1; peer_value[2] = response[7];
            previous_rise=-1;
            for (b=7;b>=0;b=b-1) begin
                @(posedge pads[1]); rise_at=cycles;
                expect_true(pads[3] === 0, "SPI select held across every bit");
                expect_true(pads[0] === transmitted[b], "SPI mode0 MSB-first MOSI data");
                if(previous_rise>=0)
                    expect_true(rise_at-previous_rise >= period && rise_at-previous_rise <= period+2,
                                "SPI requested clock period");
                previous_rise=rise_at;
                @(negedge pads[1]);
                expect_true(cycles-rise_at >= period/2-1 && cycles-rise_at <= period/2+1,
                            "SPI requested high interval");
                if(b>0) peer_value[2]=response[b-1];
            end
            @(posedge pads[3]); peer_enable[2]=0; spi_monitor=0;
            expect_true(spi_rises==8, "SPI chip select contains exactly eight clocks");
            expect_true(pads[1] === 0, "SPI clock idle low on select deassertion");
        end
    endtask
    task i2c_start;
        begin
            @(negedge pads[0]);
            expect_true(pads[1] === 1, "I2C START while SCL high");
            @(negedge pads[1]);
        end
    endtask
    task i2c_receive(input [7:0] expected, input nack,
                     input integer stretch_clocks);
        integer b;
        begin
            // Enter on the low edge following START or the preceding ACK.
            for (b=7;b>=0;b=b-1) begin
                if (b==4 && stretch_clocks>0) begin
                    peer_enable[1]=1; peer_value[1]=0;
                    repeat (stretch_clocks) @(negedge clk);
                    expect_true(pads[1] === 0, "I2C clock stretching physically holds SCL low");
                    peer_enable[1]=0;
                end
                @(posedge pads[1]);
                expect_true(pads[0] === expected[b], "I2C MSB-first address/data");
                @(negedge pads[1]);
            end
            peer_enable[0]=!nack; peer_value[0]=0;
            @(posedge pads[1]);
            expect_true(pads[0] === nack, "I2C target ACK/NACK sampled");
            @(negedge pads[1]); peer_enable[0]=0;
        end
    endtask
    task i2c_send(input [7:0] value, input expected_nack);
        integer b;
        begin
            for (b=7;b>=0;b=b-1) begin
                peer_enable[0]=!value[b]; peer_value[0]=0;
                @(posedge pads[1]);
                expect_true(pads[0] === value[b], "I2C target-driven read data");
                @(negedge pads[1]);
            end
            peer_enable[0]=0;
            @(posedge pads[1]);
            expect_true(pads[0] === expected_nack, "I2C controller ACK continuation/NACK final byte");
            @(negedge pads[1]);
        end
    endtask
    task i2c_stop;
        begin
            @(posedge pads[0]);
            while(pads[1] !== 1) @(posedge pads[0]);
            expect_true(pads[1] === 1, "I2C STOP while SCL high");
        end
    endtask
    task programmable_pulses(input integer count, input integer width,
                             input integer gap, input integer trigger_at);
        integer b, rise_at, last_fall;
        begin
            last_fall=-1;
            for(b=0;b<count;b=b+1) begin
                @(posedge pads[7]); rise_at=cycles;
                if(b==0) expect_true(rise_at-trigger_at>0 && rise_at-trigger_at<=128,
                                    "programmable response follows external trigger promptly");
                else expect_true(rise_at-last_fall==gap, "program-selected pulse gap");
                @(negedge pads[7]);
                expect_true(cycles-rise_at==width, "runtime branch selected programmed pulse width");
                last_fall=cycles;
            end
            programmable_quiet=1;
            repeat (gap+width+8) begin
                @(negedge clk);
                expect_true(pads[7] === 0, "programmed finite loop emits no extra pulses");
            end
        end
    endtask
    task arm_programmable;
        integer watchdog;
        begin
            watchdog=0;
            while(pads[7] !== 0 && watchdog<128) begin
                expect_true(!(uio_oe[7] === 1 && uio_out[7] === 1),
                            "programmable output cannot pulse during initialization");
                @(negedge clk); watchdog=watchdog+1;
            end
            expect_true(pads[7] === 0, "programmable output initializes low within 128 clocks");
            programmable_quiet=1;
            repeat (200) @(negedge clk);
            expect_true(pads[7] === 0, "programmable output waits for input trigger");
        end
    endtask
@HOST_TASKS@
    integer trigger_at;
    initial begin
@SCENARIOS@
        $display("VERIFIER_COMPLETE @NONCE@ cases=@CASES@ stages=@STAGES@ checks=%0d cycles=%0d", checks, cycles);
        $finish;
    end
    initial begin #2000000000; $fatal(1, "FAIL global simulation watchdog expired"); end
endmodule
