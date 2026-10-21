"""Transport tests for the cell DIAG bridge helper beside this file.

If this module fails at collection with an ``ImportError`` from ``diaggrok``
(e.g. ``cannot import name 'is_probably_capture' from 'diaggrok.dlf'``), the
pinned decoder library is older than what the bridge imports: the observation
layer (``diaggrok.observation``, ``diaggrok.gnss_gate``, ``diaggrok.inventory``),
``dlf.is_probably_capture`` and the log-code parsers. That is a statement about
the pin, not about this tree, and it is left loud on purpose: a skip would hide
a real dependency gap. Bump the pin rather than skipping.

What is tested here is transport: the emitted JSON record shapes, the
diag_inventory wire record and its length bounds, the GNSS gps_fix line, the
guard that keeps one bad record from killing the stream, the stdin chunking,
and the exit-status contract the C capture source depends on.

Not tested here, deliberately:

  * The mapping and the GNSS gates. They are decode -- what a field means, and
    which gate withholds a record -- and they live in the diaggrok decoder
    library with their own tests. This file asserts that the writer emits what
    the gate accepted, never what the gate should decide.
  * Anything needing real captures. This tree is code-only and ships no
    captured radio data.
  * Container unwrapping and F3 relay. This tree's bridge has neither, by
    design -- read_capture_file does a plain read, and F3 is absent rather than
    disabled.

Run with the decoder library fetched and on the path::

    make -f standalone.mk deps
    PYTHONPATH=.deps/diaggrok/src python3 -m pytest tests/
"""
from __future__ import annotations

import importlib.util
import io
import json
import struct
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent


