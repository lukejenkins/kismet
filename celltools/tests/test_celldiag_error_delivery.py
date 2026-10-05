# SPDX-License-Identifier: Apache-2.0
"""Post-open error reporting for ``kismet_cap_cell_diag``.

## The hazard

Without a delivered reason, a celldiag source whose bring-up fails tells the
operator only this::

    INFO:  Cell DIAG source celldiag-<imei> accepted; opening modem in the background
    INFO:  celldiag stats: NODATA | port=closed bytes=0 | obs=0 | helper=dead
    ERROR: Data source '...' encountered an error: IPC connection closed
    ALERT: SOURCEERROR ... will attempt to re-open the source in 5 seconds. (1 failures)

...forever, once per 5 s. ``helper=dead`` plus ``IPC connection closed`` reads
like a modem or a port problem, even when the helper has composed a precise,
actionable sentence (for example, ``helper_dir_check()`` reporting a missing
decoder).

## Why: ``cf_send_error()`` is discarded under protocol v3

``cf_send_error()`` serialises a ``KIS_EXTERNAL_V3_CMD_ERROR`` frame, and **no
server dispatcher handles that command.**
``kis_external_interface::dispatch_rx_packet_v3`` has cases for MESSAGE / PING /
PONG / SHUTDOWN / the WEB and EVT families and falls through to
``kis_datasource::dispatch_rx_packet_v3``, which has cases for the KDS reports
and no ERROR either. Both return false and the frame is dropped. From
``kis_datasource.cc``::

    // v3 drops explicit error/warning reports and rolls them into the return
    // codes of the packet headers itself.  The error message is sent as a
    // message prior to the packet being sent.

So ``diag_report_error()`` also says it as a MESSAGE (which the server does
handle) carrying ``MSGFLAG_ERROR``.

## Why only *post-open* failures are affected

An error raised from ``open_callback`` rides out on the OPENREPORT's return code
and message -- the idiom the comment above describes -- and is always legible.
Only failures raised *after* the open was answered need the MESSAGE route:
deferred bring-up, replay/rawlog open, and the helper dying mid-run. That
asymmetry is asserted below rather than described, by the open-time tests at
the bottom of this file.

## Why this drives a replay source and not a bogus IMEI

The obvious reproduction -- point celldiag at an IMEI that matches nothing -- is
**not safe here** and must not be reintroduced. Resolving an IMEI runs an AT
scan across every serial port on the host, which on a wardriving workstation
means opening the operator's live modems and writing AT commands to them, from a
suite whose whole premise is that it needs no hardware. ``test_cellat_ipc``
documents the same hazard and the large slowdown that comes with it.

``replay=<missing file>`` reaches the identical code path -- ``capture_thread``,
after the open was answered, reporting through ``diag_report_error()`` -- while
opening no modem and scanning no ports. The failure mode under test is the
*delivery mechanism*, which is common to every post-open error, so the cheapest
and safest trigger is the correct one to use.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

# conftest.py puts both this directory and celltools/ on sys.path, so these
# resolve identically under `pytest <file>`, `pytest celltools/tests` and
# `make -C celltools check`.
import celldiag_parity
from celldiag_stub_tree import (forget_installed_datadir, python3_shim,
                                run_env, stub_tree)
from kismet_capture_ipc import CaptureRun

#: MSGFLAG_ERROR, from kis_external_packet.h. The point is not just
#: that the text arrives but that it arrives flagged as an error, so the server
#: renders it as ERROR rather than burying it among INFO status lines.
MSGFLAG_ERROR = 4
MSGFLAG_INFO = 2

#: Any well-formed IMEI. Never resolved -- ``replay=`` short-circuits the modem
#: lookup entirely, which is precisely why this test needs no hardware.
IMEI = "123456789012345"

MISSING_REPLAY = "/nonexistent/celldiag-error-delivery-probe.hdlc"


def _binary_or_skip() -> str:
    found = celldiag_parity.discover("celldiag")
    if not found.ok:
        pytest.skip(found.skip_reason())
    return str(found.path)


def _decoder_or_skip(binary: str) -> None:
    """Skip unless this binary's tree has a fetched decoder, which the run needs
    only to get PAST ``helper_dir_check``.

    Without one the source fails in ``open_callback`` instead, which is the
    open-time path -- the test would then pass whether or not post-open
    delivery works, and prove nothing. A skip here is easy to miss, so it names
    the one command that ends it.
    """
    for root in list(Path(binary).resolve().parents)[:4]:
        if (root / ".deps/diaggrok/src").is_dir():
            return
    pytest.skip(
        "no decoder: this binary's tree has no fetched decoder (run "
        "`make -f standalone.mk deps` in capture_cell_diag/)")


def _run_with_failing_replay() -> CaptureRun:
    binary = _binary_or_skip()
    _decoder_or_skip(binary)
    definition = f"celldiag-{IMEI}:replay={MISSING_REPLAY}"
    return CaptureRun(binary, definition, timeout=20, env=run_env()).run()


@pytest.fixture(scope="module")
def failed_run() -> CaptureRun:
    return _run_with_failing_replay()


def test_the_failure_is_post_open_not_an_open_rejection(failed_run):
    """Premise guard: the open SUCCEEDED, so this is the silent class.

    If the open were rejected instead, the reason would ride the OPENREPORT and
    the rest of this module would be testing the open-time path instead.
    """
    assert failed_run.open_code == 1, (
        f"expected the open to be answered OK; got code={failed_run.open_code} "
        f"message={failed_run.open_message!r}")


def test_post_open_failure_reason_is_delivered_as_a_message(failed_run):
    """The anchor: the reason, not just the status lines, is in the messages."""
    hits = [m for m in failed_run.messages if "cannot open replay file" in m]
    assert hits, (
        "the post-open failure reason never reached the MESSAGE channel -- the "
        "operator sees only 'IPC connection closed'. messages="
        f"{failed_run.messages!r}")


def test_the_reason_is_flagged_as_an_error_not_info(failed_run):
    """Arriving as INFO would bury it between two status lines."""
    flags = [f for m, f in zip(failed_run.messages, failed_run.message_flags)
             if "cannot open replay file" in m]
    assert flags, "reason not present at all; see the anchor test"
    assert MSGFLAG_ERROR in flags, (
        f"reason arrived with flags {flags}, expected MSGFLAG_ERROR "
        f"({MSGFLAG_ERROR}); INFO is {MSGFLAG_INFO}")


def test_the_error_frame_is_still_emitted(failed_run):
    """``cf_send_error`` is kept, not replaced.

    It is the protocol-correct frame, it is what a v2 peer consumes, and it is
    what drives the source error state. The message is added alongside it; a future
    refactor that drops the error frame in favour of the message would be a
    different behaviour change and should fail here first.
    """
    assert any("cannot open replay file" in e for e in failed_run.errors), (
        f"ERROR frame no longer emitted; errors={failed_run.errors!r}")


# ---------------------------------------------------------------------------
# Open-time helper failures
#
# An unconfigured binary inside its own tree decodes with that tree's fetched
# decoder, so there is nothing to refuse there. The property -- a helper
# problem that is knowable at open is reported ON the open, never left to
# surface as a later obs=0 -- is pinned below where it can still happen, plus
# the dead-helper exit report.
# ---------------------------------------------------------------------------

def _rejected_open(run: CaptureRun) -> str:
    """The OPENREPORT's message, after asserting the open was REFUSED."""
    assert run.open_code != 1, (
        "the open was accepted; the reason would then arrive only after the "
        f"source reported healthy. open_message={run.open_message!r} "
        f"errors={run.errors!r}")
    return run.open_message or ""


