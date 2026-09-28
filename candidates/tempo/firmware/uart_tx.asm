# 8N1, LSB first; 16 core ticks/bit; TX FIFO stream
SET pins, 1
SET dirs, 1
next_byte:
PULL
SET x, 7
SET pins, 0 @ 15
bit:
OUT lsb0 @ 14
JMP xnz, bit
SET pins, 1 @ 15
JMP always, next_byte
