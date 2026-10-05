"""The DIAG read loop must never wait on the Python decoder.

With a blocking write to the decode bridge, a decoder that falls behind a DIAG
burst fills the 64 KiB pipe, the read loop stops reading the tty, and a
USB-serial port drops the modem's bytes with nothing in the kernel log (seen as
CRC-bad frames in the capture). So decoder input is queued, and when the queue
overflows the decoder input is dropped -- counted, reported, recorded -- never
the capture.

These drive the real binary with a stub decoder -- a copy of the binary in a
stub tree (``celldiag_stub_tree``) whose ``kismet_diag_decode.py`` reads
at a pace the test sets -- and a pty fake modem.

A pty is not a USB-serial port: it applies backpressure instead of dropping.
With a blocking bridge write the fake modem's writer would simply be throttled
to the decoder's pace, so the discriminating measurement is throughput and a
lossless tee -- every frame the fake sent is in the rawlog, at a rate far above
what the decoder consumed -- not CRC damage.

The fast-decoder run is the positive control: the same stream, a decoder that
keeps up, and the bridge counters must report 0 dropped (measured, not absent).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fake_at_modem import FakeAtModem  # noqa: E402
from fake_diag_modem import FakeDiagModem, hdlc_frame  # noqa: E402
from celldiag_stub_tree import run_env, stub_tree  # noqa: E402
from test_celldiag_replay_ab import CellDiagRun  # noqa: E402

IMEI = "123456789012345"   # the leak gate's blessed placeholder
#: Frames the fake may send before the capture opens its pty. The filler starts
#: when the fake does; what it wrote into the unopened pty is discarded by the
#: open's flush (typically 1 of 2000, in the fast-decoder control too).
#: A pty buffers at most 64 KiB, so ~16 of these ~4 KB frames is the ceiling.
PRE_OPEN_SLACK = 16
QUEUE_ENV = "CELLDIAG_BRIDGE_QUEUE_BYTES"

#: The stub decoder. It answers the --help capability probe (no optional
#: features), then reads stdin in `chunk`-byte bites, sleeping `delay` between
#: them, and writes the byte total to `count` at EOF. It emits no observations.
STUB = r'''
import os, sys, time
if "--help" in sys.argv:
    print("usage: kismet_diag_decode.py [--imei IMEI]")
    sys.exit(0)
delay = float(os.environ.get("STUB_DELAY", "0"))
chunk = int(os.environ.get("STUB_CHUNK", "65536"))
total = 0
fd = sys.stdin.fileno()
while True:
    b = os.read(fd, chunk)
    if not b:
        break
    total += len(b)
    if delay:
        time.sleep(delay)
with open(os.environ["STUB_COUNT"], "w") as f:
    f.write(str(total))
'''


class _Stub:
    """A stub-tree binary whose bridge is STUB, and the env its runs get."""

    def __init__(self, tmp_path: Path) -> None:
        self.binary = str(stub_tree(tmp_path, bridge=STUB))
        self.count = tmp_path / "decoder_consumed"
        self.env = {**run_env(), "STUB_COUNT": str(self.count)}

    def set(self, **kv: str) -> None:
        self.env.update(kv)


@pytest.fixture()
def stub(tmp_path):
    return _Stub(tmp_path)


def _last_stats(run: CellDiagRun) -> dict:
    stats = [json.loads(c) for c in run.control if '"diag_stats"' in c.replace(" ", "")]
    assert stats, f"no diag_stats line: {run.control} errors={run.errors}"
    return stats[-1]


def _drop_messages(run: CellDiagRun) -> list[str]:
    return [m for m in run.messages + run.errors if "decoder fell behind" in m]


def _live(stub: "_Stub", tee: Path, frames: int, pad: int,
          hz: float) -> tuple[CellDiagRun, FakeDiagModem]:
    """A live source on a fake modem that sends exactly `frames` filler frames
    of ~`pad` bytes at `hz`, then goes quiet; the run ends after the quiet."""
    with FakeAtModem(imei=IMEI) as at, FakeDiagModem(
            filler_hz=hz, filler_pad=pad, filler_max=frames) as dg:
        definition = (f"celldiag-{IMEI}:atport={at.port},diagport={dg.port},"
                      f"nomask=true,defer=false,stats_interval=0,"
                      f"clock_anchor_sec=0,rawlog={tee}")
        def quiet(_run) -> bool:
            # Every frame sent, then 2 s of silence: the server's graceful
            # close, so the helper reaches its final diag_stats. A read
            # loop throttled to the stub's pace never gets here, and the run
            # ends at the timeout instead.
            last = dg.filler_last_wall
            return dg.filler_sent >= frames and last is not None and time.time() - last > 2.0

        run = CellDiagRun(stub.binary, definition, timeout=frames / hz + 12.0,
                          close_when=quiet, env=stub.env).run()
    return run, dg


# ── live: a slow decoder never stalls the read loop ──────────────────────────
def test_a_slow_decoder_does_not_stall_the_read_loop(tmp_path, stub):
    # ~20 KB/s of decoder against ~1.6 MB/s of stream, and a 256 KiB queue.
    stub.set(STUB_DELAY="0.2")
    stub.set(STUB_CHUNK="4096")
    stub.set(**{QUEUE_ENV: str(256 * 1024)})
    tee = tmp_path / "tee.hdlc"
    frames, pad, hz = 2000, 4000, 400.0

    run, dg = _live(stub, tee, frames, pad, hz)

    assert dg.filler_sent == frames, (
        f"the fake modem sent {dg.filler_sent} of {frames} frames: its writes "
        f"were back-pressured, so the read loop waited on the decoder")
    data = tee.read_bytes()
    one = dg.filler_frame
    assert one, 'the fake sent no padded frame'
    assert frames - PRE_OPEN_SLACK <= data.count(one) <= frames, (
        f"the tee holds {data.count(one)} of {frames} frames -- capture lost bytes")

    st = _last_stats(run)
    assert st.get("bridge_dropped_chunks", 0) > 0, st
    assert st["bridge_dropped_bytes"] > 0, st
    # Everything the read loop took went to the decoder or was counted dropped.
    assert len(_drop_messages(run)) == 1, _drop_messages(run)
    rows = [json.loads(r) for r in run.bridge_drops]
    assert len(rows) == 1 and rows[0]["schema"] == "diag-bridge-drop/1", rows


def test_a_decoder_that_keeps_up_drops_nothing(tmp_path, stub):
    """The positive control: same stream, fast decoder, counters read 0."""
    stub.set(STUB_DELAY="0")
    tee = tmp_path / "tee.hdlc"
    frames, pad, hz = 2000, 4000, 400.0

    run, dg = _live(stub, tee, frames, pad, hz)

    assert dg.filler_sent == frames
    assert frames - PRE_OPEN_SLACK <= tee.read_bytes().count(dg.filler_frame) <= frames
    st = _last_stats(run)
    assert "bridge_dropped_chunks" in st, f"the bridge counters are absent: {st}"
    assert (st["bridge_dropped_chunks"], st["bridge_dropped_bytes"]) == (0, 0), st
    assert _drop_messages(run) == []
    assert run.bridge_drops == []


# ── replay: its input can wait, so it never drops ────────────────────────────
def test_a_replay_behind_a_slow_decoder_waits_and_delivers_every_byte(
        tmp_path, stub):
    # ~1.6 MB/s of decoder, and a 1 MiB queue: far smaller than the replay, and
    # larger than one 64 KiB read, so ~1 MiB is still QUEUED when the replay
    # hits EOF. (A queue no larger than a read is always empty at EOF -- the
    # loop reads a replay only while a whole read fits -- and the EOF drain is
    # then untested, so a "no drain" mutation would survive.)
    stub.set(STUB_DELAY="0.01")
    stub.set(STUB_CHUNK="16384")
    stub.set(**{QUEUE_ENV: str(1024 * 1024)})
    replay = tmp_path / "big.hdlc"
    frame = hdlc_frame(bytes(range(256)) * 8)
    replay.write_bytes(frame * 1000)                 # ~2 MB

    run = CellDiagRun(stub.binary, f"celldiag-{IMEI}:replay={replay},stats_interval=0",
                      timeout=120.0, env=stub.env).run()

    st = _last_stats(run)
    assert (st.get("bridge_dropped_chunks"), st.get("bridge_dropped_bytes")) == (0, 0), st
    assert _drop_messages(run) == []
    assert stub.count.read_text() == str(replay.stat().st_size), (
        "the decoder did not receive the whole replay")
