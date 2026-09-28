# Mode0 MSB-first full duplex, one CS pulse per byte, 18 ticks/bit; TX in / RX out
SET pins, 8
SET dirs, 11
next_byte:
PULL
SET x, 7
SET clearpins, 8 @ 3
bit:
OUT msb0 @ 3
SET setpins, 2 @ 5
IN left2, 1 @ 2
SET clearpins, 2 @ 3
JMP xnz, bit
SET setpins, 8 @ 3
PUSH
JMP always, next_byte
