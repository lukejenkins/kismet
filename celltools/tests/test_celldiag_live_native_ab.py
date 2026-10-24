"""The live-hardware ``nativedecode`` off-vs-on A/B.

**Why the binary, not a fixture, is the oracle.** In ``on`` mode the Python
bridge is still fed every byte — only its ``cell_observation`` line is dropped
in ``drain_helper_lines`` — so **both renderings of one stream exist at the
same time**. The source counts them (``emitted`` vs ``bridge-suppressed``) and
reports their agreement itself, at ``MSGFLAG_ERROR`` if they differ. The A/B is
therefore a property of every live run, and the measurement is to *read the
spindown line*.

**Two ways this test could pass while measuring nothing, both foreclosed.**

1. ``0 == 0``. A run with no observations makes the two counters agree
   vacuously. ``MIN_OBS`` fails the test instead.
2. ``--silent``. The counters live in a **log line**, and the shared harness
   runs Kismet with ``--silent``, which suppresses every message after
   startup. A regex that finds nothing would look exactly like a run that
   printed nothing. ``silent=False`` is mandatory here, and the absence of the
   line is a failure rather than a skip.

Skips cleanly with no modem, and when the binary was built without
``make NATIVE=1`` (``nativedecode=on`` is refused at open on a baseline build,
deliberately — it would suppress the only decoder present).
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decode_oracle  # noqa: E402

from test_celldiag_server_routes import (  # noqa: E402
    KismetServer,
    _base,
    _server_or_skip,
)
from test_celldiag_live_modem_mask import _live_modem_or_skip  # noqa: E402

#: Below this the A/B is vacuous. An unregistered LTE modem with signal yields
#: roughly 1.4 obs/s, so 90 s clears this comfortably; the
#: bar is deliberately low enough that a quiet RF environment does not turn a
#: real regression into a red test for the wrong reason.
MIN_OBS = 20

DWELL = 90

_AB = re.compile(r"emitted=(\d+) bridge-suppressed=(\d+) "
                 r"emit_declined=(\d+) emit_truncated=(\d+)")


@pytest.fixture()
def native_server(tmp_path: Path):
    _server_or_skip()
    oracle = decode_oracle.discover()
    if not oracle.ok:
        pytest.skip(oracle.skip_reason())
    iface, _desc = _live_modem_or_skip()
    srcdef = (f"{iface}:mask=wardrive,f3=off,name=nativeab,retry=false,"
              f"nativedecode=on")
    # silent=False is load-bearing: the measurement IS a log line.
    srv = KismetServer(tmp_path, srcdef, silent=False)
    with srv:
        try:
            srv.await_running(True)
        except AssertionError as e:
            if "native decode legs" in srv.tail(6000):
                pytest.skip("binary built without 'make NATIVE=1'; "
                            "nativedecode=on is refused at open by design")
            raise e
        yield srv


def test_native_and_bridge_render_the_same_observation_count(native_server):
    """On hardware: emitted == bridge-suppressed at spindown."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < DWELL:
        time.sleep(10)

    row = native_server.source() or {}
    obs = int(row.get("kismet.datasource.celldiag.obs_total") or 0)

    base = _base(row)
    native_server.get(base + "close_source.cmd")
    native_server.await_running(False)
    time.sleep(8)
    log = native_server.tail(400_000)

    hits = _AB.findall(log)
    assert hits, (
        "the `on`-only A/B group never printed. Either nativedecode=on was "
        "not armed, or the server was run with --silent (which suppresses "
        "every message after startup).\n" + log[-3000:])

    # ── negative control: a vacuous 0 == 0 is a FAILURE, not a pass ──────
    assert obs >= MIN_OBS, (
        f"only {obs} observations in {DWELL}s -- below MIN_OBS={MIN_OBS}, so "
        f"'emitted == bridge-suppressed' would agree vacuously and prove "
        f"nothing")

    pairs = [(int(e), int(s), int(d), int(t)) for e, s, d, t in hits]
    seq = " ".join(f"{e}/{s}" for e, s, _, _ in pairs)

    for _e, _s, declined, truncated in pairs:
        assert declined == 0, f"emit_declined={declined}  [{seq}]"
        assert truncated == 0, f"emit_truncated={truncated}  [{seq}]"

    # ── the assertion the design actually supports: agreement AT SPINDOWN ──
    #
    # Not "every interval agrees" -- that is stricter than the mechanism and
    # fails on a live modem. The two counters are incremented at different
    # pipeline stages: the native tap counts as it renders, while the bridge's
    # `cell_observation` line reaches `drain_helper_lines` through a pipe and
    # is counted when it arrives. A stats line printed mid-stream therefore
    # catches the native counter a few records ahead.
    #
    # The final line is read after close_source + drain, the only point at
    # which both renderings have seen the whole stream.
    emitted, suppressed, _, _ = pairs[-1]
    assert emitted == suppressed, (
        f"the two renderings of one stream disagree AT SPINDOWN: native "
        f"emitted {emitted}, bridge-suppressed {suppressed}. This is exactly "
        f"what the counters exist to catch.\n  sequence (emitted/suppressed): "
        f"{seq}")
    assert emitted > 0, f"the final interval emitted nothing  [{seq}]"

    # ── and the skew is bounded and one-directional, so a real divergence
    #    cannot hide inside "it's just lag" ────────────────────────────────
    for e, s, _, _ in pairs:
        assert e >= s, (
            f"the BRIDGE led the native tap ({e} emitted, {s} suppressed) -- "
            f"pipe lag can only make native lead, so this is a real "
            f"divergence, not skew.\n  sequence: {seq}")
        assert e - s <= 16, (
            f"in-flight skew of {e - s} is too large to be pipe lag.\n"
            f"  sequence: {seq}")
