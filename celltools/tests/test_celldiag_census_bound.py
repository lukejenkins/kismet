# SPDX-License-Identifier: Apache-2.0
"""The census line's bound, and the C relay's reading of it.

## The bound

``capture_cell_diag.c`` reads helper lines into a ``JSON_LINE_MAX`` (8,192)
byte buffer. ``drain_helper_lines`` drops everything past the buffer to the
next newline and then processes the truncated prefix as if it were a complete
line. A ``diag_inventory`` census line can be far larger: at the helper's own
caps (``max_codes`` 512 x ``max_versions_per_code`` 16 = 8,192 keys) the
``unrecognized`` array alone renders to ~101 KB. The producer therefore omits
``keys`` from the wire and bounds the three flag arrays; that side is pinned in
``capture_cell_diag/tests/test_kismet_diag_decode.py``.

## What is pinned here

Bounding the arrays means their length is no longer the count. The C relay
derives ``inventory.unrecognized`` / ``inventory.silent`` and decides whether
to escalate the census line to ``MSGFLAG_ERROR`` from the helper's explicit
``*_total`` integers, falling back to counting elements (``json_array_len``)
and to the absence of the substring ``"unrecognized":[]`` for an older helper.
An installed data dir from an earlier ``make install`` can hold an older
bridge, so that fallback is a live configuration.

That preference and that fallback are C-side behaviour on a JSON line, so they
are testable only by feeding the REAL BINARY a crafted line. This module does
that with a stub tree (``celldiag_stub_tree``): a copy of the binary beside a
``kismet_diag_decode.py`` that ignores its stdin and prints exactly the census
lines under test.

A stub rather than the real helper, because the real helper cannot emit the
two shapes that matter: a census whose arrays are cut (it bounds them) and a
census with no ``*_total`` keys at all (it always sends them). Those are the
old-deployment and the pathological cases, which no fixture capture can
produce.

Like ``test_celldiag_error_delivery``, this drives ``replay=`` rather than an
IMEI. Resolving an IMEI runs an AT scan across every serial port on the host,
which would write AT commands to any live modem from a suite that is meant to
need no hardware.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import decode_oracle
from celldiag_stub_tree import REAL_CAPDIR, run_env, stub_tree
from kismet_capture_ipc import CaptureRun

MSGFLAG_ERROR = 4
MSGFLAG_INFO = 2

IMEI = "123456789012345"

#: The reader's line buffer, read from the C source rather than copied. A
#: literal here would keep passing after someone changed the C side.
_CAPTURE_C = Path(__file__).resolve().parents[2] / \
    "capture_cell_diag/capture_cell_diag.c"


def _json_line_max() -> int:
    import re

    m = re.search(r"^#define\s+JSON_LINE_MAX\s+(\d+)",
                  _CAPTURE_C.read_text(), re.M)
    assert m, f"JSON_LINE_MAX not found in {_CAPTURE_C}"
    return int(m.group(1))


def _stub_bridge(lines: list[dict]) -> str:
    """A bridge that prints ``lines`` and lingers. Staying alive briefly keeps
    the relay from racing a helper that has already exited -- an exited helper
    is reported as helper=dead and the run can tear down before the lines are
    drained."""
    payload = "".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines)
    return ("import sys, time\n"
            f"sys.stdout.write({payload!r})\n"
            "sys.stdout.flush()\n"
            "time.sleep(3)\n")


def _run_with_census(tmp_path: Path, lines: list[dict]) -> CaptureRun:
    binary = stub_tree(tmp_path, bridge=_stub_bridge(lines))
    # A replay file that exists and is empty: the capture thread has nothing to
    # feed, which is exactly what this test wants -- the helper's output is the
    # subject, not the decode.
    replay = tmp_path / "empty.hdlc"
    replay.write_bytes(b"")
    return CaptureRun(str(binary), f"celldiag-{IMEI}:replay={replay}",
                      timeout=20, env=run_env()).run()


def _census(*, status: str, unrecognized, silent, enrich_failed,
            totals: bool, distinct: int = 7,
            table: str = "0xB193/v1,1,1,0,1,0,-",
            n_unrec: int | None = None, n_silent: int | None = None,
            n_enrich: int | None = None,
            n_silent_actionable: int | None = None):
    """A census line in the current field order. `totals` off emits the shape a
    helper without the `*_total` fields produces: arrays and no `*_total` keys.

    The `n_*` overrides exist because the count and the array length can
    differ once the arrays are bounded. Defaulting them to `len(...)` and
    forgetting to pass them makes every escalation test assert "0 is 0" and
    pass against a relay that reads the census as clean.
    """
    out = {"type": "diag_inventory", "ts": 1700000000.0,
           "distinct": distinct, "overflow": 0}
    if totals:
        out["unrecognized_total"] = \
            len(unrecognized) if n_unrec is None else n_unrec
        out["silent_total"] = len(silent) if n_silent is None else n_silent
        if n_silent_actionable is not None:
            out["silent_actionable_total"] = n_silent_actionable
        out["enrich_failed_total"] = \
            len(enrich_failed) if n_enrich is None else n_enrich
    out["status"] = status
    out["table"] = table
    out["unrecognized"] = unrecognized
    out["silent"] = silent
    out["enrich_failed"] = enrich_failed
    return out


def _flags_for(run: CaptureRun, needle: str) -> list[int]:
    return [f for m, f in zip(run.messages, run.message_flags) if needle in m]


# --------------------------------------------------------------------------


def test_a_healthy_census_stays_INFO(tmp_path):
    """Premise guard. Without this the escalation tests below would pass
    against a binary that flags EVERY census, which is the same as flagging
    none -- the failure mode `test_routine_status_messages_stay_INFO` exists
    for."""
    run = _run_with_census(tmp_path, [_census(
        status="celldiag inventory: 7 (code,ver) key(s), 0 unrecognized, "
               "0 silent",
        unrecognized=[], silent=[], enrich_failed=[], totals=True)])
    flags = _flags_for(run, "celldiag inventory:")
    assert flags, f"no census line relayed; messages={run.messages!r}"
    assert MSGFLAG_ERROR not in flags, (
        f"a clean census escalated to ERROR (flags={flags}) -- a permanent "
        f"ERROR is not a signal")


def test_the_escalation_reads_the_TOTAL_not_the_array_length(tmp_path):
    """The load-bearing case, and the one bounding the arrays created.

    A census whose ``unrecognized`` array is EMPTY on the wire while
    ``unrecognized_total`` says 400. Under the old substring test
    (``"unrecognized":[]`` present => nothing to flag) this reads CLEAN. It is
    the worst possible census to read as clean: 400 (code,version) pairs no
    parser handles.

    Not a synthetic shape -- it is what a cut or a further-bounded array
    produces, and the array is bounded now precisely so the line fits.
    """
    run = _run_with_census(tmp_path, [_census(
        status="celldiag inventory: 400 (code,ver) key(s), 400 unrecognized, "
               "0 silent",
        unrecognized=[], silent=[], enrich_failed=[], totals=True,
        distinct=400, n_unrec=400)])
    flags = _flags_for(run, "celldiag inventory:")
    assert flags, f"no census line relayed; messages={run.messages!r}"
    assert MSGFLAG_ERROR in flags, (
        f"a census reporting 400 unrecognized keys was relayed at flags="
        f"{flags}; the relay is still counting array elements")


def test_silent_total_escalates_the_same_way(tmp_path):
    """`silent` is the stronger of the two flags -- a code the emit contract
    DEPENDS on that parsed fine and produced nothing, which on a live source is
    indistinguishable from "no cells in range"."""
    run = _run_with_census(tmp_path, [_census(
        status="celldiag inventory: 9 (code,ver) key(s), 0 unrecognized, "
               "5 silent",
        unrecognized=[], silent=[], enrich_failed=[], totals=True,
        n_silent=5)])
    assert MSGFLAG_ERROR in _flags_for(run, "celldiag inventory:")


def test_an_OLD_helper_with_no_totals_still_escalates_on_the_arrays(tmp_path):
    """The fallback. An installed data dir can hold an older bridge, so a helper
    predating the ``*_total`` fields is a live configuration -- it sends
    arrays and no ``*_total``. Dropping ``json_array_len`` outright would make
    every one of its censuses read as clean."""
    run = _run_with_census(tmp_path, [_census(
        status="celldiag inventory: 3 (code,ver) key(s), 2 unrecognized, "
               "0 silent unrecognized[0x117E/v3, 0xB0C0/v9]",
        unrecognized=["0x117E/v3", "0xB0C0/v9"], silent=[], enrich_failed=[],
        totals=False)])
    flags = _flags_for(run, "celldiag inventory:")
    assert flags, f"no census line relayed; messages={run.messages!r}"
    assert MSGFLAG_ERROR in flags, (
        f"an old helper's array-only census no longer escalates (flags="
        f"{flags}); the fallback was dropped, not kept")


def test_an_OLD_helper_with_clean_arrays_stays_INFO(tmp_path):
    """The fallback's negative control. Without it the test above is satisfied
    by a binary that escalates on every no-totals census -- which would make
    the fallback itself a permanent ERROR."""
    run = _run_with_census(tmp_path, [_census(
        status="celldiag inventory: 3 (code,ver) key(s), 0 unrecognized, "
               "0 silent",
        unrecognized=[], silent=[], enrich_failed=[], totals=False)])
    flags = _flags_for(run, "celldiag inventory:")
    assert flags, f"no census line relayed; messages={run.messages!r}"
    assert MSGFLAG_ERROR not in flags, f"clean old-helper census flagged: {flags}"


def test_the_real_helpers_worst_case_census_fits_the_readers_buffer():
    """End-to-end version of the producer-side bound: the largest census the
    SHIPPING bridge can emit, measured against the C reader's own constant.

    Skips, naming why, when this tree has no fetched decoder -- the bridge
    imports diaggrok at module top.
    """
    oracle = decode_oracle.discover()
    if not oracle.ok or decode_oracle.import_diaggrok("diaggrok") is None:
        pytest.skip(oracle.skip_reason())
    sys.path.insert(0, str(REAL_CAPDIR))
    try:
        import kismet_diag_decode as kdd
    finally:
        sys.path.pop(0)

    inv = kdd.DiagInventory()
    t = 1000.0
    for code in range(inv.max_codes):
        for ver in range(inv.max_versions_per_code):
            inv.record(0xB000 + code, ver, False, t, enrich_failed=True)
            t += 0.5
    line = json.dumps(inv.summary(9999.0, wire=True), separators=(",", ":"))
    limit = _json_line_max()
    assert len(line) <= limit, (
        f"the shipping helper's worst-case census is {len(line)} bytes against "
        f"JSON_LINE_MAX {limit} in {_CAPTURE_C.name}")


# --- the actionable split: what ESCALATES vs what is merely SHOWN ---
#
# `silent_total` counts every emit-contract code yielding nothing, which
# correctly includes 0x14D8 -- a code whose lat/lon/alt are hardcoded 0.0 in
# firmware and which therefore parses 100% and emits 0% on every healthy
# source. Escalating on it would make every census an ERROR (on an RM500Q-AE
# it is commonly the sole silent key). The helper sends
# `silent_actionable_total` beside `silent_total`; the relay escalates on the
# former and falls back to the latter.

def test_a_structurally_silent_census_no_longer_escalates(tmp_path):
    """One silent key, and it is the one that can never be anything else
    (a typical RM500Q-AE state). The census still SAYS "1 silent" -- only the
    severity changes, because there is nothing here an operator can act on."""
    run = _run_with_census(tmp_path, [_census(
        status="celldiag inventory: 4 (code,ver) key(s), 0 unrecognized, "
               "1 silent silent[0x14D8/v1]",
        unrecognized=[], silent=["0x14D8/v1"], enrich_failed=[], totals=True,
        n_silent=1, n_silent_actionable=0)])
    flags = _flags_for(run, "celldiag inventory:")
    assert flags, f"no census line relayed; messages={run.messages!r}"
    assert MSGFLAG_ERROR not in flags, (
        f"a census whose only silent key CANNOT emit was relayed at ERROR "
        f"(flags={flags}); the relay is still escalating on silent_total")


def test_a_real_silence_still_escalates_when_mixed_with_a_structural_one(tmp_path):
    """The negative control, a state seen on the RM520N-GL: 0x1476/v24
    silent (engine had no time solution) ALONGSIDE 0x14D8. The
    actionable count is 1, so this must still be an ERROR -- otherwise the fix
    above is indistinguishable from switching the alarm off."""
    run = _run_with_census(tmp_path, [_census(
        status="celldiag inventory: 5 (code,ver) key(s), 0 unrecognized, "
               "2 silent silent[0x1476/v24, 0x14D8/v1]",
        unrecognized=[], silent=["0x1476/v24", "0x14D8/v1"], enrich_failed=[],
        totals=True, n_silent=2, n_silent_actionable=1)])
    flags = _flags_for(run, "celldiag inventory:")
    assert flags, f"no census line relayed; messages={run.messages!r}"
    assert MSGFLAG_ERROR in flags, (
        f"a census with a REAL silent code was relayed at flags={flags}; the "
        f"actionable split suppressed the finding it exists to surface")


def test_a_helper_predating_the_split_still_escalates_on_silent_total(tmp_path):
    """Precedence: an installed data dir can hold an older bridge,
    so a helper sending `silent_total` and no `silent_actionable_total` is a live
    configuration. It must behave exactly as it did before this change -- reading
    a missing key as "nothing to flag" is the regression direction."""
    run = _run_with_census(tmp_path, [_census(
        status="celldiag inventory: 9 (code,ver) key(s), 0 unrecognized, "
               "5 silent",
        unrecognized=[], silent=[], enrich_failed=[], totals=True,
        n_silent=5)])
    flags = _flags_for(run, "celldiag inventory:")
    assert flags, f"no census line relayed; messages={run.messages!r}"
    assert MSGFLAG_ERROR in flags, (
        f"an old helper's silent_total census stopped escalating (flags="
        f"{flags}); the fallback was dropped, not kept")
