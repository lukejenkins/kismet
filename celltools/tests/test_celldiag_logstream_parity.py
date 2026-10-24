"""Parity harness: this tree's C HDLC->LOG-record extractor vs diaggrok.

``capture_cell_diag/diag_logstream.c`` is a hand-port of diaggrok's
:func:`diaggrok.hdlc.iter_log_records_stream` into C, so that the capture helper
can produce the log-code-stripped ``LOG_F`` payloads its native decode legs
(``diag_native_decode``) consume without forking the Python bridge.

**Why this needs an external reference.** The extractor's correctness is defined
entirely by agreement with diaggrok, which is not in this tree. The in-file
selftest (``make check`` suite ``test_diag_logstream``) pins the properties a
synthetic frame set can pin -- chunking invariance, CRC rejection, mid-stream
join, oversize resync -- but it cannot pin *"decodes the same records diaggrok
does"*, because the reference is elsewhere. This module closes that half, and
skips with the reason named when no reference is reachable (see
``celltools/decode_oracle.py``).

**What a divergence would mean.** Every generated decode leg is byte-pinned against
the Python bridge. If the C extractor hands a leg a payload the bridge would
never have produced -- a wrong offset, a dropped 0x98-wrapped record, a
chunk-boundary desync -- then the leg's proven byte-parity is irrelevant: it is
correctly decoding the wrong bytes. That failure is invisible from either side
alone.

Skips cleanly unless all three are present:

* ``KP_CELLDIAG_LOGSTREAM_BIN`` -> the driver built by ``make -f standalone.mk
  diag_logstream_dump`` in ``capture_cell_diag``; discovered in this tree when
  unset.
* a reference decoder: this tree's fetched ``capture_cell_diag/.deps/diaggrok``
  (``decode_oracle``).
* a raw-HDLC capture: ``$CELLDIAG_REPLAY_FIXTURE``, else a fixed file name
  under ``$CELL_CAPTURES`` (see ``_capture_or_skip``; captures live outside
  any repo).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import celldiag_parity
import decode_oracle

#: Where the replay fixtures live. **No default.** Captures are large binary
#: recordings kept outside any source tree, and the directory holding them is a
#: property of the host, not of this repo -- so guessing one would either invent
#: a path that does not exist (a skip whose stated cause is wrong) or name
#: somebody's private corpus in a published file. Unset, the fixture-backed
#: tests skip and say which variable to set.
_CAPTURES_ENV = "CELL_CAPTURES"
CAPTURES = Path(os.environ[_CAPTURES_ENV]) if os.environ.get(_CAPTURES_ENV) else None


def _captures_or_skip() -> Path:
    if CAPTURES is None:
        pytest.skip(f"no capture directory: set {_CAPTURES_ENV}=<dir> "
                    "(replay fixtures live outside the source tree)")
    return CAPTURES

# Match diag_logstream_dump's read size (and diag_reader_t's), so the comparison
# exercises the streaming residual path on the SAME boundaries the C side sees --
# a whole-buffer decode on either side would not test the seam that matters.
CHUNK = 65536


def _driver_or_skip() -> str:
    """The C driver, discovered rather than demanded.

    The env var wins when set, but an unset one does not mean "skip":
    celldiag_parity finds an already-built driver in this tree. When it does
    skip, the message distinguishes "this is not a source tree" (legitimate)
    from "the sources are right there and nobody built the driver", which
    would otherwise leave the decode contract unchecked indefinitely."""
    found = celldiag_parity.discover("logstream")
    if not found.ok:
        pytest.skip(found.skip_reason())
    _fail_if_driver_is_stale(found.path)
    return str(found.path)


def _fail_if_driver_is_stale(driver: Path) -> None:
    """FAIL (not skip) when the driver predates its own sources.

    ``test_celldiag_replay_ab.py`` carries the same guard. A stale driver would
    let ``diag_logstream_dump`` report green while testing an extractor nobody
    had shipped, or make the tests accuse the decode path for a build artifact.

    The dependency set is derived from the build, not globbed from the
    directory: ``diag_logstream_dump``'s rule links straight from
    ``diag_logstream.c`` / ``diag_hdlc.c``, so editing ``celldiag_probe.c`` or
    ``capture_cell_diag.c`` correctly leaves it alone. ``link_dependencies``
    consults ``standalone.mk`` as well as ``Makefile``, because ``Makefile``
    exists only after ``./configure``.
    """
    try:
        bin_mtime = driver.stat().st_mtime
    except OSError:
        return

    deps = celldiag_parity.link_dependencies(driver)
    newer = sorted(
        p.name for p in deps.sources
        if p.exists() and p.stat().st_mtime > bin_mtime
    )
    if newer:
        shown = ", ".join(newer[:6]) + (f" +{len(newer) - 6} more" if len(newer) > 6 else "")
        pytest.fail(
            f"KP_CELLDIAG_LOGSTREAM_BIN is STALE: {driver} is older than its "
            f"sources ({shown}). This harness only means anything against a "
            f"current driver — a stale one silently pins a contract nobody "
            f"shipped. [dependency set: {deps.mode} — {deps.note}] "
            f"Rebuild: ({celldiag_parity.staleness('logstream', driver).rebuild_cmd()})"
        )


def _capture_or_skip(tmp_path: Path) -> Path:
    """Materialize the raw-HDLC capture (decompressing a .zst) or skip."""
    env = os.environ.get("CELLDIAG_REPLAY_FIXTURE")
    if env:
        src = Path(env)
        if not src.exists():
            pytest.skip(f"CELLDIAG_REPLAY_FIXTURE={env!r} not found")
    else:
        plain = _captures_or_skip() / "tmp_probe.dlf"
        zst = _captures_or_skip() / "tmp_probe.dlf.zst"
        if plain.exists():
            src = plain
        elif zst.exists():
            src = zst
        else:
            pytest.skip("no replay capture ($CELL_CAPTURES/tmp_probe.dlf[.zst])")

    if src.suffix != ".zst":
        return src

    try:
        import zstandard as zstd
    except ImportError:  # pragma: no cover
        pytest.skip("zstandard needed to decompress the capture")
    dst = tmp_path / "replay.hdlc"
    with src.open("rb") as fp, dst.open("wb") as out:
        zstd.ZstdDecompressor().copy_stream(fp, out)
    return dst


def _c_records(binary: str, capture: Path) -> tuple[list[tuple], dict]:
    """Run the C driver; return its record list plus its framing counters.

    Line format (diag_logstream.c, DIAG_LOGSTREAM_DUMP):
        <log_code:04X> <ts64> <payload_len> <first-8-payload-bytes hex> <T|W>
    """
    proc = subprocess.run([binary, str(capture)], capture_output=True, timeout=300)
    assert proc.returncode == 0, f"driver failed: {proc.stderr.decode(errors='replace')}"

    records = []
    for line in proc.stdout.decode().splitlines():
        code, ts, plen, prefix, origin = line.split(" ")
        records.append((int(code, 16), int(ts), int(plen),
                        "" if prefix == "-" else prefix, origin))

    stats = {}
    for field in proc.stderr.decode().split():
        if "=" in field:
            k, v = field.split("=", 1)
            stats[k] = int(v)
    return records, stats


def _hdlc_or_skip():
    """The reference extractor module, or a skip that says why it is absent.

    Imported through ``decode_oracle`` rather than at module scope: a bare
    ``from diaggrok.hdlc import ...`` at the top would make this file an
    **import error** on a checkout with no reference decoder, which pytest
    reports as a collection failure -- a red suite for a host that is simply not
    equipped to answer this question. What happens outside this tree must not
    decide whether this tree's suite passes.
    """
    hdlc = decode_oracle.import_diaggrok("diaggrok.hdlc")
    if hdlc is None:
        pytest.skip(decode_oracle.discover().skip_reason())
    return hdlc


def _py_records(capture: Path) -> tuple[list[tuple], object]:
    """The reference: diaggrok over the same bytes, same chunking, CRC on.

    ``verify_crc=True`` is not the diaggrok default but IS what the C side does
    unconditionally -- it runs on a live modem port, where a corrupt LOG_F handed
    to a decode leg would be indistinguishable from a real record. Comparing
    against the CRC-off default would compare two different contracts.
    """
    hdlc = _hdlc_or_skip()

    def chunks():
        with capture.open("rb") as fp:
            while True:
                buf = fp.read(CHUNK)
                if not buf:
                    return
                yield buf

    stats = hdlc.HdlcStats()
    records = [
        (code, ts, len(payload), payload[:8].hex(), None)
        for code, ts, payload in hdlc.iter_log_records_stream(
            chunks(), verify_crc=True, stats=stats)
    ]
    return records, stats


@pytest.fixture(scope="module")
def _driver():
    return _driver_or_skip()


@pytest.fixture(scope="module")
def _capture(tmp_path_factory):
    return _capture_or_skip(tmp_path_factory.mktemp("logstream"))


def test_record_stream_matches_diaggrok(_driver, _capture):
    """The two extractors emit the SAME records, in the same order.

    The strong assertion of this module. Order matters as much as content: the
    helper's native path will decode records in stream order and a reordering
    would scramble the SIB1-identity enrichment that depends on a 0xB0C0 being
    seen before the measurement it enriches.
    """
    c_recs, _ = _c_records(_driver, _capture)
    py_recs, _ = _py_records(_capture)

    assert py_recs, "reference produced 0 records - bad capture, not a passing test"
    assert len(c_recs) == len(py_recs), (
        f"record count differs: C={len(c_recs)} diaggrok={len(py_recs)}")

    for i, (c, p) in enumerate(zip(c_recs, py_recs)):
        assert c[:4] == p[:4], (
            f"record {i} differs:\n  C       = {c[:4]}\n  diaggrok= {p[:4]}")


def test_framing_counters_match_diaggrok(_driver, _capture):
    """Frame-level accounting agrees too -- not just the records that survived.

    Equal record lists with unequal CRC tallies would mean the two disagree about
    which frames are valid and coincidentally land on the same LOG set; that is a
    latent divergence on the next capture, so it is asserted, not assumed.
    """
    _, c_stats = _c_records(_driver, _capture)
    _, py_stats = _py_records(_capture)

    assert c_stats["crc_ok"] == py_stats.crc_ok
    assert c_stats["crc_bad"] == py_stats.crc_bad
    assert c_stats["short"] == py_stats.skipped_short
    assert c_stats["records"] == py_stats.log_records
    assert c_stats["from_wrapper"] == py_stats.log_records_from_wrapper


def test_wrapper_recovered_records_are_present(_driver, _capture):
    """A 0x98-sourced record must actually be in the corpus this ran on.

    Guards against a green run that proves nothing: on SDX72-class parts EVERY
    record is 0x98-wrapped, so envelope handling is the difference between full
    decode and total silence. If the chosen capture carries no wrapped record,
    the parity above never exercised that branch and the run must not read as
    coverage of it.
    """
    c_recs, c_stats = _c_records(_driver, _capture)

    assert c_stats["from_wrapper"] > 0, (
        "capture carries no 0x98-wrapped LOG records -- the envelope branch was "
        "never exercised; use a capture from an SDX62/72-class modem")
    assert sum(1 for r in c_recs if r[4] == "W") == c_stats["from_wrapper"]


def test_oversize_and_desync_did_not_fire(_driver, _capture):
    """A clean capture must not trip the C-only defensive paths.

    ``oversize_dropped`` is the one deliberate divergence from diaggrok (which
    has no frame ceiling). It firing on a real capture would mean the parity
    results above were measured on a stream the C side silently truncated.
    """
    _, c_stats = _c_records(_driver, _capture)
    assert c_stats["oversize"] == 0, (
        "the C extractor dropped an oversize frame; its records are NOT "
        "comparable to diaggrok's on this capture")
