"""The canned replay fixture and its comparator.

These tests need no decoder and no network: they pin the fixture's own
properties and the comparator's behaviour, both of which are pure logic. The
end-to-end decode claim is the `functional` makefile target's job, and it is
deliberately not duplicated here: a pytest that skipped when the decoder is
unavailable would turn a loud failure of that target into a silent yellow.

The load-bearing tests in this module are the negative controls
(`test_the_comparator_fails_on_an_empty_observation_set` and its sibling). The
comparator's entire job is to reject an empty or wrong observation set, so a
comparator that could only ever return 0 would satisfy every positive test here
while proving nothing.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
FIXTURES = HERE / "fixtures"
FIXTURE = FIXTURES / "functional.hdlc"
EXPECTED = FIXTURES / "functional.expected.json"
GENERATOR = FIXTURES / "make_functional_fixture.py"
COMPARATOR = FIXTURES / "check_functional.py"

sys.path.insert(0, str(FIXTURES))
import make_functional_fixture as gen  # noqa: E402


# ── the fixture itself ───────────────────────────────────────────────────────

def test_the_committed_fixture_matches_its_generator_byte_for_byte():
    """The generator is the fixture's documentation, so drift between them
    would make the documentation wrong in the one way nobody checks. Bytes,
    not a record count: a regenerated fixture with the same shape and different
    contents would pass a count-based test and invalidate the expected file."""
    assert FIXTURE.read_bytes() == gen.build()


def test_the_fixture_clears_the_hdlc_detector_delimiter_floor():
    """Regression guard.

    ``diaggrok.dlf.detect_format`` classifies a stream as "hdlc" only when its
    head window holds at least 4 ``0x7E`` delimiters, and ``is_probably_capture``
    -- which the bridge runs before decoding anything -- returns False for any
    other verdict. A 3-frame version of this fixture is refused with "not a
    DIAG capture" even though every frame in it is CRC-valid.

    That failure mode is invisible by inspection: the bytes are well-formed and
    the rejection names the file, not the frame count. Anyone trimming this
    fixture toward "one record is enough" will reintroduce it, so the floor is
    asserted here rather than left in a comment."""
    assert FIXTURE.read_bytes().count(0x7E) >= 4


def test_every_record_is_a_crc_valid_log_f_frame():
    """Walks the fixture the way the deframer does. This is what makes the
    generator's framing arithmetic -- the two length fields especially -- a
    checked claim rather than an assumption: a frame whose ``outer_len``
    disagrees with its true length is dropped as desynced, and the capture
    would decode to zero records with every byte still looking plausible."""
    data = FIXTURE.read_bytes()
    segments = [s for s in data.split(b"\x7e") if s]
    assert len(segments) == len(gen.RECORDS)
    for seg in segments:
        frame = _unescape(seg)
        assert frame[0] == 0x10, "not a DIAG_LOG_F frame"
        body, crc = frame[:-2], int.from_bytes(frame[-2:], "little")
        assert gen.crc16_ccitt(body) == crc, "CRC mismatch"
        # The redundant-duplicate invariant diaggrok's _is_len_desync enforces.
        assert int.from_bytes(frame[2:4], "little") == len(frame) - 2 - 4
        assert int.from_bytes(frame[4:6], "little") == len(frame) - 2 - 4


def _unescape(seg: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(seg):
        if seg[i] == 0x7D and i + 1 < len(seg):
            out.append(seg[i + 1] ^ 0x20)
            i += 2
        else:
            out.append(seg[i])
            i += 1
    return bytes(out)


def test_the_records_do_not_all_decode_to_the_same_values():
    """A fixture whose every record carries identical fields cannot separate
    "the parser read the field" from "the parser returned a constant"."""
    assert len({r[1] for r in gen.RECORDS}) == len(gen.RECORDS), "PCIs repeat"
    assert len({r[2] for r in gen.RECORDS}) > 1, "RSRP is constant"


# ── the expected set ─────────────────────────────────────────────────────────

def test_the_expected_set_is_non_empty_and_carries_no_wallclock():
    """``prov.captured_at`` is stamped at replay time, so an expected file
    carrying one could never match. Its absence is what lets the comparator
    assert equality rather than a fuzzy subset."""
    expected = json.loads(EXPECTED.read_text())
    assert expected, "an empty expectation asserts nothing"
    for obs in expected:
        assert "captured_at" not in obs.get("prov", {})
        assert obs.get("prov", {}).get("log_tick") is not None


def test_the_expected_set_agrees_with_the_generator():
    """Pins the two committed artifacts to each other. Regenerating the fixture
    without regenerating the expected file is the obvious way to leave this
    target asserting yesterday's answer."""
    expected = json.loads(EXPECTED.read_text())
    assert len(expected) == len(gen.RECORDS)
    for obs, (earfcn, pci, rsrp, rsrq) in zip(expected, gen.RECORDS):
        assert obs["pci"] == pci
        assert obs["earfcn"] == earfcn
        assert obs["rsrp"] == pytest.approx(rsrp)
        assert obs["rsrq"] == pytest.approx(rsrq)


