# Single-controller write: TX count-minus-one then address/data bytes; RX one ACK bit/byte (0 ACK, 1 NACK); aborts on NACK; START/STOP and stretch-aware SCL
SET pins, 3
SET dirs, 3
PULL
ALU to_x
WAIT high1 @ 7
WAIT high0
SET clearpins, 1 @ 7
SET clearpins, 2 @ 7
next_byte:
PULL
SET y, 7
bit:
OUT msb0 @ 7
SET setpins, 2
WAIT high1 @ 7
SET clearpins, 2 @ 7
JMP ynz, bit
SET setpins, 1 @ 7
SET setpins, 2
WAIT high1 @ 3
SET sr, 0
IN left0 @ 3
SET clearpins, 2 @ 7
PUSH
JMP nonzero, stop
JMP xnz, next_byte
stop:
SET clearpins, 1 @ 7
SET setpins, 2
WAIT high1 @ 7
SET setpins, 1 @ 7
HALT
