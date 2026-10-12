#!/usr/bin/env python3
"""Offline test of the planned wide mode: does leandvb still decode when the I/Q stream is
resampled to fewer samples per symbol and packed to 4 bits with a per-packet scale?

    sim_wide.py REF.cf32 --fs-in 333333 --fs-out 187500 --bits 4 --scale block --esn0 8

REF.cf32 is a recording (python -m espdr.nb run ... --convert cf32) of a 125 kS/s transmission.
Scaled to the 333 kS/s transmitter, --fs-out 187500 is 1.5 samples per symbol (500 ksps) and
166667 is 1.33 (667 ksps for 500 kS/s). Prints the decoded frame counts.
"""
import argparse
import os
import re
import subprocess
import sys
from fractions import Fraction

import numpy as np
from scipy import signal

HERE = os.path.dirname(os.path.abspath(__file__))


def resample(x, fs_in, fs_out, flat=0.8):
    """Polyphase resampler; the passband is flat to `flat` x the output Nyquist, like the chip's FIR."""
    if fs_out == fs_in:
        return x
    r = Fraction(fs_out / fs_in).limit_denominator(64)
    up, down = r.numerator, r.denominator
    nyq_out = fs_out / 2
    fs_hi = fs_in * up
    h = signal.remez(24 * max(up, down) + 1, [0, flat * nyq_out, nyq_out * (2 - flat), fs_hi / 2], [up, 0], fs=fs_hi)
    return signal.resample_poly(x, up, down, window=h).astype(np.complex64)


def add_noise(x, fs, sr, esn0_db, rng):
    """Add white noise so that Es/N0 (on top of the recording's own noise) is about esn0_db."""
    ps = np.mean(np.abs(x) ** 2)
    n0 = ps / sr / 10 ** (esn0_db / 10)          # noise density for that Es/N0
    sigma = np.sqrt(n0 * fs / 2)
    return (x + sigma * (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))).astype(np.complex64)


def quantize(x, bits, scale, block, target):
    """Signed `bits`-bit I/Q. scale='fixed': one scale for the whole file (no AGC);
    'block': a power-of-two scale per packet of `block` samples, chosen so the packet's RMS is
    at most `target` LSB (the firmware would send the shift in the packet header);
    'fine': a per-packet gain in 0.75 dB steps that brings the RMS to about `target` LSB."""
    if bits >= 16:
        return x
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    n = len(x) // block * block
    xb = x[:n].reshape(-1, block)
    if scale == "fixed":
        rms = np.sqrt(np.mean(np.abs(x) ** 2) / 2)
        s = np.full((xb.shape[0], 1), target / rms)
    else:
        rms = np.sqrt(np.mean(np.abs(xb) ** 2, axis=1, keepdims=True) / 2) + 1e-12
        s = target / rms
        if scale == "block":
            s = 2.0 ** np.floor(np.log2(s))            # a shift: power of two, never above target
        else:
            s = 2.0 ** (np.round(np.log2(s) * 8) / 8)  # "fine": 8 steps per octave (0.75 dB)
    q = lambda v: np.clip(np.round(v * s - 0.5) + 0.5, lo + 0.5, hi + 0.5)  # mid-rise levels
    clip = np.mean((np.abs(xb.real * s) > hi + 1) | (np.abs(xb.imag * s) > hi + 1))
    y = (q(xb.real) + 1j * q(xb.imag)) / s
    return y.reshape(-1).astype(np.complex64), clip


def decode(x, fs, args):
    leandvb = args.leandvb or os.path.join(HERE, "..", "build", "leansdr", "src", "apps", "leandvb")
    x = np.concatenate([x] * args.repeat)  # the first pass includes the lock time
    data = (x / np.sqrt(np.mean(np.abs(x) ** 2)) * 0.25).astype(np.complex64).tobytes()
    cmd = [leandvb, "--f32", "--float-scale", "1", "-f", str(int(fs)), "--sr", str(int(args.sr)),
           "--standard", "DVB-S2", "--const", "QPSK", "--cr", "3/4", "--ldpc-bf", "100", "--fastdrift", "-d"]
    cmd += args.extra.split() if args.extra else []
    p = subprocess.run(cmd, input=data, capture_output=True, timeout=600)
    err = p.stderr.decode(errors="ignore")
    return err.count("_"), err.count("!"), err.count("LOCKED"), len(p.stdout) // 188


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ref")
    ap.add_argument("--fs-in", type=float, default=333333)
    ap.add_argument("--fs-out", type=float, default=333333)
    ap.add_argument("--sr", type=float, default=125000)
    ap.add_argument("--bits", type=int, default=32)
    ap.add_argument("--scale", choices=["fixed", "block", "fine"], default="block")
    ap.add_argument("--block", type=int, default=256, help="samples per packet (default 256)")
    ap.add_argument("--target", type=float, default=2.5, help="packet RMS in LSB (default 2.5)")
    ap.add_argument("--esn0", type=float, help="add noise down to this Es/N0 in dB")
    ap.add_argument("--fade", type=float, default=0, help="multiply by a slow 0.5 Hz fade of this depth in dB")
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--leandvb")
    ap.add_argument("--extra", help="more leandvb options")
    args = ap.parse_args()

    rng = np.random.default_rng(1)
    x = np.fromfile(args.ref, np.complex64)
    x = x - x.mean()
    if args.esn0 is not None:
        x = add_noise(x, args.fs_in, args.sr, args.esn0, rng)
    if args.fade:
        t = np.arange(len(x)) / args.fs_in
        x = (x * 10 ** (-args.fade / 20 * (0.5 - 0.5 * np.cos(2 * np.pi * 0.5 * t)))).astype(np.complex64)
    x = resample(x, args.fs_in, args.fs_out)
    clip = 0.0
    if args.bits < 16:
        x, clip = quantize(x, args.bits, args.scale, args.block, args.target)
    good, bad, locks, ts = decode(x, args.fs_out, args)
    total = good + bad
    label = (f"fs {args.fs_out / 1e3:6.1f}k ({args.fs_out / args.sr:.2f} sps) bits {args.bits:2d} {args.scale:5s} "
             f"Es/N0 {'-' if args.esn0 is None else args.esn0:>4} fade {args.fade:g} dB")
    print(f"{label}: frames ok {good:3d} bad {bad:3d} locks {locks} TS {ts:5d} clip {100 * clip:.2f}%", flush=True)


if __name__ == "__main__":
    main()
