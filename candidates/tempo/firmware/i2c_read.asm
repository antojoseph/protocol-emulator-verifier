# Single-controller read: TX read-count-minus-one then address byte with R/W=1; RX address ACK followed by data; controller ACKs all except final byte (NACK), aborts address NACK
SET pins, 3
SET dirs, 3
PULL
ALU to_x
PULL
SET y, 7
WAIT high1 @ 7
WAIT high0
SET clearpins, 1 @ 7
SET clearpins, 2 @ 7
address_bit:
OUT msb0 @ 7
SET setpins, 2
WAIT high1 @ 7
SET clearpins, 2 @ 7
JMP ynz, address_bit
SET setpins, 1 @ 7
SET setpins, 2
WAIT high1 @ 3
SET sr, 0
IN left0 @ 3
SET clearpins, 2 @ 7
PUSH
JMP nonzero, stop
next_byte:
SET sr, 0
SET y, 7
read_bit:
SET setpins, 2
WAIT high1 @ 7
IN left0 @ 3
SET clearpins, 2 @ 7
JMP ynz, read_bit
PUSH
JMP xzero, last_byte
SET clearpins, 1
JMP always, acknowledge
last_byte:
SET setpins, 1
acknowledge:
NOP @ 7
SET setpins, 2
WAIT high1 @ 7
SET clearpins, 2 @ 7
SET setpins, 1
JMP xnz, next_byte
stop:
SET clearpins, 1 @ 7
SET setpins, 2
WAIT high1 @ 7
SET setpins, 1 @ 7
HALT
