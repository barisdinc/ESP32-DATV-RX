"""Stream cu8 I/Q from an rtl_tcp server to stdout: rtltcp_cat.py FREQ_HZ [GAIN_DB]."""
import socket, struct, sys

freq = float(sys.argv[1]); gain = float(sys.argv[2]) if len(sys.argv) > 2 else 0
s = socket.create_connection(("127.0.0.1", 1234))
s.recv(12)
for cmd, val in ((0x02, 333333), (0x01, int(freq)), (0x03, 1), (0x08, 0), (0x04, int(gain * 10))):
    s.sendall(struct.pack(">BI", cmd, val))
out = sys.stdout.buffer
try:
    while d := s.recv(65536):
        out.write(d); out.flush()
except (BrokenPipeError, KeyboardInterrupt):
    pass
