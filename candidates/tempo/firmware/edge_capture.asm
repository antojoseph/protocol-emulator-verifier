# Wait for low then rising input; capture synchronized pads/cycle count, broadcast event0
WAIT low0
WAIT high0
CAPTURE
EVENT set0
HALT
