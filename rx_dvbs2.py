#!/usr/bin/env python3
"""rx_dvbs2.py: receive a DVB-S2 transmission with an ESP32-S3 and show it.

    ESP32-S3 (espdr-rx, rtl_tcp, 333 ksps) -> leandvb (DVB-S2) -> ffmpeg (retime) -> ffplay
    ESP32-S3 (--wide: espdr.nb, packed 4-bit I/Q, 500 ksps) -> leandvb -> ffmpeg -> ffplay

It starts espdr-rx when no rtl_tcp server is listening (espdr-rx loads the receiver firmware into the
board's RAM when needed), looks for the signal near the given frequency, tunes to its centre and plays
the transport stream. Ctrl-C or closing the video window ends it.

--wide receives up to about 333 kS/s. It needs the receiver firmware with the cs4 format (esp32-sdr-trx
1.0.0rc4+cs4, installed by tools/install.sh); 333 ksps int8 through rtl_tcp is limited to about 200 kS/s.
"""
import argparse
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

try:
    import numpy as np
except ImportError:  # main() re-executes inside .venv
    np = None


def log(*a):
    print(*a, file=sys.stderr, flush=True)

HERE = os.path.dirname(os.path.abspath(__file__))
FS = 333333  # espdr-rx --decim 3
FS_WIDE = 500000  # espdr.nb --decim 2 --format cs4

# DVB-S2 MODCOD numbers (EN 302 307-1, table 12), for leandvb's --modcods filter.
MODCODS = {("QPSK", r): i + 1 for i, r in enumerate(("1/4", "1/3", "2/5", "1/2", "3/5", "2/3", "3/4", "4/5", "5/6", "8/9", "9/10"))}
MODCODS.update({("8PSK", r): i + 12 for i, r in enumerate(("3/5", "2/3", "3/4", "5/6", "8/9", "9/10"))})
MODCODS.update({("16APSK", r): i + 18 for i, r in enumerate(("2/3", "3/4", "4/5", "5/6", "8/9", "9/10"))})
MODCODS.update({("32APSK", r): i + 24 for i, r in enumerate(("3/4", "4/5", "5/6", "8/9", "9/10"))})


def find_tool(name, *candidates):
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return shutil.which(name)


class RtlTcp:
    """Minimal rtl_tcp client for espdr-rx."""

    def __init__(self, host, port):
        self.s = socket.create_connection((host, port), timeout=10)
        hdr = self._recv_exact(12)
        if hdr[:4] != b"RTL0":
            raise RuntimeError("not an rtl_tcp server")

    def _recv_exact(self, n):
        b = bytearray()
        while len(b) < n:
            d = self.s.recv(n - len(b))
            if not d:
                raise EOFError("rtl_tcp server closed the connection")
            b += d
        return bytes(b)

    def cmd(self, c, v):
        self.s.sendall(struct.pack(">BI", c, int(v) & 0xFFFFFFFF))

    def setup(self, freq, gain_db):
        self.cmd(0x02, FS)
        self.cmd(0x01, round(freq))
        self.cmd(0x03, 1)  # manual gain
        self.cmd(0x08, 0)  # AGC off
        self.cmd(0x04, round(gain_db * 10))

    def read(self, n):
        return self._recv_exact(n)

    def close(self):
        self.s.close()


class NarrowSource:
    """espdr-rx's rtl_tcp server: 333 ksps cu8."""

    fs = FS

    def __init__(self, args):
        self.args = args

    def grab(self, freq, seconds):
        rx = RtlTcp(self.args.host, self.args.port)
        rx.setup(freq, self.args.gain)
        rx.read(int(0.3 * FS) * 2)  # retune settling
        b = np.frombuffer(rx.read(int(seconds * FS) * 2), np.uint8).astype(np.float32) - 127.5
        rx.close()
        return b[0::2] + 1j * b[1::2]

    def leandvb_input(self):
        return ["--u8", "-f", str(FS)]

    def start(self, freq, sink):
        """Streams to sink.write() from a thread until the server closes or sink fails."""
        def pump():
            rx = RtlTcp(self.args.host, self.args.port)
            rx.setup(freq, self.args.gain)
            try:
                while d := rx.s.recv(65536):
                    sink.write(d)
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                rx.close()
        threading.Thread(target=pump, daemon=True).start()

    def stop(self):
        pass