# ── PII: the property that lets these files ship publicly ────────────────────

def test_the_fixture_assets_carry_no_real_identity():
    """The fixture is synthesised, not scrubbed; this asserts that stays
    true. The IMEI must be the all-zero synthetic one, and there must be no
    coordinate anywhere: a high-precision bare decimal is the field class
    most likely to leak a location."""
    expected = json.loads(EXPECTED.read_text())
    for obs in expected:
        assert obs["prov"]["imei"] == "0" * 15
        for key in ("lat", "lon", "latitude", "longitude", "alt"):
            assert key not in obs


# ── the comparator: positive, then the load-bearing negatives ────────────────

def _run_comparator(observed: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(COMPARATOR), str(observed), str(EXPECTED)],
        capture_output=True, text=True)


def _observed_from_expected(tmp_path: Path, mutate=None) -> Path:
    rows = json.loads(EXPECTED.read_text())
    for i, row in enumerate(rows):
        # Put the volatile field back: the comparator must strip it, and a test
        # that never supplies one would not exercise that.
        row.setdefault("prov", {})["captured_at"] = 1785952042.0 + i
    if mutate:
        mutate(rows)
    out = tmp_path / "observed.jsonl"
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return out


def test_the_comparator_passes_on_a_faithful_replay(tmp_path):
    proc = _run_comparator(_observed_from_expected(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert "PASS" in proc.stdout


def test_the_comparator_fails_on_an_empty_observation_set(tmp_path):
    """The key test. An empty set is what a decoder that silently declines to
    decode produces, and it is the outcome the whole target exists to catch."""
    empty = tmp_path / "observed.jsonl"
    empty.write_text("")
    proc = _run_comparator(empty)
    assert proc.returncode == 1
    assert "ZERO cell observations" in proc.stderr


def test_the_comparator_fails_on_a_wrong_value(tmp_path):
    """Non-emptiness alone would accept four observations of garbage."""
    def bump_rsrp(rows):
        rows[0]["rsrp"] = rows[0]["rsrp"] + 3.0
    proc = _run_comparator(_observed_from_expected(tmp_path, bump_rsrp))
    assert proc.returncode == 1
    assert "observed" in proc.stderr


def test_the_comparator_fails_on_a_missing_record(tmp_path):
    """A decoder that drops the tail of the capture still emits a non-empty,
    entirely correct prefix -- so a prefix-tolerant compare would pass it."""
    proc = _run_comparator(_observed_from_expected(tmp_path, lambda r: r.pop()))
    assert proc.returncode == 1


def test_the_comparator_rejects_non_json_stdout(tmp_path):
    """A traceback or warning on stdout must not be silently skipped into an
    empty set whose stated cause would then be wrong."""
    bad = tmp_path / "observed.jsonl"
    bad.write_text("Traceback (most recent call last):\n")
    proc = _run_comparator(bad)
    assert proc.returncode == 1
    assert "non-JSON" in proc.stderr
