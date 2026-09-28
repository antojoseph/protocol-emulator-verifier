# 8N1 receiver, 16 ticks/bit; samples 1.5 bits after synchronized start; bad stop sets event1 and halts
next_byte:
SET x, 7
WAIT low0 @ 23
bit:
IN right0 @ 14
JMP xnz, bit
ALU to_y
SET sr, 0
IN left0
JMP zero, framing_error
ALU from_y
PUSH
JMP always, next_byte
framing_error:
EVENT set1
HALT