def test_an_unconfigured_binary_outside_any_tree_says_decoder_not_found(tmp_path):
    """The open-time refusal, on the one input that reaches it.

    Copied out of its tree, the binary has no fetched decoder beside it and
    nothing configured, so ``self_tree_decoder_root`` finds nothing and the open
    must be refused with the actionable ``decoder not found`` sentence. Inside
    its tree it would open instead.
    """
    binary = _binary_or_skip()
    lone = tmp_path / Path(binary).name
    shutil.copy2(binary, lone)
    forget_installed_datadir(lone)   # this host's `make install` is not "outside any tree"
    run = CaptureRun(str(lone), f"celldiag-{IMEI}:replay={MISSING_REPLAY}",
                     timeout=20, env=run_env()).run()
    msg = _rejected_open(run)
    assert "decoder not found" in msg, f"open_message={msg!r}"


def test_a_dead_helper_never_reports_a_fabricated_exit_0(tmp_path):
    """Never report "exited unexpectedly (exit 0)" for a status nobody received.

    The capture framework's signal thread reaps every child with
    ``waitpid(-1)``, so celldiag's own ``waitpid`` usually fails ECHILD and its
    status word stays 0. Here the interpreter is a stub that exits 3: the
    report must say 3 or say the status is unknown -- never 0.

    The order is forced, not left to the race. Who reaps first is a race, and
    a stub that simply exits lets celldiag win it about as often as not -- the
    true 3 is then read and a fabricated-0 bug would go unnoticed. So the stub
    exits 3 at once while a grandchild holds the
    bridge's pipes open for 1 s: the framework is certain to have reaped the
    stub before celldiag sees EOF, which is the production ordering.
    """
    binary = stub_tree(tmp_path)
    # fd 3 keeps the ORIGINAL stdin: a non-interactive sh gives a background
    # job /dev/null as stdin before any explicit redirection applies.
    shim = python3_shim(tmp_path,
                        "#!/bin/sh\nexec 3<&0\nsleep 1 <&3 3<&- &\nexit 3\n")
    replay = tmp_path / "some.hdlc"
    # Past the 64 KiB pipe buffer: a replay that fits in it reaches EOF and
    # takes the flush path, so the death is never reported at all.
    replay.write_bytes(b"\x7e" + b"\x00" * (1 << 20))
    run = CaptureRun(str(binary), f"celldiag-{IMEI}:replay={replay}",
                     timeout=20, env=run_env(shim)).run()
    reports = [m for m in run.messages + run.errors
               if "exited unexpectedly" in m]
    assert reports, (
        f"the helper death was not reported; messages={run.messages!r} "
        f"errors={run.errors!r}")
    for m in reports:
        assert "(exit 0)" not in m, f"fabricated clean exit: {m!r}"
        assert "(exit 3)" in m or "exit status unavailable" in m, m