class WideSource:
    """espdr.nb directly: 500 ksps packed 4-bit I/Q with a gain per packet, delivered as cf32."""

    fs = FS_WIDE

    def __init__(self, args):
        self.args = args
        self.proc = None
        try:
            from espdr import board, nb
        except ImportError:
            sys.exit("espdr not found: run tools/install.sh first (or activate the venv)")
        if "cs4" not in nb.FORMATS:
            sys.exit("the installed espdr has no cs4 format: run tools/install.sh again (it installs esp32-sdr-trx 1.0.0rc4+cs4)")
        self.port = board.ensure(board.RX, log=log)  # loads the receiver firmware into RAM if needed
        link = nb.open_link(self.port)
        status, _ = link.command(nb.NB_SET_FORMAT, nb.FORMATS["cs4"], allow=(0, 2))
        link.ser.close()
        if status != 0:
            log("the running firmware has no cs4 format; loading the one of the installed package")
            self.port = board.ensure(board.RX, load="always", log=log)
        # espdr-rx maps 0..49.6 dB to the gain selector 30..80
        self.selector = round(30 + max(0.0, min(49.6, args.gain)) * 50 / 49.6)

    def _cmd(self, freq, seconds):
        return [sys.executable, "-m", "espdr.nb", "-p", self.port, "run", "--freq", str(round(freq)), "--decim", "2",
                "--format", "cs4", "--gain", str(self.selector), "--seconds", str(seconds), "--convert", "cf32", "-o", "-"]

    def grab(self, freq, seconds):
        p = subprocess.run(self._cmd(freq, int(seconds) + 1), capture_output=True, timeout=30)
        if p.returncode:
            sys.exit("espdr.nb failed:\n" + p.stderr.decode(errors="ignore"))
        x = np.frombuffer(p.stdout, np.complex64)
        return x[int(0.3 * FS_WIDE):]

    def leandvb_input(self):
        # cf32 in full-scale units (about 0.02 RMS at the usual gain); leandvb's AGC does the rest
        return ["--f32", "--float-scale", "512", "-f", str(FS_WIDE)]

    def start(self, freq, sink):
        self.proc = subprocess.Popen(self._cmd(freq, 0), stdout=subprocess.PIPE,
                                     stderr=None if self.args.verbose else subprocess.DEVNULL)

        def pump():
            try:
                while d := self.proc.stdout.read1(65536):
                    sink.write(d)
            except (BrokenPipeError, OSError, ValueError):
                pass
        threading.Thread(target=pump, daemon=True).start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)  # espdr.nb stops the ESP's run cleanly on SIGINT
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def server_up(host, port):
    try:
        socket.create_connection((host, port), timeout=1).close()
        return True
    except OSError:
        return False


def start_espdr(args):
    espdr = find_tool("espdr-rx", os.path.join(HERE, ".venv", "bin", "espdr-rx"))
    if not espdr:
        sys.exit("espdr-rx not found: run tools/install.sh first (or activate the venv)")
    cmd = [espdr, "--decim", "3", "--listen", f"{args.host}:{args.port}"]
    if args.ppm:
        cmd += ["--ppm", str(args.ppm)]
    log("starting:", " ".join(cmd))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    t0 = time.time()
    while time.time() - t0 < 60:
        if p.poll() is not None:
            sys.exit("espdr-rx ended:\n" + p.stdout.read())
        if server_up(args.host, args.port):
            threading.Thread(target=lambda: [None for _ in p.stdout], daemon=True).start()  # drain its output
            return p
        time.sleep(0.5)
    p.terminate()
    sys.exit("espdr-rx did not start within 60 s")


