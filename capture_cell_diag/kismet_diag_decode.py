"""Kismet cell DIAG bridge helper for the live DIAG datasource.

Reads length-prefixed HDLC-deframed DIAG LOG_F frames on stdin (or --replay
<file>), decodes them with the diaggrok decoder library, and emits
cell_observation JSON lines on stdout for the capture_cell_diag binary to relay
to Kismet via cf_send_json().

This file contains no parsers, by design.

Every log-code decoder lives in ``diaggrok``, which this file imports and which
``make -f standalone.mk deps`` fetches at a pinned ref into ``.deps/diaggrok``.
The library declares ``dependencies = []``, so its ``src/`` on ``PYTHONPATH``
under the system ``python3`` is the whole install: no virtualenv, no wheels,
no resolver, no network at run time. Do not add a third-party import here.

F3 (DIAG MSG/EXT_MSG diagnostic prints) relay is absent, not disabled.

There is no ``--f3`` flag and no ``f3=`` parameter. A defaulted-off flag would
advertise a capability this file does not have, and importing the F3 modules
at top level would make the import fail whenever they are unavailable.

The pipe boundary between the C capture binary and this helper lets in-C
parsers replace this process one log code at a time, behind the identical
(log_code, payload) -> cell_observation contract.

Exit-status contract. The C side logs this, so it is stable:

===== ============================================================================
   0  normal end of stream / replay complete. ALSO the reader closing the pipe
      (BrokenPipeError) and a clean ``--version``: Kismet or the C source going
      away is what every normal shutdown looks like, not a fault.
   1  refused a named bad input -- e.g. ``--replay`` pointed at something that
      is not a DIAG capture. The reason is on stderr; the refusal is deliberate
      (an empty decode is indistinguishable from "no cells in range", so for the
      one input class that can be proven wrong it says so rather than exit 0).
   2  usage / bad arguments (argparse).
  70  unexpected fault (``EX_SOFTWARE``) -- a bug in this helper. The traceback
      is on stderr. This is the code that means "investigate me"; 0 never does.
 130  interrupted (Ctrl-C / SIGINT), by the 128+signo convention.
===== ============================================================================

Observation-quality gates (what is withheld, on what test, and why) are
documented where they live, not here: ``diaggrok.gnss_gate`` for the GNSS
fix-validity gates and ``diaggrok.observation`` for the omit-never-zero rule on
signal and identity fields. They are decode, not transport, and a second copy
of that table here would drift from the code it describes.

The rule they all serve applies to this file too: *absent* and *zero* must
never render alike, because a healthy-looking zero hides a dead field. Each
gate has a test in diaggrok asserting the withhold, not merely the
pass-through.

The one gate still owned here is the census's: when no ``diag_inventory`` census
has arrived, the field is left untouched rather than zeroed. That is a wire
decision, so it stays with the wire.

Run the tests with the decoder library fetched and on the path::

    make -f standalone.mk deps
    PYTHONPATH=.deps/diaggrok/src python3 -m pytest tests/
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

#: Reported by ``--version`` so a stale bridge or decoder in a decoder root is
#: diagnosable from the Kismet log rather than by inspecting the filesystem.
#: Bump on any change to the emitted record schema.
__version__ = "1.0.0"

#: The exit-status contract, enumerated in the module docstring above. Named
#: constants rather than literals so the C side's expectations and this file's
#: cannot drift apart silently.
EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_FAULT = 70          # EX_SOFTWARE
EXIT_INTERRUPTED = 130   # 128 + SIGINT

import diaggrok
from diaggrok.dlf import is_probably_capture, iter_records
from diaggrok.hdlc import iter_log_records_stream

# The decode half of this helper lives in diaggrok: what a field means, which
# gate withholds a record, and what a (code, version) census counts are decoder
# facts, true whether or not Kismet exists. This file renders, moves bytes and
# honors the exit contract.
from diaggrok.gnss_gate import (
    GPS_DECLINE_NO_POSITION, GPS_DECLINE_NO_TIME, GPS_DECLINE_NOT_GNSS,
    GPS_DECLINE_PLACEHOLDER, gps_fix_for, gps_gate_verdict,
)
from diaggrok.inventory import EMIT_CONTRACT_CODES
from diaggrok.inventory import DiagInventory as _DiagInventoryCore
from diaggrok.observation import (
    provenance, result_to_observations, update_sib1_map,
)


#: The emit contract's code set, i.e. the Kismet data contract: the five
#: cell-measurement/identity codes plus the two GNSS codes. Used only to gate
#: the census's `silent` flag; it is not a filter on what gets parsed or counted.
#:
#: Re-exported rather than defined here because it is a decoder fact: exactly
#: the codes ``diaggrok.observation`` maps plus the ones ``diaggrok.gnss_gate``
#: gates. If the mapping grows a code, this set must grow with it, which only
#: works if there is one list.
_EMIT_CONTRACT_CODES = EMIT_CONTRACT_CODES


class DiagInventory(_DiagInventoryCore):
    """The ``diag_inventory`` wire record: the census, rendered for the C relay.

    Everything that counts lives in :class:`diaggrok.inventory.DiagInventory`:
    the (code, byte-0) key, the two-tier bound, the four counters, and the
    unrecognized / silent / enrich-failed findings. Those are decoder facts.
    This subclass only renders: hex formatting, rounding, the one-line status,
    the panel table, the JSON field order, and the length bounds that exist
    because the reader's buffer is finite. Add a counter to the base class;
    add a field here.

    The split is testable in one sentence: the base class does not know a wire
    exists, and this class computes nothing that is not already in
    ``snapshot()``.
    """

    def __init__(self, max_codes: int = 512, max_versions_per_code: int = 16,
                 wire_keys: bool = False):
        super().__init__(max_codes=max_codes,
                         max_versions_per_code=max_versions_per_code)
        # Whether the per-key `keys` array rides the wire form. Default off:
        # it is ~97% of the line and has no consumer between this helper and
        # the web-UI panel, which reads the bounded `table` instead. Kept as an
        # opt-in (`--census-keys`) because summary() is also read in-process
        # by analysis and by the tests, where the full key table is the point.
        #
        # It lives on THIS class, not the census, because "does this field fit
        # the reader's buffer" is a question only a wire can ask.
        self.wire_keys = wire_keys

    #: Entries carried in the three top-level flag arrays (``unrecognized``,
    #: ``silent``, ``enrich_failed``) on the wire form of the census.
    #:
    #: Without this bound the census line can far exceed the reader's
    #: ``JSON_LINE_MAX``. At this class's own caps (512 codes x 16 versions =
    #: 8,192 keys) ``unrecognized`` alone renders to about 100 KB, over 12x the
    #: reader's buffer even with ``keys`` omitted. Dropping ``keys`` is not
    #: enough; these three lists must be bounded too.
    #:
    #: An unbounded ``unrecognized`` is also the costliest list to lose to
    #: truncation: a cut inside it takes every later field with it, so the
    #: escalation about unrecognized keys would be lost exactly when there are
    #: the most of them.
    #:
    #: The arrays' elements have one consumer (the ``status`` one-liner, built
    #: in-process from the full lists, which names up to 8 of each). Their
    #: length has one consumer, the C relay, which is why the wire form also
    #: carries ``*_total`` integers: a bounded array must not under-report a
    #: count that drives an operator escalation.
    WIRE_LIST_MAX = 32

    def _status(self, unrec: list[tuple[int, int]],
                silent: list[tuple[int, int]] | None = None,
                enriched: list[tuple[int, int]] | None = None) -> str:
        """A one-line summary the C binary can surface verbatim (via the
        diag_inventory line's ``status`` field) without parsing the key table.

        Carries both counts. `silent` answers the operator's actual question
        (a contract code parsing fine and emitting nothing), so it is named
        here rather than left to the web UI.
        """
        silent = silent or []
        enriched = enriched or []
        base = (f"celldiag inventory: {len(self.keys)} (code,ver) key(s), "
                f"{len(unrec)} unrecognized, {len(silent)} silent")
        if enriched:
            # Only when nonzero: a permanently-present ", 0 enrich-failed" would
            # train the eye past it, and this is the line an operator scans.
            n = sum(self.keys[k]["enrich_failed"] for k in enriched)
            base += f", {n} enrich-failed"
        if self.overflow:
            base += f", {self.overflow} over cap"
        if unrec:
            shown = ", ".join(f"0x{c:04X}/v{v}" for c, v in unrec[:8])
            more = "" if len(unrec) <= 8 else f" +{len(unrec) - 8} more"
            base += f" unrecognized[{shown}{more}]"
        if silent:
            shown = ", ".join(f"0x{c:04X}/v{v}" for c, v in silent[:8])
            more = "" if len(silent) <= 8 else f" +{len(silent) - 8} more"
            base += f" silent[{shown}{more}]"
        if enriched:
            shown = ", ".join(f"0x{c:04X}/v{v}" for c, v in enriched[:8])
            more = "" if len(enriched) <= 8 else f" +{len(enriched) - 8} more"
            base += f" enrich_failed[{shown}{more}]"
        return base

    #: How many census rows the web-UI table carries. A wardriving capture
    #: routinely reaches 200+ distinct (code,version) keys, and shipping all of
    #: them every interval would cost far more than a panel can usefully render.
    #:
    #: The table cannot ride the ``diag_stats`` object. The worst-case row
    #: (u64 maxima, all three flags) is 120 bytes, so 24 rows is up to 2,880
    #: bytes, against a ``DIAG_STATS_EXTRA_MAX`` of 768 inside a ``STATUS_MAX``
    #: of 1024. Adding it there would trip the ``drop_path`` retry on every
    #: emit, which silently drops ``rawlog_path`` while the JSON still looks
    #: valid (the failure ``diag_stats.h`` warns about). The table needs its
    #: own control line.
    TABLE_ROWS = 24

    def _table(self, unrec, silent, enriched) -> str:
        """The per-key census as ONE delimited string, bounded to TABLE_ROWS.

        Why a string and not the ``keys`` array that is already on this line:
        the C relay does not parse JSON. It lifts named STRING fields out of the
        helper's output with ``diag_profile_json_string()`` and forwards them,
        which is how ``status``, ``mask_preset`` and ``rawlog_path`` already
        travel. Teaching it to walk a nested array — to move data it does not
        read — would put a JSON parser in the capture binary for the sake of the
        web UI. The panel splits this instead.

        Row: ``code/vN,count,decoded,dropped,emitted,enrich_failed,flags``
        Flags: any of ``s`` silent, ``u`` unrecognized, ``e`` enrich-failed,
        else ``-``. Rows joined by ``;``.

        Selection is flagged-first, then by count. The rows worth a panel slot
        are the ones the census exists to surface (a contract code that parsed
        and emitted nothing, a (code,version) no parser handles), and those are
        by definition rare, so a plain top-by-count cut would drop exactly them.

        The truncation is reported, not silent: ``distinct`` is on the same
        line, so the panel renders "showing N of M". A bounded table that looks
        complete is worse than no table.

        No character in a row can be a quote or a backslash (codes are hex,
        counts are integers, flags are from a fixed alphabet), so this needs no
        JSON escaping and the C side's string reader cannot be confused by it.
        """
        flagged = set(unrec) | set(silent) | set(enriched)

        def row_key(item):
            k, e = item
            return (0 if k in flagged else 1, -e["count"], k)

        rows = []
        for (code, ver), e in sorted(self.keys.items(), key=row_key)[:self.TABLE_ROWS]:
            flags = ""
            if (code, ver) in silent:
                flags += "s"
            if (code, ver) in unrec:
                flags += "u"
            if (code, ver) in enriched:
                flags += "e"
            rows.append(
                f"0x{code:04X}/v{ver},{e['count']},{e['decoded']},"
                f"{e['dropped']},{e['emitted']},{e['enrich_failed']},"
                f"{flags or '-'}")
        return ";".join(rows)

    def summary(self, now: float, *, wire: bool = False) -> dict:
        """The ``diag_inventory`` line: distinct from a ``cell_observation`` by
        its ``type`` field, so the C relay routes it to status/web-UI instead of
        Kismet. ``status`` is a pre-rendered one-liner for the CLI/bus.

        ``wire=True`` returns the bounded form, and is what the emit path
        sends. Two differences, both about fitting the reader's fixed
        ``JSON_LINE_MAX`` buffer rather than about what is true:

        * ``keys`` is omitted unless ``wire_keys`` was set. It is ~97% of the
          line and has no consumer downstream of this process.
        * the three flag arrays are cut to ``WIRE_LIST_MAX`` entries, and the
          full lengths travel beside them as ``unrecognized_total`` /
          ``silent_total`` / ``enrich_failed_total``.

        The ``*_total`` keys are present in both forms, never only the wire
        one. A count that appears only when the array is short is a count a
        reader can be written against and then silently lose at scale; a
        reader that always finds it cannot regress into counting elements.

        The default is ``wire=False``: the full object, for in-process
        analysis and for the tests. Truncating by default would make every
        caller pay for the pipe's limit.

        Every fact below comes from ``snapshot()``; this method only renders.
        A count, flag or rate computed here belongs in ``diaggrok.inventory``
        instead, or the census and the wire will disagree about what was
        observed."""
        snap = self.snapshot()
        unrec = snap["unrecognized"]
        silent = snap["silent"]
        enriched = snap["enrich_failed"]
        # Presentation applied here and nowhere else: the code as a hex string
        # for a human reading a panel, and the timestamps rounded to ms because
        # a JSON line does not need float64 tails. The census keeps both raw.
        keys = [dict(row, code=f"0x{row['code']:04X}",
                     first=round(row["first"], 3), last=round(row["last"], 3))
                for row in snap["keys"]]

        def _lst(items):
            """Render a flag list, cut to WIRE_LIST_MAX on the wire form only.
            The cut is never silent: the matching `*_total` always carries the
            real length, and `status` (built from the FULL lists) names the
            first 8 with a `+N more`."""
            out = [f"0x{c:04X}/v{v}" for c, v in items]
            return out[:self.WIRE_LIST_MAX] if wire else out

        # Key order is load-bearing: everything the C relay reads comes before
        # anything whose size grows with the data. Bounding the fields (above)
        # is the primary defence; this ordering means a future field that
        # grows, or a raised WIRE_LIST_MAX, degrades by losing something nobody
        # reads rather than by losing the escalation. Pinned by
        # `test_census_wire_line_field_order`.
        out = {
            "type": "diag_inventory",
            "ts": round(now, 3),
            "distinct": snap["distinct"],
            "overflow": snap["overflow"],
            # The three counts, ahead of the three arrays they count. The C
            # relay escalates on these: an integer that is always present
            # cannot be truncated into a different meaning, whereas a substring
            # test on a cut array can read it as flagged or, for
            # `enrich_failed`, as clean.
            "unrecognized_total": snap["unrecognized_total"],
            "silent_total": snap["silent_total"],
            # The count the C relay escalates on, beside (not instead of)
            # `silent_total`. They differ by the codes that cannot emit on any
            # capture (0x14D8 reports lat/lon/alt hardcoded to 0.0), so
            # escalating on `silent_total` would fire on every healthy source.
            # The census still shows the full silence; the alarm fires only on
            # the part an operator can act on.
            "silent_actionable_total": snap["silent_actionable_total"],
            "enrich_failed_total": snap["enrich_failed_total"],
            "status": self._status(unrec, silent, enriched),
            # The web-UI table, bounded and flagged-first. Its own field
            # rather than something derived from `keys` downstream, because the
            # only consumer between here and the panel is a C relay that does
            # not parse JSON -- see _table().
            "table": self._table(unrec, silent, enriched),
            "unrecognized": _lst(unrec),
            # Symmetric with `unrecognized`. The per-key `silent`
            # boolean in the key table stays -- the list says WHICH keys, the
            # boolean lets a table row render its own flag without a lookup.
            "silent": _lst(silent),
            # Third list, same shape and for the same reason as the two above:
            # "the mapping layer is broken on a record the parser understood".
            "enrich_failed": _lst(enriched),
        }
        # Last, because it is by far the largest and the only field with no
        # consumer outside this process.
        if not wire or self.wire_keys:
            out["keys"] = keys
        return out







def _emit_records(records, imei, out, sib1_map, clock=time.time,
                  inventory=None, inv_interval=30.0):
    """Parse each (log_code, ts64, payload) DIAG record and write any
    resulting cell_observation JSON lines.

    Per-record errors are swallowed so a passive tap survives edge-case
    payloads, and "per-record" means the WHOLE per-record pipeline -- parse,
    SIB1 learn, GNSS write, observation build and write -- not just the parse.
    The two failure classes are counted separately in the census: a record
    that never parsed is ``dropped`` (a diaggrok gap), one that parsed and then
    raised downstream is ``enrich_failed`` (a defect in this file). Folding
    them together would point at the wrong layer.

    If a step is added to the per-record path, it belongs inside
    ``_enrich_and_emit`` so the same guard covers it.

    ``captured_at`` is stamped from ``clock()`` (default wall-clock ``time.time``)
    at the moment the record is processed - on the live path this is within pipe
    latency of the modem emitting it, matching the gettimeofday stamp the C
    capture binary applies. The raw ts64 is carried through as ``log_tick`` (it
    is a since-boot counter, not a wall-clock). ``clock`` is injectable so
    tests get a deterministic captured_at.

    ``inventory``, when supplied, records each record's
    ``(log_code, byte-0 version)`` with a decoded/dropped tally and, every
    ``inv_interval`` seconds, writes a ``diag_inventory`` line to ``out``
    (distinct from ``cell_observation`` by its ``type`` field; the C binary
    routes it to status/web-UI, not Kismet). Default ``None`` emits no census
    lines, so callers and tests that don't opt in are unaffected."""
    em = _LogEmitter(imei, out, sib1_map, clock=clock,
                     inventory=inventory, inv_interval=inv_interval)
    for log_code, ts64, payload in records:
        em.feed(log_code, ts64, payload)
    em.finish()


class _LogEmitter:
    """Per-record LOG_F decode->cell_observation state, so a record stream can
    be fed incrementally without duplicating the parse/sib1/inventory logic.
    ``_emit_records`` is a thin wrapper over it."""

    def __init__(self, imei, out, sib1_map, clock=time.time,
                 inventory=None, inv_interval=30.0):
        self.imei = imei
        self.out = out
        self.sib1_map = sib1_map
        self.clock = clock
        self.inventory = inventory
        self.inv_interval = inv_interval
        self.last_inv = clock() if inventory is not None else 0.0
        # (code, version) keys already warned about by _warn_enrich, so a
        # repeating enrichment defect costs one log line, not one per record.
        self._enrich_warned: set[tuple[int, int]] = set()

    def feed(self, log_code, ts64, payload):
        try:
            result = diaggrok.parse(log_code, ts64, payload)
        except Exception:
            result = None
        # version = payload byte 0 (the diaggrok dispatch key); -1 when a record
        # carries no payload, so an empty frame is still counted.
        version = payload[0] if payload else -1
        # The census is recorded after the emit, not before: the `emitted`
        # counter needs the downstream yield, and only the emit path
        # knows it. `_tally` is called on every return path below -- a record that
        # parses and emits nothing must still be counted, or the census would
        # under-report exactly the codes it exists to surface.
        if result is None:
            self._tally(log_code, version, False, 0)
            return
        # The enrich guard. Without it, a None reaching arithmetic in the
        # mapping layer, an absent key in a SIB1 walk or a bad index would
        # raise out of feed() and main() and kill the helper mid-stream. The
        # C side would then report `helper=dead`, which presents as obs=0:
        # indistinguishable from "no cells in range".
        #
        # `emitted` is passed by reference (a one-element list) rather than
        # returned, because result_to_observations is a generator: a record can
        # write two observations and raise on the third, and the two that
        # reached the pipe must still be counted. A returned value is lost on the
        # raise, and the undercount would fire the `silent` flag on a code that
        # is demonstrably producing data.
        emitted = [0]
        try:
            self._enrich_and_emit(log_code, ts64, result, emitted)
        except BrokenPipeError:
            # Not an enrichment failure: the consumer went away. The guard
            # covers I/O, and BrokenPipeError subclasses OSError -> Exception,
            # so a bare `except Exception` would swallow a
            # normal shutdown and then keep feeding records into a dead pipe,
            # warning once per key on the way. (KeyboardInterrupt and SystemExit
            # need no such clause: they are BaseException, not Exception.)
            # main() owns this one; re-raise untouched.
            raise
        except Exception:
            self._warn_enrich(log_code, version)
            self._tally(log_code, version, True, emitted[0],
                        enrich_failed=True)
            return
        self._tally(log_code, version, True, emitted[0])

    def _enrich_and_emit(self, log_code, ts64, result, emitted):
        """Everything a successfully-parsed record does downstream. Split out of
        ``feed()`` so one ``try`` covers the whole of it; inline steps would
        each join an unguarded region by default."""
        # GNSS position: 0x1476 / 0x14D8 carry a fix, not a cell -- emit a
        # gps_fix record (the C side routes it by ``type``, like diag_inventory)
        # and stop; they never produce a cell_observation and carry no SIB1
        # identity to learn.
        if log_code in _GPS_CODES:
            if _write_gps_fix(log_code, result, self.imei, self.out,
                              clock=self.clock, log_tick=float(ts64 or 0.0)):
                emitted[0] += 1
            return
        # Learn identity from SIB1 before mapping measurements, so a serving
        # cell seen after its SIB1 in the same stream is emitted with identity.
        update_sib1_map(self.sib1_map, log_code, result)
        captured_at = self.clock()
        for obs in result_to_observations(
            log_code, result, self.imei, self.sib1_map, captured_at,
            log_tick=float(ts64 or 0.0),
        ):
            self.out.write(json.dumps(obs, separators=(",", ":")) + "\n")
            self.out.flush()
            emitted[0] += 1

    def _warn_enrich(self, log_code, version):
        """One stderr line the FIRST time a given (code, version) fails to
        enrich, then silence for that key.

        Unbounded warning on a live tap is its own denial of service -- a
        firmware emitting a bad record at 50 Hz would fill the Kismet log -- but
        a swallowed failure with no human-readable trace anywhere would hide
        the defect. The census carries the counts; this carries the
        traceback, once. The warned-set is capped for the same reason the census
        keyset is: a hostile stream must not grow it without bound."""
        k = (log_code, version)
        if k in self._enrich_warned or len(self._enrich_warned) >= 32:
            return
        self._enrich_warned.add(k)
        print(f"celldiag helper: 0x{log_code:04X}/v{version} parsed but failed "
              f"to enrich; record swallowed, stream continues "
              f"(further occurrences of this key are silent)",
              file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    def _tally(self, log_code, version, decoded, emitted,
               enrich_failed=False):
        """Record one fed record in the census, and flush a diag_inventory line
        if the interval has elapsed. Factored out so every return path in feed()
        counts -- an early return that skipped this would silently drop a whole
        code from the census while the stream looked healthy."""
        if self.inventory is None:
            return
        now = self.clock()
        self.inventory.record(log_code, version, decoded, now, emitted=emitted,
                              enrich_failed=enrich_failed)
        if self.inv_interval > 0 and now - self.last_inv >= self.inv_interval:
            # wire=True: the bounded form. The reader's line buffer is a fixed
            # JSON_LINE_MAX, and the full object exceeds it on any realistic
            # census.
            self.out.write(json.dumps(self.inventory.summary(now, wire=True),
                                      separators=(",", ":")) + "\n")
            self.out.flush()
            self.last_inv = now

    def finish(self):
        # Final inventory snapshot so a finished replay / stopped stream leaves a
        # complete (code,version) census on the pipe even if no interval elapsed.
        if self.inventory is not None:
            self.out.write(json.dumps(
                self.inventory.summary(self.clock(), wire=True),
                separators=(",", ":")) + "\n")
            self.out.flush()


# ---- GNSS position ---------------------------------------------------------
#
# 0x1476 (GNSS Position Report, primary) and 0x14D8 (GNSS ME Position Fix,
# backup) are in the capture mask
# (diag_config.c DIAG_TARGET_CODES); the helper turns each decoded fix into a
# ``gps_fix`` record -- distinct from a ``cell_observation`` by its ``type``
# field, so the C binary routes it (like ``diag_inventory``) instead of
# forwarding it to Kismet as a bogus cell. The lat/lon/alt field extraction
# includes the vendor "no fix" placeholder guard, so a GNSS engine with no
# antenna never emits a fake tower.

_GPS_CODES = (0x1476, 0x14D8)




def _write_gps_fix(log_code, result, imei, out, clock=time.time,
                   log_tick=0.0):
    """Emit a ``gps_fix`` record for a decoded 0x1476 / 0x14D8 GNSS result, or
    nothing if ``gps_fix_for`` declines it (a no-fix engine must not anchor
    Kismet at 0,0 or a vendor sentinel). ``status`` is pre-rendered so the C relay
    never parses the record. Returns True if a fix was emitted.

    Every gate lives in ``diaggrok.gnss_gate.gps_fix_for``; what remains here is
    rendering and I/O.
    """
    accepted = gps_fix_for(log_code, result)
    if accepted is None:
        return False
    lat, lon, alt = accepted
    now = clock()
    src = f"0x{log_code:04X}"
    status = (f"celldiag GPS fix {lat:.6f},{lon:.6f} "
              f"alt={alt:.0f}m src={src}")
    rec = {
        "type": "gps_fix",
        "ts": round(now, 3),
        "lat": round(float(lat), 6),
        "lon": round(float(lon), 6),
        "alt": round(float(alt), 1),
        "fix_source": src,
        "status": status,
        "prov": provenance("gps", imei, now, log_tick=log_tick),
    }
    out.write(json.dumps(rec, separators=(",", ":")) + "\n")
    out.flush()
    return True


def run_stream(chunks, imei, out, sib1_map=None, clock=time.time,
               inventory=None, inv_interval=30.0):
    """Live path: decode a raw HDLC DIAG byte stream (as the modem emits it).

    iter_log_records_stream handles HDLC framing, CRC, and the QShrink4 / QSR
    envelopes (0x98 / 0x99 / 0x92 / 0x80 / 0x9E) that SDX55/62/72 basebands wrap
    LOG_F records in -- so a bare-0x10 assumption is not made.
    `chunks` is any iterable of byte chunks (e.g. reads from the DIAG port).
    `clock` stamps prov.captured_at (see _emit_records); injectable for tests.
    `inventory` opt-in per-(code,version) census; see _emit_records.

    F3 relay is absent here, not defaulted off. See the module docstring."""
    if sib1_map is None:
        sib1_map = {}
    records = iter_log_records_stream(chunks, verify_crc=True)
    _emit_records(records, imei, out, sib1_map, clock=clock,
                  inventory=inventory, inv_interval=inv_interval)


def run_bytes(data, imei, out, sib1_map=None, clock=time.time,
              inventory=None, inv_interval=30.0):
    """Offline path: decode a whole capture buffer (flat-DLF or HDLC, auto
    detected). Used by --replay and the tests. `clock` stamps prov.captured_at
    (see _emit_records); injectable for tests. `inventory` opt-in
    per-(code,version) census; see _emit_records.

    F3 relay is absent here, not defaulted off. See the module docstring."""
    if sib1_map is None:
        sib1_map = {}
    _emit_records(iter_records(data), imei, out, sib1_map, clock=clock,
                  inventory=inventory, inv_interval=inv_interval)


def read_capture_file(path, log=sys.stderr):
    """Read a capture for ``--replay`` and prove it is a capture before
    handing bytes to the decoder.

    A plain read is not enough. ``diaggrok.dlf.detect_format`` falls back to
    ``hdlc`` when it finds four ``0x7E`` bytes anywhere in a 64 KB window,
    which almost any binary file satisfies, so it must not be used to decide
    whether a file is a capture at all. Handed a compressed capture, the HDLC
    walker can synthesize a few CRC-valid "records" from compressed noise and
    the replay ends with 0 observations, exit 0 and nothing on stderr: a
    confidently wrong result that looks like "no cells in range".

    So the input is gated on ``is_probably_capture``, which belongs to the
    decoder library, and anything that fails it is refused loudly.

    The file is read as-is; no container is unwrapped. A compressed capture
    therefore hits the refusal below and exits 1 with a named reason.
    Decompress before replaying.
    """
    from pathlib import Path

    data = Path(path).read_bytes()

    if not data:
        raise SystemExit(f"kismet_diag_decode: {path}: file is empty")

    if not is_probably_capture(data):
        raise SystemExit(
            f"kismet_diag_decode: {path}: not a DIAG capture.\n"
            "  No CRC-valid DIAG frame and no flat-DLF structure was found.\n"
            "  Refusing to decode rather than emitting 0 observations and\n"
            "     exiting 0 -- which is indistinguishable from a real capture\n"
            "     with no cells in it.\n"
            f"  First bytes: {data[:16].hex()}"
        )

    return data


def _stdin_chunks(fp, size=65536):
    # read1() returns whatever is already available (up to size) instead of
    # blocking for a full `size` buffer -- essential for a live pipe where the
    # modem trickles bytes; read() would deadlock until the buffer fills.
    reader = getattr(fp, "read1", None) or fp.read
    while True:
        chunk = reader(size)
        if not chunk:
            return
        yield chunk


def _silence_broken_stdout():
    """Point stdout at /dev/null so the interpreter's own flush-at-exit cannot
    re-raise the BrokenPipeError we just handled.

    Without this, CPython prints ``Exception ignored in: <_io.TextIOWrapper
    name='<stdout>'>`` plus a traceback during shutdown, and sets a nonzero exit
    status, after the clean return has already happened. The recipe is the one in the stdlib docs' BrokenPipeError
    note."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except Exception:
        pass


def main(argv=None):
    """Entry point. Returns the process exit status (see the module docstring's
    exit-status contract); never raises for a shutdown that is normal.

    Shutdown is an event, not an exception. Kismet or the C capture source
    closing the read end is what every normal stop looks like, so it must not
    print a traceback: a traceback on the happy path trains operators to
    ignore helper output, and then the real one gets missed."""
    try:
        return _run(argv)
    except BrokenPipeError:
        # The consumer went away. Normal.
        _silence_broken_stdout()
        return EXIT_OK
    except KeyboardInterrupt:
        _silence_broken_stdout()
        return EXIT_INTERRUPTED
    except SystemExit:
        # argparse (usage) and read_capture_file's deliberate refusal. Both
        # already carry their own status and message; do not reclassify them.
        raise
    except Exception:
        # A real bug in this helper. Loud, with a traceback, and under its OWN
        # exit code -- the C side must be able to tell "crashed" from "stream
        # ended", which a bare nonzero cannot.
        traceback.print_exc(file=sys.stderr)
        return EXIT_FAULT


def _run(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version", action="version",
                   version=f"kismet_diag_decode {__version__} "
                           f"(diaggrok {getattr(diaggrok, '__version__', '?')})",
                   help="print the helper + diaggrok versions and exit; the C "
                        "side logs this so a stale decoder is diagnosable from "
                        "the Kismet log")
    p.add_argument("--imei", required=True, help="modem IMEI, stamped into prov.imei")
    p.add_argument("--replay", help="decode a capture file instead of live stdin")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--inventory-interval", type=float, default=30.0,
                   help="seconds between diag_inventory (code,version) census "
                        "lines; 0 disables the periodic census")
    p.add_argument("--no-inventory", action="store_true",
                   help="disable the per-(code,version) inventory census entirely")
    p.add_argument("--census-keys", action="store_true",
                   help="carry the full per-key 'keys' array on the census "
                        "line. OFF by default: it is ~97%% of the line "
                        "and has no consumer downstream of this helper -- the "
                        "C relay forwards the bounded 'table' string "
                        "instead. Debug/analysis only; it will overrun the "
                        "reader's JSON_LINE_MAX on any real census")
    a = p.parse_args(argv)
    # Inventory is on by default in real runs (main); the unit tests call
    # run_bytes/run_stream directly without it, keeping their output clean.
    inventory = None if a.no_inventory else DiagInventory(
        wire_keys=a.census_keys)
    if a.replay:
        run_bytes(read_capture_file(a.replay), a.imei, sys.stdout,
                  inventory=inventory, inv_interval=a.inventory_interval)
    else:
        run_stream(_stdin_chunks(sys.stdin.buffer), a.imei, sys.stdout,
                   inventory=inventory, inv_interval=a.inventory_interval)
    return EXIT_OK


if __name__ == "__main__":
    # sys.exit(main()) -- NOT a bare main(). Without this the process exits 0
    # on every path including EXIT_FAULT, and the exit-status contract above is
    # a document describing something that does not happen.
    sys.exit(main())
