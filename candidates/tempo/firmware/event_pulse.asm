# Wait/consume event0, emit nine-tick pulse on pin7, halt and release
SET pins, 0
SET dirs, 128
EVENT wait0
SET setpins, 128 @ 8
HALT