def search(src, freq):
    """Find the signal near freq; return its centre frequency in Hz."""
    fs = src.fs
    x = src.grab(freq, 1.0)
    n = 1024
    seg = len(x) // n
    p = np.mean(np.abs(np.fft.fftshift(np.fft.fft(x[: seg * n].reshape(seg, n) * np.hanning(n), axis=1), axes=1)) ** 2, 0)
    p = np.convolve(10 * np.log10(p + 1e-9), np.ones(9) / 9, "same")
    f = (np.arange(n) - n / 2) * fs / n
    usable = np.abs(f) < 0.45 * fs
    floor = np.percentile(p[usable], 10)
    peak = np.percentile(p[usable], 95)
    if peak - floor < 6:
        log(f"search: no signal near {freq / 1e6:.4f} MHz (only {peak - floor:.1f} dB above the floor)")
        return None
    occ = usable & (p > peak - 3)
    lo, hi = f[occ].min(), f[occ].max()
    centre = freq + (lo + hi) / 2
    log(f"search: signal {peak - floor:.0f} dB above the floor, -3 dB width {(hi - lo) / 1e3:.0f} kHz, "
          f"centre {centre / 1e6:.4f} MHz ({(centre - freq) / 1e3:+.1f} kHz)")
    if hi - lo > 0.8 * fs:
        log(f"search: the signal is wider than the receiver ({fs / 1e3:.0f} ksps)"
            + ("" if src.fs == FS_WIDE else ": use --wide, or set the transmitter to 125 kS/s"))
    return centre


