"""The native ``gps_fix`` line: ``nativedecode=on`` renders it, the bridge does not.

The helper's ``gps_fix`` line never reaches the server as a report. The capture
binary intercepts it and does two things with it:
- relays its ``status`` one-liner (``celldiag GPS fix <lat>,<lon> alt=<m>m
  src=0x1476``) to the message bus;
- caches the position, so later ``cell_observation`` reports carry a GPS block
  and Kismet geo-tags the devices.

A native ``gps_fix`` therefore lands identically if it produces the same
message-bus lines and the same geo-tags. Under ``nativedecode=on`` the native
GNSS leg does both, and the bridge's copy is dropped and counted for a
spindown A/B, the same way its ``cell_observation`` lines are.

Two captures carry accepted fixes, and both are compared:
- ``tmp_probe.dlf``, the committed replay fixture: 16 fixes;
- a live LM960A18 capture: 34 fixes, a held position, and
  125 cells to geo-tag. LM960A18 is a core-four modem with no other fixture.
The other three committed fixtures decline every 0x1476 (no sky view).

Skips unless ``$CELL_CAPTURES`` holds the capture and a ``make NATIVE=1``
binary is available (see ``test_celldiag_replay_ab._native_binary_or_skip``).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_CAPTURES_ENV = "CELL_CAPTURES"
CAPTURES = Path(os.environ[_CAPTURES_ENV]) if os.environ.get(_CAPTURES_ENV) else None

from celldiag_goldens import FIXTURE_BY_LABEL  # noqa: E402

#: Captures with accepted 0x1476 fixes, relative to $CELL_CAPTURES.
#: - tmp_probe.dlf: the committed replay fixture, 16 fixes.
#: - the live LM960A18 capture: 34 fixes (a held position, pos_source=4),
#:   12 declined 0x14D8, and LTE cells to geo-tag. Its corpus path is in the
#:   golden ledger (``goldens/FIXTURES.tsv``); a corpus with no ledger entry
#:   for it is a setup error, so the lookup fails loudly rather than skipping.
GNSS_FIXTURES = {"tmp_probe": "tmp_probe.dlf"}
if CAPTURES is not None:
    GNSS_FIXTURES["lm960"] = FIXTURE_BY_LABEL["lm960"]

_FIX_PREFIX = "celldiag GPS fix "


def _ab():
    import test_celldiag_replay_ab as ab  # same directory; conftest adds it
    return ab


def _gnss_fixture_or_skip(tmp_path: Path, rel: str) -> Path:
    if CAPTURES is None:
        pytest.skip(f"no capture directory: set {_CAPTURES_ENV}=<dir> "
                    "(replay fixtures live outside the source tree)")
    plain, zst = CAPTURES / rel, CAPTURES / f"{rel}.zst"
    if plain.exists():
        return plain
    if not zst.exists():
        pytest.skip(f"GNSS fixture {rel}[.zst] not under ${_CAPTURES_ENV}")
    if shutil.which("zstd") is None:
        pytest.skip("zstd is needed to materialize a .zst fixture from CELL_CAPTURES")
    out = tmp_path / Path(rel).name
    subprocess.run(["zstd", "-dqf", str(zst), "-o", str(out)], check=True)
    return out


@pytest.fixture(scope="module", params=sorted(GNSS_FIXTURES))
def runs(request, tmp_path_factory):
    """One replay of each GNSS fixture per mode, shared by the tests below."""
    ab = _ab()
    binary = ab._native_binary_or_skip()
    replay = _gnss_fixture_or_skip(tmp_path_factory.mktemp("gnss"),
                                   GNSS_FIXTURES[request.param])
    out = {}
    for mode in ("off", "shadow", "on"):
        run = ab.CellDiagRun(binary, ab._definition(replay, nativedecode=mode),
                             timeout=300.0).run()
        assert run.open_code in (1, None), (
            f"nativedecode={mode}: OPENREPORT failed: {run.open_message!r}")
        out[mode] = run
    return out


def _fix_lines(run) -> list[str]:
    return [m for m in run.messages if m.startswith(_FIX_PREFIX)]


def test_on_emits_the_bridges_gps_fix_lines_byte_for_byte(runs):
    """The status lines, in order and byte for byte. `off` is the bridge's
    rendering; `on` is the native leg's, with the bridge's dropped."""
    off, on = _fix_lines(runs["off"]), _fix_lines(runs["on"])
    assert off, "the bridge produced no gps_fix on the GNSS fixture -- nothing to compare"
    assert on == off, (
        f"native gps_fix lines differ from the bridge's: {len(on)} vs {len(off)}; "
        f"first native {on[:1]}, first bridge {off[:1]}")


def test_on_geotags_identically_to_off(runs):
    """The other half of what a gps_fix line does: the position every later
    observation is stamped with. Same observations, same GPS blocks."""
    off, on = runs["off"], runs["on"]
    assert len(on.observations) == len(off.observations) > 0
    assert any(g is not None for g in off.observation_gps), (
        "no observation was geo-tagged under the bridge -- the fixture proves nothing")
    assert _positions(on) == _positions(off)


def _positions(run) -> list:
    """Each observation's GPS block reduced to what both renderings must
    agree on: lat, lon and fix. The block's ts_sec is the arrival wall clock
    of the fix that filled the cache, so it differs between any two runs."""
    ab = _ab()
    keys = (ab.SUB_GPS_FIELD_LAT, ab.SUB_GPS_FIELD_LON, ab.SUB_GPS_FIELD_FIX)
    return [None if g is None else tuple(g.get(k) for k in keys)
            for g in run.observation_gps]


def test_on_reports_its_gps_fix_agreement_with_the_bridge(runs):
    """The spindown A/B: the bridge still decoded every fix, so `on` counts
    the copies it dropped and says whether the two renderings agree."""
    n = len(_fix_lines(runs["off"]))
    msgs = runs["on"].messages
    assert any("nativedecode=on gps_fix agrees with the Python bridge: "
               f"{n} fix(es)" in m for m in msgs), (
        f"no gps_fix agreement line for {n} fixes: "
        f"{[m for m in msgs if 'gps_fix' in m]}")
    assert not any("gps_fix DIVERGED" in m for m in msgs)


def test_shadow_does_not_double_the_gps_fix_lines(runs):
    """In shadow the bridge stays authoritative: the native leg caches the fix
    but must not emit a second status line for it."""
    assert _fix_lines(runs["shadow"]) == _fix_lines(runs["off"])