def _load_bridge():
    """Import the bridge that sits beside these tests.

    By path rather than by package import: this directory is not a package and
    the helper is exec'd as a script by the C capture source, so loading it the
    same way keeps the test honest about what ships.
    """
    path = HERE / "kismet_diag_decode.py"
    spec = importlib.util.spec_from_file_location("kismet_diag_decode", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kdd = _load_bridge()

# ---------------------------------------------------------------------------
# A synthetic 0xB193, because this tree ships no captures
# ---------------------------------------------------------------------------
#
# Synthesized, not extracted: lifting capture bytes into this tree would put
# measured radio data in published source. So the payload is built to the
# parser's v3 (MDM9200) subpacket shape -- the simplest one the decoder
# accepts -- and nothing about it came off a modem.
#
# What the tests below need from it is only "a record the decoder returns a
# result for", so they can make the ENRICHMENT step raise and watch the stream
# survive. The cell values are arbitrary and load-bearing only in that the PCI
# must be <= 503 for the v3 arm to emit an entry.
#: A synthetic IMEI, not a device's: an identifier lifted off real hardware
#: has no business in published source, and the helper treats it as
#: an opaque label anyway -- it is stamped into the provenance block and never
#: parsed. The diagspec harnesses use "0" for the same reason.
_SYNTH_IMEI = "0"

_SYNTH_B193_EARFCN = 800
_SYNTH_B193_PCI = 6


def _synthetic_b193_payload():
    """A 0xB193 payload ``diaggrok.parse`` decodes: v=1, one v3 subpacket."""
    return (
        bytes([1, 1])                                   # version, num_subpackets
        + struct.pack("<H", 0)                          # outer counter
        + bytes([0, 3])                                 # subpacket id, version=3
        + struct.pack("<H", 4)                          # subpacket size
        + struct.pack("<HH", _SYNTH_B193_EARFCN, _SYNTH_B193_PCI)
        + bytes(4)                                      # pad to the 14 B minimum
    )


def _chunks(data, size):
    return [data[i:i + size] for i in range(0, len(data), size)]


def test_stdin_chunks_uses_read1_not_blocking_read():
    """_stdin_chunks must drain via read1 (available bytes) so a live pipe
    does not deadlock waiting for a full buffer."""
    calls = {"read": 0, "read1": 0}

    class FakePipe:
        def __init__(self):
            self._data = [b"abc", b"de", b""]

        def read1(self, _n):
            calls["read1"] += 1
            return self._data.pop(0)

        def read(self, _n):
            calls["read"] += 1
            return self._data.pop(0)

    got = list(kdd._stdin_chunks(FakePipe(), size=64))
    assert got == [b"abc", b"de"]
    assert calls["read1"] >= 1 and calls["read"] == 0


# --- per-(code,version) inventory census ---

def test_diag_inventory_record_and_summary():
    """Core DiagInventory bookkeeping: count/decoded/dropped per (code,version),
    the `new` flag for a recognized-but-never-decoded key, and the pre-rendered
    status line the C relay surfaces."""
    inv = kdd.DiagInventory()
    # 0xB193 v36 decoded twice; 0xB193 v99 dropped once; 0xFFFF v0 dropped twice.
    inv.record(0xB193, 36, True, 100.0)
    inv.record(0xB193, 36, True, 101.0)
    inv.record(0xB193, 99, False, 102.0)
    inv.record(0xFFFF, 0, False, 103.0)
    inv.record(0xFFFF, 0, False, 104.0)

    s = inv.summary(105.0)
    assert s["type"] == "diag_inventory"
    assert s["distinct"] == 3
    assert s["overflow"] == 0
    by = {(k["code"], k["ver"]): k for k in s["keys"]}
    assert by[("0xB193", 36)]["count"] == 2
    assert by[("0xB193", 36)]["decoded"] == 2
    assert by[("0xB193", 36)]["new"] is False
    # keys seen but never decoded are flagged new and listed in unrecognized.
    assert by[("0xB193", 99)]["new"] is True
    assert by[("0xFFFF", 0)]["dropped"] == 2 and by[("0xFFFF", 0)]["new"] is True
    assert set(s["unrecognized"]) == {"0xB193/v99", "0xFFFF/v0"}
    assert "2 unrecognized" in s["status"]
    assert "last" in by[("0xB193", 36)] and by[("0xB193", 36)]["last"] == 101.0


def test_diag_inventory_per_key_rate():
    """Per-key rate/count is viewable: every census key carries a RATE, the lifetime average over its observed window: count / (last-first). A
    key with a single observation has zero span, so no interval has elapsed and
    the rate is reported 0.0 rather than dividing by zero."""
    inv = kdd.DiagInventory()
    # One key, 4 records across a 3.0 s window -> 4 / 3.0 records/s.
    inv.record(0xB821, 1, True, 10.0)
    inv.record(0xB821, 1, True, 11.0)
    inv.record(0xB821, 1, True, 12.0)
    inv.record(0xB821, 1, True, 13.0)
    # A single-observation key: zero span, rate must be 0.0 (not a ZeroDivision).
    inv.record(0xB193, 36, True, 20.0)

    by = {(k["code"], k["ver"]): k for k in inv.summary(99.0)["keys"]}
    multi = by[("0xB821", 1)]
    assert multi["count"] == 4
    # first-seen is exposed so the rate is auditable from the row itself.
    assert multi["first"] == 10.0
    assert multi["last"] == 13.0
    assert multi["rate"] == round(4 / 3.0, 4)
    single = by[("0xB193", 36)]
    assert single["first"] == single["last"] == 20.0
    assert single["rate"] == 0.0


def test_diag_inventory_unrecognized_serialization_contract():
    """The C relay escalates the diag_inventory line from INFO to ERROR
    when there are unrecognized (code,version) keys, detecting the empty case by
    the exact substring `"unrecognized":[]`. Pin that serialization so a future
    change to the census output can't silently disable the severity escalation."""
    sep = (",", ":")   # the exact separators run_bytes/_emit_records serialize with

    # No unrecognized keys -> renders the empty-array sentinel the C side keys on.
    clean = kdd.DiagInventory()
    clean.record(0xB193, 36, True, 1.0)
    assert '"unrecognized":[]' in json.dumps(clean.summary(2.0), separators=sep)

    # At least one unrecognized key -> the sentinel is ABSENT (C escalates).
    dirty = kdd.DiagInventory()
    dirty.record(0xFFFF, 0, False, 1.0)   # dropped, never decoded
    line = json.dumps(dirty.summary(2.0), separators=sep)
    assert '"unrecognized":[]' not in line
    assert '"unrecognized":["0xFFFF/v0"]' in line


def test_diag_inventory_overflow_cap():
    """Bounded distinct-CODE keyset: past max_codes, a NEW code is dropped and
    counted, existing keys still update -- a novel/hostile stream can't grow the
    map unboundedly."""
    inv = kdd.DiagInventory(max_codes=2)
    inv.record(0x0001, 0, True, 1.0)
    inv.record(0x0002, 0, True, 2.0)
    inv.record(0x0003, 0, True, 3.0)   # 3rd distinct code -> overflow, not stored
    inv.record(0x0001, 0, True, 4.0)   # existing key still updates
    s = inv.summary(5.0)
    assert s["distinct"] == 2
    assert s["overflow"] == 1
    assert "over cap" in s["status"]
    by = {(k["code"], k["ver"]): k for k in s["keys"]}
    assert by[("0x0001", 0)]["count"] == 2


def test_diag_inventory_per_code_version_cap():
    """The byte-0-as-data guard: one code emitting many byte-0 values is capped at
    max_versions_per_code, so it cannot evict genuine codes -- while every OTHER
    code is still admitted (code coverage is order-independent). This is the fix
    for the real full-replay saturation where 0x117E-class codes (byte-0 = a data
    counter, not a version) fragmented into hundreds of keys and starved the NR
    target codes."""
    inv = kdd.DiagInventory(max_codes=512, max_versions_per_code=4)
    # 0x117E-like code: byte-0 spans 0..99 (data, not a version).
    for v in range(100):
        inv.record(0x117E, v, True, float(v))
    # A genuine target code arrives AFTER the flood -- must still be admitted.
    inv.record(0xB97F, 0, True, 200.0)
    s = inv.summary(201.0)
    versions_117e = [k for k in s["keys"] if k["code"] == "0x117E"]
    assert len(versions_117e) == 4, "per-code version budget caps the byte-0 flood"
    assert inv.overflow == 96, "the 96 excess 0x117E versions are counted, not stored"
    assert any(k["code"] == "0xB97F" for k in s["keys"]), \
        "a real code after the flood is NOT starved by the capped one"


# The wardrive mask preset the live celldiag source subscribes to (mirrors
# DIAG_TARGET_CODES in capture_cell_diag/diag_config.c): the LTE + NR cell
# codes plus the GNSS position codes. Byte-0 is the version field on all
# of these -- they are the version-gated measurement/RRC/GNSS codes -- so the
# (code, byte-0) census key is exactly right for them.


# ---- GNSS gps_fix emit -----------------------------------------------------

import math  # noqa: E402

from diaggrok.parsers.diag_0x1476 import (  # noqa: E402
    _POS_FIELDS as _GNSS_POS_FIELDS,
    _POS_FMT as _GNSS_POS_FMT,
)


def _gnss_0x1476_payload(lat_deg, lon_deg, alt_m, version=2, gps_week=0):
    """Build a minimal valid v1/v2 0x1476 GNSS Position Report payload with the
    given position (lat/lon are stored as radians in the wire format)."""
    vals = {f: 0 for f in _GNSS_POS_FIELDS}
    vals["version"] = version
    vals["gps_week"] = gps_week
    vals["lat_rad"] = math.radians(lat_deg)
    vals["lon_rad"] = math.radians(lon_deg)
    vals["alt_m"] = alt_m
    return struct.pack(_GNSS_POS_FMT, *[vals[f] for f in _GNSS_POS_FIELDS])


def _feed_one(log_code, payload, clock_val=1700000000.0):
    """Feed a single record through _LogEmitter and return the emitted JSON
    lines (as parsed dicts)."""
    out = io.StringIO()
    em = kdd._LogEmitter(_SYNTH_IMEI, out, {}, clock=lambda: clock_val)
    em.feed(log_code, 12345, payload)
    return [json.loads(ln) for ln in out.getvalue().splitlines() if ln]


def test_gps_fix_valid_position_emits_gps_fix_line():
    """A decoded 0x1476 with a real position emits one gps_fix record with
    lat/lon/alt and a pre-rendered status one-liner (the C relay contract)."""
    # A plausible, non-placeholder location.
    payload = _gnss_0x1476_payload(28.6, -57.3, 1400.0)
    recs = _feed_one(0x1476, payload)
    assert len(recs) == 1, "exactly one gps_fix line"
    r = recs[0]
    assert r["type"] == "gps_fix"
    assert abs(r["lat"] - 28.6) < 0.001
    assert abs(r["lon"] - (-57.3)) < 0.001
    assert abs(r["alt"] - 1400.0) < 1.0
    assert r["fix_source"] == "0x1476"
    # status is pre-rendered so the C binary never parses the record.
    assert r["status"].startswith("celldiag GPS fix 28.6")
    # It is NOT a cell_observation -- no cell fields leak in.
    assert "pci" not in r and "earfcn" not in r


def test_gps_fix_zero_zero_placeholder_suppressed():
    """A (0,0) 'value unavailable' sentinel must NOT emit a fake fix -- a no-fix
    engine can't be allowed to anchor Kismet at null island."""
    payload = _gnss_0x1476_payload(0.0, 0.0, 0.0)
    assert _feed_one(0x1476, payload) == []


def test_gps_fix_nevada_placeholder_suppressed():
    """The Qualcomm Nevada default (38.0, -117.0) is a known no-fix sentinel."""
    payload = _gnss_0x1476_payload(38.0, -117.0, 0.0)
    assert _feed_one(0x1476, payload) == []


def test_gps_fix_week_unknown_suppressed():
    """gps_week == 0xFFFF means the receiver has no time solution, and a
    GNSS position is a time-of-arrival solution -- so any lat/lon riding along
    is a seed, not a fix.

    The position here is deliberately plausible (not a known sentinel, |lat|
    and |lon| both > 1), because every coordinate-based gate passes it. An
    RM520N-GL camped on NR5G-SA with no fix in its own NMEA reports exactly
    this shape: a constant, plausible position far from the true site."""
    payload = _gnss_0x1476_payload(40.221638, -109.778721, 0.0,
                                   gps_week=0xFFFF)
    assert _feed_one(0x1476, payload) == []


def test_gps_fix_valid_week_still_emits():
    """The negative control for the gate above: the SAME plausible position with
    a real week number must still emit. A gate that rejected both would look
    identical on the defect capture and silently kill GNSS everywhere else."""
    payload = _gnss_0x1476_payload(40.221638, -109.778721, 0.0, gps_week=2429)
    recs = _feed_one(0x1476, payload)
    assert len(recs) == 1 and recs[0]["type"] == "gps_fix"


def test_gps_fix_week_gate_does_not_key_on_pos_source():
    """Regression guard: pos_source is not the discriminator.

    pos_source == 4 (DB) reads as "database position" and looks like the obvious
    discriminator -- but it is 4 on the bogus SDX62 v24 records and also on
    correct SDX55 v13 / SDX20 v10 records captured alongside them. Gating on it
    would silently kill GNSS on two working chipsets. This pins
    that pos_source alone never suppresses a fix."""
    payload = _gnss_0x1476_payload(28.6, -57.3, 1400.0, gps_week=2429)
    # pos_source is byte 5 of the _POS_FMT header.
    payload = payload[:5] + bytes([4]) + payload[6:]
    recs = _feed_one(0x1476, payload)
    assert len(recs) == 1, "pos_source=4 must NOT suppress a time-valid fix"


# ── census: `emitted` vs `decoded` ───────────────────────────────────────────
#
# `decoded` means "the parser produced a result". That answers "did the parser
# understand this?", but the census exists to answer "is data flowing?", and
# the two can differ enormously: on a typical RM520N-GL capture nearly every
# record is `decoded` and only a handful are emitted.
#
# The sharpest case is 0x14D8, in the mask as a backup GNSS source. It parses
# on every record and emits a gps_fix on none -- its lat/lon/alt are hardcoded
# 0.0 in the reference parser, so the validity gate rejects every one. Under a
# decoded-only census that reads as healthy. The `emitted` counter plus the
# contract-gated `silent` flag is what makes it visible.

def _inv_feed(records, inv):
    """Feed (log_code, ts64, payload) tuples through a _LogEmitter with a
    deterministic clock so the census is reproducible."""
    import itertools
    out = io.StringIO()
    ticks = itertools.count(0.0, 0.001)
    em = kdd._LogEmitter("0", out, {}, clock=lambda: next(ticks),
                         inventory=inv, inv_interval=0)
    for code, ts64, payload in records:
        em.feed(code, ts64, payload)
    return out


def _key(summary, code, ver):
    for k in summary["keys"]:
        if k["code"] == f"0x{code:04X}" and k["ver"] == ver:
            return k
    raise AssertionError(f"no census key for 0x{code:04X}/v{ver}")


def test_0x14d8_is_fully_decoded_and_emits_nothing():
    """The finding, pinned. A well-formed 0x14D8 parses every time and yields
    nothing -- so a decoded-only census reports it as healthy."""
    payload = struct.pack("<IIIII", 1, 0, 0, 0, 0)
    inv = kdd.DiagInventory()
    out = _inv_feed([(0x14D8, 0, payload)] * 5, inv)
    k = _key(inv.summary(1.0), 0x14D8, 1)
    assert k["count"] == 5
    assert k["decoded"] == 5, "0x14D8 parses fine -- that was never in doubt"
    assert k["emitted"] == 0, "...and emits nothing; if this changes, the " \
                              "no-0x14D8-leg decision must be revisited"
    assert k["silent"] is True
    assert out.getvalue() == "", "no gps_fix line should have been written"


def test_emitted_counts_the_actual_downstream_yield():
    """A 0x1476 record that clears the validity gate counts as emitted; the same
    record at a vendor placeholder does not. Both are `decoded`."""
    good = _gnss_0x1476_payload(28.6, -57.3, 100.0)
    nevada = _gnss_0x1476_payload(38.0, -117.0, 100.0)   # Qualcomm placeholder
    inv = kdd.DiagInventory()
    _inv_feed([(0x1476, 0, good), (0x1476, 0, nevada)], inv)
    k = _key(inv.summary(1.0), 0x1476, good[0])
    assert (k["count"], k["decoded"], k["emitted"]) == (2, 2, 1)
    assert k["silent"] is False, "one real fix means the code is not silent"


def test_silent_is_gated_to_the_emit_contract():
    """A flag that is always on is not a signal.

    A wardriving capture is mostly codes diaggrok parses and Kismet does not
    consume -- ungated, `silent` fires on nearly every key. It is therefore
    restricted to the codes the emit contract expects output from."""
    assert 0x14D8 in kdd._EMIT_CONTRACT_CODES
    assert 0x1476 in kdd._EMIT_CONTRACT_CODES
    # 0x1874 is a high-volume non-contract code. It parses and emits nothing,
    # and that is not a finding.
    assert 0x1874 not in kdd._EMIT_CONTRACT_CODES


def test_a_record_that_emits_nothing_is_still_counted():
    """Every return path in feed() must tally.

    The census records after the emit (it needs the yield), so an early
    return that skipped the tally would drop a whole code from the census while
    the stream looked healthy."""
    inv = kdd.DiagInventory()
    # A payload no parser accepts: dropped, but present in the census.
    _inv_feed([(0xB192, 0, b"\x00")], inv)
    k = _key(inv.summary(1.0), 0xB192, 0)
    assert (k["count"], k["decoded"], k["dropped"], k["emitted"]) == (1, 0, 1, 0)
    assert k["new"] is True, "never parsed at all -- distinct from `silent`"
    assert k["silent"] is False, "`silent` means understood-and-yielding-zero"


# ── `silent` as a top-level list, consumed by the C relay ─────────────────────
# `silent` is the field that answers the operator's actual question, so it is
# surfaced as a top-level list the C relay can act on directly, not only as a
# per-key boolean that only a web UI would render.

def test_silent_is_a_top_level_list_symmetric_with_unrecognized():
    """`silent` is reported as a top-level key list, not only per-key.

    The C relay must decide INFO-vs-ERROR without walking the key table -- it has a
    narrow string extractor, not a JSON parser. `unrecognized` was already shaped
    for that; `silent` now matches, so both are one cheap substring test.
    """
    inv = kdd.DiagInventory()
    # 0xB193 is an emit-contract code. Parsed (decoded) but emitting nothing.
    inv.record(0xB193, 36, True, 1.0, emitted=0)
    summary = inv.summary(2.0)
    assert summary["silent"] == ["0xB193/v36"]
    # ...and the per-key boolean is still there: the list says WHICH keys, the
    # boolean lets a table row render its own flag without a lookup.
    assert _key(summary, 0xB193, 36)["silent"] is True


def test_silent_serialization_contract_matches_the_c_relay():
    """Pin the exact `"silent":[]` sentinel the C side keys on.

    Same contract as `unrecognized`, and the same reason to pin it: the C test is a
    substring, so any change to separators or key name silently disables the
    escalation rather than failing.
    """
    sep = (",", ":")

    clean = kdd.DiagInventory()
    clean.record(0xB193, 36, True, 1.0, emitted=2)
    assert '"silent":[]' in json.dumps(clean.summary(2.0), separators=sep)

    quiet = kdd.DiagInventory()
    quiet.record(0xB193, 36, True, 1.0, emitted=0)
    line = json.dumps(quiet.summary(2.0), separators=sep)
    assert '"silent":[]' not in line
    assert '"silent":["0xB193/v36"]' in line


def test_silent_list_is_gated_to_the_emit_contract_like_the_flag():
    """The list inherits the flag's gate, or the C relay escalates permanently.

    Ungated, `silent` fires on nearly every key of a wardriving capture -- and
    since the C side escalates the whole census line to ERROR on a non-empty list,
    an ungated list would make EVERY inventory line an ERROR. A severity that is
    always on is worse than none: it trains the operator to ignore the line that
    exists to be noticed.
    """
    inv = kdd.DiagInventory()
    inv.record(0x1874, 1, True, 1.0, emitted=0)   # high-volume NON-contract code
    assert inv.summary(2.0)["silent"] == []
    assert '"silent":[]' in json.dumps(inv.summary(2.0), separators=(",", ":"))


def test_status_line_names_both_findings_and_they_are_different():
    """The one-liner carries both counts and both key lists.

    `unrecognized` and `silent` are DIFFERENT findings and a status line reporting
    only one is misleading: an unrecognized key is new firmware the parsers do not
    handle yet (expected on a new part), whereas a silent key is a code Kismet
    DEPENDS on going quiet -- which on a live source is indistinguishable from "no
    cells in range". The C relay surfaces this string verbatim, so it is where
    an operator sees both.
    """
    inv = kdd.DiagInventory()
    inv.record(0xB193, 36, True, 1.0, emitted=0)   # contract code, silent
    inv.record(0xFFFF, 0, False, 1.0)              # never decoded, unrecognized
    status = inv.summary(2.0)["status"]
    assert "1 unrecognized" in status
    assert "1 silent" in status
    assert "unrecognized[0xFFFF/v0]" in status
    assert "silent[0xB193/v36]" in status


# ---------------------------------------------------------------------------
# --replay and the capture gate.
#
# A plain read of a compressed capture does not fail on its own:
# `detect_format` returns "hdlc" for any buffer with four 0x7E in its first
# 64 KB, and the HDLC walker can then yield records synthesized from compressed
# noise that happens to pass CRC16, ending in 0 observations, exit 0 and
# nothing on stderr. `is_probably_capture` rejects that input correctly, so
# `--replay` gates on it.
# ---------------------------------------------------------------------------

import gzip  # noqa: E402

import pytest  # noqa: E402


# ---------------------------------------------------------------------------
# The per-record guard must cover the whole per-record pipeline
#
# Everything that runs on a record that parsed fine -- `update_sib1_map`,
# `_write_gps_fix`, and the `result_to_observations` generator (whose body
# executes inside the for-loop) -- must be inside the guard, not just
# `diaggrok.parse()`. Otherwise a payload that decodes cleanly but enriches
# badly kills the helper mid-stream, and the C side reports `helper=dead`:
# obs=0, which is indistinguishable from "no cells in range".
# ---------------------------------------------------------------------------

def _three_b193_records():
    """Three identical, perfectly-decodable 0xB193 records."""
    payload = _b193_payload_bytes()
    return [(0xB193, 100 + i, payload) for i in range(3)]


def _b193_payload_bytes():
    # version=1, then the v1 body 0xB193 needs. Reuse the real serializer used by
    # the HDLC tests rather than hand-rolling a second one.
    return _real_b193_payload()


def _real_b193_payload():
    """A byte payload that ``diaggrok.parse(0xB193, ...)`` decodes."""
    return _synthetic_b193_payload()


def test_enrich_failure_does_not_kill_the_stream(monkeypatch):
    """Negative control for the per-record guard.

    A record that parses cleanly and then raises while building observations
    must be swallowed like a parse error -- the tap keeps running. Unguarded,
    this raises out of feed() -> _emit_records() -> main() and the helper
    dies on record 2 of 3.
    """
    records = _three_b193_records()
    calls = {"n": 0}
    real = kdd.result_to_observations

    def boom(log_code, result, imei, sib1_map, captured_at, log_tick=0.0):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("synthetic enrichment defect")
        return real(log_code, result, imei, sib1_map, captured_at,
                    log_tick=log_tick)

    monkeypatch.setattr(kdd, "result_to_observations", boom)
    out = io.StringIO()
    kdd._emit_records(records, _SYNTH_IMEI, out, {}, clock=lambda: 1.0)

    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    obs = [x for x in lines if x.get("type") != "diag_inventory"]
    # records 1 and 3 still produced output: the stream survived record 2.
    assert calls["n"] == 3, "the emitter stopped feeding after the raise"
    assert len(obs) >= 2


def test_enrich_failure_is_counted_distinctly_from_a_decode_drop(monkeypatch):
    """A distinct counter, not a fold into `dropped`.

    Folding "parsed but failed to enrich" into `dropped` would report an
    enrichment defect as a decode gap and point at the wrong layer. They are different bugs in different code.
    """
    records = _three_b193_records()

    def boom(*a, **kw):
        raise ValueError("synthetic enrichment defect")

    monkeypatch.setattr(kdd, "result_to_observations", boom)
    # wire_keys=True: these assertions read the per-key census table off
    # the emitted line, and `keys` is opt-in on the wire.
    inv = kdd.DiagInventory(wire_keys=True)
    out = io.StringIO()
    kdd._emit_records(records, _SYNTH_IMEI, out, {}, clock=lambda: 1.0,
                      inventory=inv, inv_interval=0)

    summary = json.loads(out.getvalue().splitlines()[-1])
    assert summary["type"] == "diag_inventory"
    key = [k for k in summary["keys"] if k["code"] == "0xB193"][0]
    assert key["count"] == 3
    # It PARSED -- so it is decoded, not dropped. That distinction is the point.
    assert key["decoded"] == 3
    assert key["dropped"] == 0
    assert key["enrich_failed"] == 3
    assert key["emitted"] == 0
    # ...and surfaced at the top level, symmetric with `unrecognized`/`silent`,
    # so the C relay can escalate on it with one substring test.
    assert summary["enrich_failed"] == ["0xB193/v1"]
    assert "3 enrich-failed" in summary["status"] or "enrich_failed" in summary["status"]


def test_enrich_failure_in_the_sib1_learn_is_guarded(monkeypatch):
    """`update_sib1_map` runs on every non-GNSS record, after the parse."""
    records = _three_b193_records()

    def boom(*a, **kw):
        raise KeyError("synthetic sib1 defect")

    monkeypatch.setattr(kdd, "update_sib1_map", boom)
    # wire_keys=True: these assertions read the per-key census table off
    # the emitted line, and `keys` is opt-in on the wire.
    inv = kdd.DiagInventory(wire_keys=True)
    out = io.StringIO()
    kdd._emit_records(records, _SYNTH_IMEI, out, {}, clock=lambda: 1.0,
                      inventory=inv, inv_interval=0)
    summary = json.loads(out.getvalue().splitlines()[-1])
    key = [k for k in summary["keys"] if k["code"] == "0xB193"][0]
    assert key["enrich_failed"] == 3 and key["dropped"] == 0


def test_enrich_failure_in_the_gnss_write_is_guarded(monkeypatch):
    """The GNSS branch returns early, so it needs its own coverage -- an early
    return that skipped the guard would leave GNSS enrichment unguarded."""
    payload = _gnss_0x1476_payload(28.6, -57.3, 1400.0)
    records = [(0x1476, 1, payload)]

    def boom(*a, **kw):
        raise ZeroDivisionError("synthetic gnss defect")

    monkeypatch.setattr(kdd, "_write_gps_fix", boom)
    # wire_keys=True: these assertions read the per-key census table off
    # the emitted line, and `keys` is opt-in on the wire.
    inv = kdd.DiagInventory(wire_keys=True)
    out = io.StringIO()
    kdd._emit_records(records, _SYNTH_IMEI, out, {}, clock=lambda: 1.0,
                      inventory=inv, inv_interval=0)
    summary = json.loads(out.getvalue().splitlines()[-1])
    key = [k for k in summary["keys"] if k["code"] == "0x1476"][0]
    assert key["enrich_failed"] == 1 and key["dropped"] == 0


def test_partial_emit_before_a_raise_is_still_counted_as_emitted(monkeypatch):
    """`result_to_observations` is a GENERATOR: a record can write two lines and
    then raise on the third. The lines that reached the pipe must be counted --
    otherwise `emitted` under-reports and the `silent` flag fires on a code that
    is demonstrably producing data."""
    records = [_three_b193_records()[0]]
    real = kdd.result_to_observations

    def partial(log_code, result, imei, sib1_map, captured_at, log_tick=0.0):
        for i, o in enumerate(real(log_code, result, imei, sib1_map,
                                   captured_at, log_tick=log_tick)):
            yield o
        raise RuntimeError("synthetic defect after the last yield")

    monkeypatch.setattr(kdd, "result_to_observations", partial)
    # wire_keys=True: these assertions read the per-key census table off
    # the emitted line, and `keys` is opt-in on the wire.
    inv = kdd.DiagInventory(wire_keys=True)
    out = io.StringIO()
    kdd._emit_records(records, _SYNTH_IMEI, out, {}, clock=lambda: 1.0,
                      inventory=inv, inv_interval=0)
    summary = json.loads(out.getvalue().splitlines()[-1])
    key = [k for k in summary["keys"] if k["code"] == "0xB193"][0]
    assert key["enrich_failed"] == 1
    assert key["emitted"] > 0, "observations that reached the pipe were not counted"


def test_the_guard_docstring_matches_the_guard():
    """A guard sentence broader than its guard hides an unguarded path. Pin
    the two together."""
    doc = kdd._emit_records.__doc__
    assert "parse" in doc and "enrich" in doc.lower()


# ---------------------------------------------------------------------------
# Shutdown is a normal event, not an exception
# ---------------------------------------------------------------------------

class _BrokenOut:
    def write(self, *a):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


# ---------------------------------------------------------------------------
# An exit-status contract, and a --version the C side can log
# ---------------------------------------------------------------------------

def test_version_flag_reports_something_the_c_side_can_log(capsys):
    with pytest.raises(SystemExit) as e:
        kdd.main(["--version"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "kismet_diag_decode" in out
    assert kdd.__version__ in out
    # A stale decoder root must be diagnosable from the Kismet log, so the
    # diaggrok it decodes with is named too.
    assert "diaggrok" in out


def test_exit_codes_are_enumerated_in_the_module_docstring():
    """An exit-status contract nobody wrote down is not a contract."""
    doc = kdd.__doc__
    for code in (kdd.EXIT_OK, kdd.EXIT_USAGE, kdd.EXIT_FAULT,
                 kdd.EXIT_INTERRUPTED):
        assert f"{code}" in doc, f"exit code {code} is undocumented"


def test_bad_arguments_exit_with_the_usage_code(capsys):
    with pytest.raises(SystemExit) as e:
        kdd.main([])          # --imei is required
    assert e.value.code == kdd.EXIT_USAGE


# ---------------------------------------------------------------------------
# The withholding gates, and where they are documented
# ---------------------------------------------------------------------------

def test_the_module_docstring_points_at_the_gates_new_home():
    """The gate table deliberately does not live in this file.

    The gates live in diaggrok, and a second copy of their table here would
    drift from the code it describes. So what this file must carry is a
    working pointer plus the standing rule itself, and that is what is
    asserted.

    The rule is restated here rather than only cross-referenced because this file
    still renders records, and a renderer that forgets "absent and zero must not
    look alike" reintroduces the defect at the wire even with every gate intact.
    """
    doc = kdd.__doc__
    for pointer in ("diaggrok.gnss_gate", "diaggrok.observation"):
        assert pointer in doc, f"the gates' home {pointer!r} is not named"
    assert "never render" in doc, "the absent-vs-zero rule is not restated"
    assert "_is_placeholder_position" not in doc, (
        "the gate table came back into this docstring -- it belongs at the "
        "gates' home in diaggrok, in one copy")


def test_a_closed_pipe_is_not_counted_as_an_enrichment_failure():
    """Regression guard for the per-record guard vs. shutdown interaction.

    The per-record guard covers the observation write, and `BrokenPipeError`
    subclasses OSError -> Exception. Caught with everything else, a normal
    shutdown would become a per-record "parsed but failed to enrich" warning
    while the emitter kept feeding records into a pipe with no reader.
    The consumer going away is main()'s business, not the guard's.
    """
    records = _three_b193_records()
    inv = kdd.DiagInventory()
    with pytest.raises(BrokenPipeError):
        kdd._emit_records(records, "3566", _BrokenOut(), {}, clock=lambda: 1.0,
                          inventory=inv, inv_interval=0)
    assert inv.enrich_failed() == []


# ── the web-UI census table ──────────────────────────────────────────────────
def test_census_table_is_one_delimited_string_the_c_relay_can_forward():
    """The table travels as a STRING, not as the `keys` array beside it.

    The only consumer between here and the panel is a C relay that does not
    parse JSON — it lifts named string fields out with
    `diag_profile_json_string()`, which is how `status`, `mask_preset` and
    `rawlog_path` already travel. So the table is pre-rendered here.
    """
    inv = kdd.DiagInventory()
    # emitted=1 on purpose: 0xB193 is an EMIT-CONTRACT code, so decoding it with
    # emitted=0 is legitimately `silent` and the row would carry that flag. The
    # unflagged assertion below is about the '-' placeholder, so the fixture has
    # to be a genuinely healthy key.
    inv.record(0xB193, 1, True, 100.0, emitted=1)
    inv.record(0xB193, 1, True, 101.0, emitted=1)
    inv.record(0xB0C0, 27, True, 102.0, emitted=1)

    s = inv.summary(105.0)
    assert isinstance(s["table"], str)
    rows = s["table"].split(";")
    assert len(rows) == 2
    by = {r.split(",")[0]: r.split(",") for r in rows}
    assert by["0xB193/v1"][1] == "2", f"count wrong: {by['0xB193/v1']}"
    assert by["0xB193/v1"][2] == "2", "decoded wrong"
    assert by["0xB193/v1"][6] == "-", "an unflagged row must carry '-', not empty"
    # 7 fields exactly: code/ver, count, decoded, dropped, emitted,
    # enrich_failed, flags. A row the panel splits on a fixed arity is a row a
    # dropped counter breaks loudly instead of shifting every column silently.
    for r in rows:
        assert len(r.split(",")) == 7, f"row arity changed: {r!r}"


def test_census_table_needs_no_json_escaping():
    """The property that lets the C side forward it verbatim.

    Every character is from hex digits, decimal digits, ``x/v,;-`` and the flag
    alphabet. A quote or backslash reaching `diag_profile_json_string()` would
    put the relay's reader out of phase.
    """
    inv = kdd.DiagInventory()
    for code in (0xB193, 0xB0C0, 0x1476):
        inv.record(code, 7, True, 100.0)
    table = inv.summary(101.0)["table"]
    assert '"' not in table and "\\" not in table
    assert all(32 <= ord(c) < 127 for c in table), "non-printable byte in table"


def test_census_table_puts_FLAGGED_rows_first_even_when_they_are_rare():
    """The selection rule, and the reason it is not "top N by count".

    The rows worth a panel slot are the ones the census exists to surface — a
    contract code that parsed and emitted nothing, a (code,version) no parser
    handles. Those are by definition rare, so a plain top-by-count cut drops
    exactly them: the table would be busiest-and-healthiest, which reads as a
    clean bill of health.
    """
    inv = kdd.DiagInventory()
    # A loud, healthy key…
    for i in range(200):
        inv.record(0xB193, 1, True, 100.0 + i, emitted=1)
    # …and one quiet unrecognized key, seen once.
    inv.record(0xDEAD, 3, False, 300.0)

    s = inv.summary(400.0)
    rows = s["table"].split(";")
    assert rows[0].startswith("0xDEAD/v3"), (
        f"the flagged key is not first; a top-by-count table would have buried "
        f"it: {rows[:3]}")
    assert rows[0].endswith(",u"), f"flag not rendered: {rows[0]!r}"
    assert rows[1].startswith("0xB193/v1")


def test_census_table_truncation_is_REPORTED_not_silent():
    """A bounded table that looks complete is worse than no table.

    `distinct` rides the same line, so the panel can render "showing N of M".
    This pins that the two disagree when they should — i.e. that the cap is
    visible rather than presenting a partial census as the whole one.
    """
    inv = kdd.DiagInventory()
    n = kdd.DiagInventory.TABLE_ROWS + 10
    for i in range(n):
        inv.record(0xB000 + i, 1, True, 100.0 + i)

    s = inv.summary(500.0)
    assert s["distinct"] == n
    assert len(s["table"].split(";")) == kdd.DiagInventory.TABLE_ROWS
    assert s["distinct"] > len(s["table"].split(";")), (
        "the cap is invisible: distinct equals the row count, so a truncated "
        "table would render as complete")


def test_an_empty_census_produces_an_empty_table_not_a_bogus_row():
    """`"".split(";")` is `[""]` in both Python and JS — one empty row, not zero.

    The panel must gate on the string being empty, so this pins that a census
    with no keys produces exactly the empty string and not, say, a lone
    separator that would render as a blank table row.
    """
    assert kdd.DiagInventory().summary(1.0)["table"] == ""


# ---- the census wire form is bounded against JSON_LINE_MAX -----------------
#
# The census line must fit the C reader's JSON_LINE_MAX by construction, with
# a bound rather than a bigger buffer. These tests construct the census this
# class can produce at its own caps and assert the emitted line fits the
# reader's buffer, with JSON_LINE_MAX read out of the C source rather than
# copied, so the two cannot drift apart silently.

import re  # noqa: E402

# The C source is a sibling of this test's subject.
_CAPTURE_CELL_DIAG_C = HERE / "capture_cell_diag.c"


def _json_line_max():
    """The reader's line-buffer size, read from the C source it is defined in.

    Deliberately not a literal here. A copied 8192 would keep passing after
    someone changed the C side: two places holding the same number and only
    one of them being maintained.
    """
    import pytest

    if not _CAPTURE_CELL_DIAG_C.exists():  # pragma: no cover
        pytest.skip(f"capture source missing: {_CAPTURE_CELL_DIAG_C}")
    m = re.search(r"^#define\s+JSON_LINE_MAX\s+(\d+)",
                  _CAPTURE_CELL_DIAG_C.read_text(), re.M)
    assert m, "JSON_LINE_MAX not found in capture_cell_diag.c"
    return int(m.group(1))


def _worst_case_census():
    """The largest census this class can produce, built adversarially.

    Every dimension pushed at once: the distinct-code cap, the per-code version
    cap, every key unrecognized AND enrich-failed (so all three flag lists
    saturate), and counts large enough that the table's rows render at their
    widest. Emit-contract codes are included so `silent` is non-empty too.
    """
    inv = kdd.DiagInventory()
    big = 2 ** 63          # a count no real run reaches; widest decimal rows
    contract = sorted(kdd._EMIT_CONTRACT_CODES)
    # dict.fromkeys, not a set: `0xB000 + i` overlaps four of the contract
    # codes, and the duplicates cost distinct keys — the fixture then quietly
    # falls short of the cap it exists to reach (8,128 of 8,192 keys).
    codes = list(dict.fromkeys(
        contract + [0xB000 + i for i in range(inv.max_codes)]))[:inv.max_codes]
    t = 1000.0
    for code in codes:
        for ver in range(inv.max_versions_per_code):
            inv.record(code, ver, False, t, enrich_failed=True)
            t += 0.5
    # Widen the counters to the widest decimal rows without 2**63 record()
    # calls. `decoded` is set per flag, not uniformly: a key cannot be both
    # `unrecognized` (decoded == 0) and `silent` (decoded > 0, emitted == 0),
    # so the contract codes carry `silent` and every other key carries
    # `unrecognized`. Widening `decoded` everywhere — the obvious way to write
    # this — empties `unrecognized` and makes the size assertions vacuous.
    for k, e in inv.keys.items():
        for f in ("count", "dropped", "enrich_failed"):
            e[f] = big
        if k[0] in kdd._EMIT_CONTRACT_CODES:
            e["decoded"], e["emitted"] = big, 0     # silent
        else:
            e["decoded"], e["emitted"] = 0, big     # unrecognized
    return inv


def test_census_wire_line_fits_the_C_readers_JSON_LINE_MAX_at_the_caps():
    """The bound, derived from this class's own caps.

    Not "8 KB looked fine on one fixture": this is the census the producer is
    *capable* of producing.
    """
    limit = _json_line_max()
    inv = _worst_case_census()
    line = json.dumps(inv.summary(9999.0, wire=True), separators=(",", ":"))
    assert inv.summary(9999.0)["distinct"] == inv.max_codes * \
        inv.max_versions_per_code, "the fixture did not reach the caps"
    assert len(line) <= limit, (
        f"the worst-case census line is {len(line)} bytes against a "
        f"JSON_LINE_MAX of {limit} — it would be cut and the prefix processed "
        f"as a complete line")

    # One dimension the fixture cannot max out simultaneously: a key is
    # either `unrecognized` (decoded == 0) or `silent` (decoded > 0), so the
    # rendered rows are a few digits short of the widest possible. Charge the
    # difference analytically rather than leaving it as unexamined slack: the
    # worst-case row is 120 bytes.
    table_now = len(json.dumps({"table": inv.summary(9999.0)["table"]})) - 2
    widest = len('"table":"') + 1 + kdd.DiagInventory.TABLE_ROWS * 120
    assert len(line) - table_now + widest <= limit, (
        f"the line fits only because the table rows are not at their widest: "
        f"{len(line)} - {table_now} + {widest} > {limit}")


def test_dropping_keys_alone_would_NOT_have_bounded_the_census_line():
    """The discriminating test, and the reason WIRE_LIST_MAX exists.

    On a typical census `keys` is nearly the whole line, so omitting it looks
    like enough. It is not a bound: at the producer's caps the `unrecognized`
    array alone is many times the reader's buffer with `keys` already gone.

    This fails if someone "simplifies" the fix back to just omitting `keys`.
    """
    limit = _json_line_max()
    s = _worst_case_census().summary(9999.0)          # full form
    keys_only_dropped = {k: v for k, v in s.items() if k != "keys"}
    size = len(json.dumps(keys_only_dropped, separators=(",", ":")))
    assert size > limit, (
        f"expected dropping `keys` alone to still overrun JSON_LINE_MAX; got "
        f"{size} <= {limit}. If the flag lists became bounded elsewhere this "
        f"test is stale — but check that they did, rather than deleting it")
    assert len(json.dumps(s["unrecognized"])) > limit, (
        "the `unrecognized` array alone should exceed the reader's whole "
        "line buffer at the caps — that is the finding this pins")


def test_census_wire_line_puts_everything_the_C_relay_reads_FIRST():
    """Key order is load-bearing — pinned, not left to luck.

    The C relay reads `distinct`, the three `*_total` counts, `status` and
    `table`. All of them must precede the flag arrays, whose length grows with
    the data, so that a truncated line loses only fields the relay does not
    read.
    """
    s = _worst_case_census().summary(9999.0, wire=True)
    order = list(s)
    read_by_c = ["distinct", "unrecognized_total", "silent_total",
                 "enrich_failed_total", "status", "table"]
    grows = ["unrecognized", "silent", "enrich_failed"]
    last_read = max(order.index(k) for k in read_by_c)
    first_grow = min(order.index(k) for k in grows)
    assert last_read < first_grow, (
        f"a field that grows with the data precedes one the C relay reads: "
        f"{order}")


def test_census_totals_are_present_in_BOTH_forms_and_survive_the_cut():
    """A bounded array must never under-report the count that drives the
    escalation. The totals are the real lengths in both forms."""
    inv = _worst_case_census()
    full = inv.summary(9999.0)
    wire = inv.summary(9999.0, wire=True)
    for name in ("unrecognized", "silent", "enrich_failed"):
        total = f"{name}_total"
        assert full[total] == len(full[name]), \
            f"{total} disagrees with the full list it counts"
        assert wire[total] == len(full[name]), (
            f"{total} on the wire reports {wire[total]}, but there are "
            f"{len(full[name])} — a bounded array under-reporting its count")
        assert len(wire[name]) <= kdd.DiagInventory.WIRE_LIST_MAX
        assert len(wire[name]) < len(full[name]), (
            f"{name} was not actually cut, so this asserts nothing")


def test_wire_form_omits_keys_by_default_and_census_keys_puts_it_back():
    """`keys` is opt-in on the wire and unconditional in-process."""
    inv = kdd.DiagInventory()
    inv.record(0xB193, 1, True, 100.0, emitted=1)
    assert "keys" not in inv.summary(200.0, wire=True), \
        "`keys` rode the wire by default — it is ~97% of the line"
    assert "keys" in inv.summary(200.0), \
        "the in-process form lost `keys`; analysis and these tests read it"

    dbg = kdd.DiagInventory(wire_keys=True)
    dbg.record(0xB193, 1, True, 100.0, emitted=1)
    assert "keys" in dbg.summary(200.0, wire=True), \
        "--census-keys did not restore the key table"


def _census_lines(out):
    return [json.loads(x) for x in out.getvalue().splitlines()
            if x.strip() and json.loads(x).get("type") == "diag_inventory"]


def _assert_is_wire_form(c, n_extra=20):
    assert "keys" not in c, "the emit path sent the full object"
    assert len(c["unrecognized"]) == kdd.DiagInventory.WIRE_LIST_MAX, \
        f"flag list not cut: {len(c['unrecognized'])} entries"
    assert c["unrecognized_total"] == kdd.DiagInventory.WIRE_LIST_MAX + n_extra


def test_the_FINAL_census_line_is_the_wire_form_not_the_full_object():
    """The bound is worth nothing if the emit path sends the other one."""
    inv = kdd.DiagInventory()
    for i in range(kdd.DiagInventory.WIRE_LIST_MAX + 20):
        inv.record(0xB000 + i, 1, False, 100.0 + i)
    out = io.StringIO()
    kdd._emit_records(iter(()), _SYNTH_IMEI, out, {},
                      clock=lambda: 1700000000.0, inventory=inv,
                      inv_interval=0.0)
    census = _census_lines(out)
    assert census, f"no census emitted; got {out.getvalue()!r}"
    _assert_is_wire_form(census[-1])


def test_the_PERIODIC_census_line_is_the_wire_form_too():
    """A separate test because it is a separate call site, and this one is
    the live path: `finish()` fires once at shutdown, the interval timer fires
    every `--inventory-interval` seconds of a running capture.

    Every other census test runs with `inv_interval=0` and therefore exercises
    `finish()` alone, so without this test reverting only the periodic site to
    the full object would leave the suite green.
    """
    import itertools

    inv = kdd.DiagInventory()
    for i in range(kdd.DiagInventory.WIRE_LIST_MAX + 20):
        inv.record(0xB000 + i, 1, False, 100.0 + i)

    out = io.StringIO()
    # A clock that jumps past the interval on the first feed, so the PERIODIC
    # branch fires; the emitter is then dropped without finish() being called.
    ticks = itertools.count(1000.0, 60.0)
    em = kdd._LogEmitter("0", out, {}, clock=lambda: next(ticks),
                         inventory=inv, inv_interval=30.0)
    for code, ts64, payload in _three_b193_records():
        em.feed(code, ts64, payload)

    census = _census_lines(out)
    assert census, (
        f"the periodic census never fired, so this test asserts nothing about "
        f"the wire form; output={out.getvalue()[:400]!r}")
    _assert_is_wire_form(census[0])


# ---------------------------------------------------------------------------
# The gate/writer seam
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,lat,lon,alt,week", [
    ("accept_real_fix", 28.6, -57.3, 1400.0, 2429),
    ("reject_zero", 0.0, 0.0, 0.0, 2429),
    ("reject_nevada", 38.0, -117.0, 0.0, 2429),
    ("reject_telit", 5.5, 6.6, 0.0, 2429),
    ("reject_noweek", 40.221638, -109.778721, 0.0, 0xFFFF),
])
def test_the_writer_holds_no_gates_of_its_own(name, lat, lon, alt, week):
    """``_write_gps_fix`` emits iff ``gps_fix_for`` accepts -- on every case.

    The writer writes rather than returns, so an oracle cannot call it; an
    oracle that re-assembles the decision from helpers will drift from it
    whenever a gate is added. So the decision lives in
    ``diaggrok.gnss_gate.gps_fix_for`` and this test pins the remaining risk:
    a gate added to the writer, below the function any oracle can see. Both
    directions are checked: an emit with no acceptance, or an acceptance with
    no emit, fails here.

    The gate is the decoder library's and the writer is this tree's, which is
    why the test belongs here: a seam is asserted from the side that can drift.

    If this ever fails, do not "fix" it by teaching an oracle the new gate.
    Move the gate into ``gps_fix_for``, where the oracle and the C++ leg can
    both see it.
    """
    from diaggrok.gnss_gate import gps_fix_for
    from diaggrok.parsers.diag_0x1476 import parse_0x1476

    payload = _gnss_0x1476_payload(lat, lon, alt, gps_week=week)
    result = parse_0x1476(0, payload)
    assert result is not None, f"{name}: fixture did not parse"

    accepted = gps_fix_for(0x1476, result) is not None
    out = io.StringIO()
    wrote = kdd._write_gps_fix(0x1476, result, "0", out, clock=lambda: 0.0)

    assert wrote == accepted, (
        f"{name}: _write_gps_fix returned {wrote} but gps_fix_for "
        f"{'accepted' if accepted else 'declined'} -- the writer is deciding "
        "something no oracle can see")
    assert bool(out.getvalue()) == accepted, (
        f"{name}: emitted output disagrees with the decision")


def test_0x14d8_can_never_emit_a_fix():
    """0x14D8 is not a usable backup position source.

    Its parser hardcodes lat/lon/alt to 0.0 -- the bytes are unbound carryover
    and no known firmware populates them -- so 0.0 fails the gate's
    ``1 < |lat| < 90`` bound and this branch of the writer is unreachable in
    its emitting form.

    If a future parser revision does bind those bytes, this fails and the
    "0x14D8 is a backup source" claim becomes worth revisiting.
    """
    from diaggrok.gnss_gate import gps_latlonalt
    from diaggrok.parsers.diag_0x14d8 import parse_0x14d8

    # A well-formed 20 B short-form record and a 104 B long-form one, both
    # passing the version gate.
    for size in (20, 104):
        payload = bytearray(size)
        struct.pack_into("<IIIII", payload, 0, 1, 0, 0, 0, 0)
        result = parse_0x14d8(0, bytes(payload))
        assert result is not None, f"{size}B 0x14D8 should parse"
        lat, lon, alt = gps_latlonalt(0x14D8, result.to_dict())
        assert (lat, lon, alt) == (0.0, 0.0, 0.0), \
            f"0x14D8 {size}B now carries position -- revisit the no-leg decision"
        out = io.StringIO()
        assert kdd._write_gps_fix(0x14D8, result, "0", out, clock=lambda: 0.0) is False
        assert out.getvalue() == ""