def main():
    # Run inside the project's venv (numpy, espdr) when started as ./rx_dvbs2.py with the system Python.
    venv_python = os.path.join(HERE, ".venv", "bin", "python")
    if os.path.exists(venv_python) and os.path.realpath(sys.prefix) != os.path.realpath(os.path.join(HERE, ".venv")):
        os.execv(venv_python, [venv_python, os.path.abspath(__file__)] + sys.argv[1:])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--freq", type=float, default=2300.5, help="transmitter frequency in MHz (default 2300.5)")
    ap.add_argument("--sr", type=float, default=125e3,
                    help="symbol rate in S/s (default 125000; at most about 200000, with --wide about 333000)")
    ap.add_argument("--wide", action="store_true", help="500 ksps 4-bit mode for symbol rates above 200 kS/s")
    ap.add_argument("--cr", default="3/4", help="code rate (default 3/4)")
    ap.add_argument("--const", default="QPSK", help="constellation (default QPSK)")
    ap.add_argument("--gain", type=float, default=10, help="receiver gain in dB, 0..49.6 (default 10)")
    ap.add_argument("--ppm", type=float, default=0, help="crystal correction passed to espdr-rx")
    ap.add_argument("--no-search", action="store_true", help="tune exactly to --freq, do not look for the signal")
    ap.add_argument("--record", metavar="FILE.ts", help="also write the transport stream to this file")
    ap.add_argument("--no-play", action="store_true", help="do not start ffplay (use with --record or --stdout)")
    ap.add_argument("--stdout", action="store_true", help="write the transport stream to stdout")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1234)
    ap.add_argument("--leandvb", help="path of leandvb (default: build/leansdr/src/apps/leandvb, then PATH)")
    ap.add_argument("--ldpc", choices=("auto", "tool", "bitflip"), default="auto",
                    help="LDPC decoder: ldpc_tool (soft decision, about 5 dB better, 32-frame latency) or leandvb's "
                         "bit-flip one; auto takes ldpc_tool when it is installed")
    ap.add_argument("-v", "--verbose", action="store_true", help="show leandvb's messages")
    args = ap.parse_args()

    leandvb = find_tool("leandvb", args.leandvb, os.path.join(HERE, "build", "leansdr", "src", "apps", "leandvb"))
    ldpc_tool = None if args.ldpc == "bitflip" else find_tool("ldpc_tool", os.path.join(HERE, "build", "LDPC", "ldpc_tool"))
    if args.ldpc == "tool" and not ldpc_tool:
        sys.exit("ldpc_tool not found: run tools/install.sh first")
    modcod = MODCODS.get((args.const.upper(), args.cr))
    if modcod is None:
        sys.exit(f"unknown DVB-S2 modulation {args.const} {args.cr}")
    if not leandvb:
        sys.exit("leandvb not found: run tools/install.sh first, or give --leandvb")
    if not args.no_play and not (shutil.which("ffmpeg") and shutil.which("ffplay")):
        sys.exit("ffmpeg/ffplay not found: sudo apt install ffmpeg")

    espdr = None
    if args.wide:
        if server_up(args.host, args.port):
            sys.exit("an rtl_tcp server (espdr-rx) is running and holds the board: stop it for --wide")
        src = WideSource(args)
    else:
        espdr = None if server_up(args.host, args.port) else start_espdr(args)
        src = NarrowSource(args)
    procs = []
    try:
        freq = args.freq * 1e6
        if not args.no_search:
            # A signal far off centre is cut by the +-166 kHz window: retune to the estimate and look again.
            for _ in range(3):
                found = search(src, freq)
                if found is None:
                    break
                step, freq = found - freq, found
                if abs(step) < 5e3:
                    break

        dvb = subprocess.Popen([leandvb] + src.leandvb_input() + ["--sr", str(int(args.sr)), "--standard", "DVB-S2",
                                "--const", args.const, "--cr", args.cr, "--fastdrift", "-d",
                                # only our MODCOD and normal frames: a corrupted PLS header would otherwise start an
                                # ldpc_tool for a random MODCOD, which then waits for its batch of 32 frames forever
                                "--modcods", str(1 << modcod), "--framesizes", "1"]
                               + (["--ldpc-helper", ldpc_tool] if ldpc_tool else ["--ldpc-bf", "100"]),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        procs.append(dvb)
        sinks = []
        if not args.no_play:
            ffmpeg = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "fatal", "-probesize", "64000",
                                       "-analyzeduration", "3000000", "-fflags", "+igndts+genpts", "-i", "-",
                                       "-vf", "setpts=N/25/TB", "-an", "-c:v", "rawvideo", "-pix_fmt", "yuv420p",
                                       "-f", "nut", "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            # SDL's GLX path fails on some X11 setups; EGL works there and elsewhere.
            env = dict(os.environ, SDL_VIDEO_X11_FORCE_EGL=os.environ.get("SDL_VIDEO_X11_FORCE_EGL", "1"))
            ffplay = subprocess.Popen(["ffplay", "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer",
                                       "-window_title", f"DVB-S2 {freq / 1e6:.4f} MHz (ESP32-S3 RX)", "-i", "-"],
                                      stdin=ffmpeg.stdout, env=env)
            ffmpeg.stdout.close()
            procs += [ffmpeg, ffplay]
            sinks.append(ffmpeg.stdin)
        if args.record:
            sinks.append(open(args.record, "wb"))
        if args.stdout:
            sinks.append(sys.stdout.buffer)

        stats = {"good": 0, "bad": 0, "locks": 0, "ts": 0}

        def pump_ts():
            while d := dvb.stdout.read1(65536):
                stats["ts"] += len(d) // 188
                for s in list(sinks):
                    try:
                        s.write(d)
                        s.flush()
                    except (BrokenPipeError, OSError):
                        sinks.remove(s)

        def pump_log():
            # leandvb -d prints one character per frame ('_' good, '!' with errors) without a newline.
            while d := dvb.stderr.read1(4096):
                t = d.decode(errors="ignore")
                stats["good"] += t.count("_")
                stats["bad"] += t.count("!")
                stats["locks"] += t.count("LOCKED")
                if args.verbose:
                    sys.stderr.write(t)

        src.start(freq, dvb.stdin)
        for fn in (pump_ts, pump_log):
            threading.Thread(target=fn, daemon=True).start()

        log(f"receiving {freq / 1e6:.4f} MHz, {args.sr / 1e3:g} kS/s {args.const} {args.cr}, gain {args.gain:g} dB, "
            f"LDPC {'ldpc_tool' if ldpc_tool else 'bit-flip'}. Ctrl-C ends.")
        if ldpc_tool:
            log(f"ldpc_tool decodes 32 frames at a time: the first picture comes after about "
                f"{32 * 33282 / args.sr:.0f} s")
        last = None
        while dvb.poll() is None and (args.no_play or procs[-1].poll() is None):
            time.sleep(5)
            now = (stats["good"], stats["bad"], stats["locks"], stats["ts"])
            if now != last:
                log(f"frames ok {stats['good']}, with errors {stats['bad']}, locks {stats['locks']}, "
                    f"TS packets {stats['ts']}")
                last = now
    except KeyboardInterrupt:
        pass
    finally:
        for p in reversed(procs):
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()
        src.stop()
        for p in procs:
            for f in (p.stdin, p.stdout):
                try:
                    if f:
                        f.close()
                except OSError:  # its reader or writer is gone already
                    pass
        if espdr:
            espdr.terminate()
            espdr.wait(10)


if __name__ == "__main__":
    main()
