"""Differential test: the native gps_fix status line against the bridge's.

Under ``nativedecode=on`` the capture binary renders the gps_fix status line
itself (``diag_gpsfix.c``). The bridge renders it as
``f"celldiag GPS fix {lat:.6f},{lon:.6f} alt={alt:.0f}m src=0x{code:04X}"``.
Byte equality rests on Python's fixed-point format and glibc's printf both
rounding the exact binary value, half to even. This holds them to it on
random fixes and on the bit-neighbours of every kind of tie, including
half-way altitudes and negative zero. Synthetic: no capture needed.

The C side is ``test_diag_gpsfix --format``, built by ``make check`` here.
"""
from __future__ import annotations

import random
import struct
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
DRIVER = HERE / "test_diag_gpsfix"


def _bridge_line(lat, lon, alt, code):
    # kismet_diag_decode.py _write_gps_fix, verbatim.
    src = f"0x{code:04X}"
    return f"celldiag GPS fix {lat:.6f},{lon:.6f} alt={alt:.0f}m src={src}"


def _neighbours(x):
    bits = struct.unpack("<q", struct.pack("<d", x))[0]
    return [struct.unpack("<d", struct.pack("<q", bits + d))[0] for d in (-1, 0, 1)]


def _cases():
    rng = random.Random(2896)
    out = [(-0.0, -0.0, -0.0, 0x1476), (0.5, 1.5, 12.5, 0x1476),
           (89.9999995, -179.9999995, -12.5, 0x1476)]
    for _ in range(20000):
        out.append((rng.uniform(-90, 90), rng.uniform(-180, 180),
                    rng.uniform(-500, 9000), 0x1476))
    for _ in range(5000):          # ties at 6 dp (lat/lon) and 0 dp (alt)
        t6 = (rng.randint(-180_000_000, 180_000_000) + 0.5) / 1e6
        t0 = rng.randint(-500, 9000) + 0.5
        for a, b in zip(_neighbours(t6), _neighbours(t0)):
            out.append((a, a, b, rng.choice((0x1476, 0x14D8))))
    return out


def test_native_gps_fix_line_matches_the_bridge_bytes():
    if not DRIVER.exists():
        pytest.skip(f"{DRIVER.name} is not built: run `make check` in {HERE}")
    cases = _cases()
    inp = "".join(f"{a!r} {b!r} {c!r} {d}\n" for a, b, c, d in cases)
    got = subprocess.run([str(DRIVER), "--format"], input=inp, text=True,
                         capture_output=True, check=True).stdout.splitlines()
    want = [_bridge_line(*c) for c in cases]
    assert len(got) == len(want)
    bad = [(c, g, w) for c, g, w in zip(cases, got, want) if g != w]
    assert not bad, f"{len(bad)} of {len(cases)} differ; first: {bad[:3]}"
