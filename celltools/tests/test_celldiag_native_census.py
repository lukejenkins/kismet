"""The native ``diag_inventory`` census, A/B against the bridge's, on real captures.

``capture_cell_diag/diag_inventory.c`` is the census the capture source runs
without the Python bridge. Its counting and rendering are held byte-for-byte
to the bridge's ``DiagInventory`` here, on every committed replay fixture.

**What is compared, and what is not.** The bridge is run on each fixture
exactly as the capture binary feeds it (``run_stream`` over the raw bytes),
with a deterministic clock and a short census interval, so a fixture yields
dozens of periodic lines plus the final one. Every per-record fact the bridge
hands its census -- ``(code, version, decoded, emitted, enrich_failed)`` -- is
recorded, replayed into ``test_diag_inventory --trace``, and every census line
must come back byte-identical. That pins the census itself (keys, both cap
tiers, the findings, the ``status`` line, the table, the bounds, ``ts``) on
the streams that matter.

The per-record facts themselves are the decode path's, not the census's: in
the binary they come from the native legs (``decoded`` = a native leg claimed
the record). The end-to-end ``census=native`` test below compares the binary's
own native census against the bridge's on the fixture recorded with the
wardrive mask, where every code has a native leg.

Skips unless ``$CELL_CAPTURES`` holds the fixtures, the tree's decoder is
fetched (``decode_oracle``), and ``test_diag_inventory`` is built
(``make check`` in ``capture_cell_diag``).
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import decode_oracle

_CAPTURES_ENV = "CELL_CAPTURES"
CAPTURES = Path(os.environ[_CAPTURES_ENV]) if os.environ.get(_CAPTURES_ENV) else None

_CELLDIAG = Path(__file__).resolve().parents[2] / "capture_cell_diag"
TRACE_BIN = _CELLDIAG / "test_diag_inventory"
TRACE_SOURCES = ("diag_inventory.c", "diag_inventory.h", "test_diag_inventory.c")

#: The committed replay fixtures (the ones test_celldiag_replay_ab.py uses).
FIXTURES = (
    "tmp_probe.dlf",
    "celldiag_silent_only.hdlc",
    "celldiag_mixed_lte_nr.hdlc",
    "celldiag_rm500q_cellcfg_beams.hdlc",
)

#: A deterministic clock: one tick per call, so periodic census lines fall at
#: the same records on every run and in both censuses.
_T0 = 1_792_532_000.0
_TICK = 0.0371
_INTERVAL = 2.0


def _trace_or_skip() -> Path:
    if not TRACE_BIN.exists():
        pytest.skip(f"{TRACE_BIN.name} is not built: run `make check` (or "
                    f"`make {TRACE_BIN.name}`) in {_CELLDIAG}")
    newer = [s for s in TRACE_SOURCES
             if (_CELLDIAG / s).stat().st_mtime > TRACE_BIN.stat().st_mtime]
    if newer:
        pytest.fail(f"{TRACE_BIN} is STALE (older than {', '.join(newer)}); "
                    f"rebuild with `make -C {_CELLDIAG} {TRACE_BIN.name}`")
    return TRACE_BIN


def _fixture_or_skip(name: str, tmp_path: Path) -> Path:
    if CAPTURES is None:
        pytest.skip(f"no capture directory: set {_CAPTURES_ENV}=<dir> "
                    "(replay fixtures live outside the source tree)")
    plain, zst = CAPTURES / name, CAPTURES / f"{name}.zst"
    if plain.exists():
        return plain
    if not zst.exists():
        pytest.skip(f"fixture {name}[.zst] not under ${_CAPTURES_ENV}")
    if shutil.which("zstd") is None:
        pytest.skip("zstd is needed to materialize a .zst fixture from CELL_CAPTURES")
    out = tmp_path / name
    subprocess.run(["zstd", "-dqf", str(zst), "-o", str(out)], check=True)
    return out


def _bridge_or_skip():
    if decode_oracle.import_diaggrok("diaggrok.inventory") is None:
        pytest.skip(decode_oracle.discover().skip_reason())
    script = decode_oracle.helper_script_in(decode_oracle._TREE_ROOT)
    if script is None:
        pytest.skip("no bridge script in this tree")
    spec = importlib.util.spec_from_file_location("kismet_diag_decode", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _recorded_bridge_run(kdd, data: bytes):
    """Run the bridge on ``data`` the way the binary feeds it, and return
    (trace commands, the census lines it wrote)."""
    cmds: list[str] = []

    class Recording(kdd.DiagInventory):
        def record(self, code, version, decoded, now, emitted=0,
                   enrich_failed=False):
            cmds.append(f"R {code} {version} {int(bool(decoded))} "
                        f"{int(emitted)} {int(bool(enrich_failed))}")
            super().record(code, version, decoded, now, emitted=emitted,
                           enrich_failed=enrich_failed)

        def summary(self, now, *, wire=False):
            if wire:
                cmds.append(f"W {now!r}")
            return super().summary(now, wire=wire)

    ticks = iter(range(10**9))

    def clock():
        return _T0 + next(ticks) * _TICK

    out = io.StringIO()
    kdd.run_stream([data], "000000000000000", out, clock=clock,
                   inventory=Recording(), inv_interval=_INTERVAL)
    lines = [ln for ln in out.getvalue().splitlines()
             if '"type":"diag_inventory"' in ln]
    return cmds, lines


@pytest.mark.parametrize("fixture", FIXTURES)
def test_native_census_lines_byte_equal_the_bridge(fixture, tmp_path):
    trace = _trace_or_skip()
    kdd = _bridge_or_skip()
    data = _fixture_or_skip(fixture, tmp_path).read_bytes()

    cmds, want = _recorded_bridge_run(kdd, data)
    assert len(want) >= 2, (
        f"{fixture}: the bridge wrote {len(want)} census line(s); the A/B "
        "needs periodic lines as well as the final one")
    got = subprocess.run([str(trace), "--trace"], input="\n".join(cmds) + "\n",
                         text=True, capture_output=True, check=True
                         ).stdout.splitlines()

    assert len(got) == len(want), (
        f"{fixture}: native wrote {len(got)} census line(s), bridge {len(want)}")
    for i, (w, g) in enumerate(zip(want, got)):
        assert g == w, (f"{fixture}: census line {i} of {len(want)} diverged\n"
                        f"  bridge: {w}\n  native: {g}")
    # And the comparison is not vacuous: the final census counts records.
    assert json.loads(want[-1])["distinct"] > 0


# ── end to end: the binary's own census=native ─────────────────────────────────
#: The fixture recorded with the wardrive mask: all ten target codes, every one
#: with a native surface, so the native and bridge censuses see the same facts.
_WARDRIVE_FIXTURE = "celldiag_rm500q_cellcfg_beams.hdlc"


def _replay_harness():
    import test_celldiag_replay_ab as ab  # same directory; conftest puts it on sys.path
    return ab


def _final_census(ab, run) -> dict:
    """The last census a run relayed, as the server sees it: the status line
    on the message bus, the diag_census table, and the inventory_* fields of
    the final diag_stats."""
    msgs = [m for m in run.messages if "inventory:" in m and "(code,ver)" in m]
    tables = []
    for js in run.control:
        try:
            o = json.loads(js)
        except json.JSONDecodeError:
            continue
        if isinstance(o, dict) and o.get("type") == "diag_census":
            tables.append(o["table"])
    stats = ab._diag_stats_lines(run)
    inv = {k: v for k, v in (stats[-1] if stats else {}).items()
           if k.startswith("inventory_")}
    return {"status": msgs[-1] if msgs else None,
            "table": tables[-1] if tables else None,
            "stats": inv, "lines": len(msgs)}


def test_census_native_lands_exactly_like_the_bridge_census(tmp_path):
    """census=native vs the bridge's census, through the real binary and the
    real relay, on the same bytes: the final status line, table and
    inventory_* stats fields are identical, and periodic lines arrive too."""
    ab = _replay_harness()
    binary = ab._native_binary_or_skip()
    replay = _fixture_or_skip(_WARDRIVE_FIXTURE, tmp_path)

    runs = {}
    for producer in ("helper", "native"):
        run = ab.CellDiagRun(binary, ab._definition(
            replay, nativedecode="shadow", census=producer, stats_interval=1),
            timeout=180.0).run()
        assert run.open_code in (1, None), (
            f"census={producer}: OPENREPORT failed ({run.open_code}): "
            f"{run.open_message!r}")
        runs[producer] = _final_census(ab, run)

    helper, native = runs["helper"], runs["native"]
    assert helper["status"], "the bridge relayed no census -- nothing to compare"
    assert native["status"] == helper["status"], (
        f"final census status differs\n  helper: {helper['status']}\n"
        f"  native: {native['status']}")
    assert native["table"] == helper["table"], (
        f"final census table differs\n  helper: {helper['table']}\n"
        f"  native: {native['table']}")
    # The one native-only key: present (0 -- every code here has a native
    # leg) under census=native, absent under the helper, which cannot say.
    nstats = dict(native["stats"])
    assert nstats.pop("inventory_undecoded_native", None) == 0, native["stats"]
    assert "inventory_undecoded_native" not in helper["stats"], helper["stats"]
    assert nstats == helper["stats"] and nstats, (
        f"inventory_* stats differ: helper {helper['stats']} native {native['stats']}")


@pytest.mark.parametrize("fixture", FIXTURES)
def test_census_native_reserves_unrecognized_for_what_diaggrok_drops(fixture, tmp_path):
    """The third state, end to end, on every fixture (an operator
    decision). A code diaggrok decodes and no native leg reads is
    `undecoded_native`, so `unrecognized` -- and with it the ERROR escalation --
    means what the bridge's census means. Keys, overflow and silent agree with
    the bridge; the undecoded codes are counted and named, never escalated."""
    ab = _replay_harness()
    binary = ab._native_binary_or_skip()
    replay = _fixture_or_skip(fixture, tmp_path)

    runs = {}
    for producer in ("helper", "native"):
        run = ab.CellDiagRun(binary, ab._definition(
            replay, nativedecode="shadow", census=producer), timeout=300.0).run()
        assert run.open_code in (1, None), run.open_message
        runs[producer] = (run, _final_census(ab, run))

    (_, helper), (nrun, native) = runs["helper"], runs["native"]
    for key in ("inventory_distinct_codes", "inventory_unrecognized",
                "inventory_silent"):
        assert native["stats"].get(key) == helper["stats"].get(key), (
            f"{fixture}: {key} native {native['stats']} helper {helper['stats']}")
    undecoded = native["stats"].get("inventory_undecoded_native")
    if fixture == _WARDRIVE_FIXTURE:
        assert undecoded == 0, native["stats"]
        assert "undecoded-native" not in native["status"]
    else:
        assert undecoded and undecoded > 0, native["stats"]
        assert f"{undecoded} undecoded-native" in native["status"], native["status"]
        assert " undecoded_native[0x" in native["status"], native["status"]
        assert any(r.rsplit(",", 1)[-1] == "n" for r in native["table"].split(";")), (
            "no undecoded_native row reached the diag_census table")
    # A to-do list, not an alarm: the final census line escalates exactly when
    # the bridge's does.
    assert _final_census_flags(nrun) == _final_census_flags(runs["helper"][0]), (
        f"{fixture}: the native census escalated differently from the bridge's")


def _final_census_flags(run) -> int:
    idx = [i for i, m in enumerate(run.messages)
           if "inventory:" in m and "(code,ver)" in m]
    assert idx, "no census status line relayed"
    return run.message_flags[idx[-1]]


def test_census_native_needs_the_native_tap(tmp_path):
    """With native decode off nothing in the binary sees a record; an armed
    census would publish "0 keys". The source must refuse at open instead."""
    ab = _replay_harness()
    binary = ab._native_binary_or_skip()
    replay = _fixture_or_skip(_WARDRIVE_FIXTURE, tmp_path)
    run = ab.CellDiagRun(binary, ab._definition(replay, nativedecode="off",
                                                census="native")).run()
    assert run.open_code not in (1, None), "census=native opened with no native tap"
    assert "nativedecode" in (run.open_message or ""), run.open_message


def test_census_rejects_an_unknown_producer(tmp_path):
    ab = _replay_harness()
    binary = ab._native_binary_or_skip()
    replay = _fixture_or_skip(_WARDRIVE_FIXTURE, tmp_path)
    run = ab.CellDiagRun(binary, ab._definition(
        replay, nativedecode="shadow", census="python")).run()
    assert run.open_code not in (1, None)
    assert "census" in (run.open_message or ""), run.open_message


# ── pinned divergence: a record a native leg claims but diaggrok rejects ───────
def _functional_fixture_module():
    path = _CELLDIAG / "fixtures" / "make_functional_fixture.py"
    spec = importlib.util.spec_from_file_location("make_functional_fixture", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


#: A 0xB193 record with outer v1 and one subpacket (v36) whose declared size
#: (200) overruns the payload (16 B). diaggrok.parse returns None on it. The
#: native 0xB193 leg exists, so the native census claims it.
_MALFORMED_B193 = (bytes([1, 1]) + (0).to_bytes(2, "little") + bytes([0, 36])
                   + (200).to_bytes(2, "little") + bytes(8))
_MALFORMED_COUNT = 4


def _census_row(table: str, key: str) -> str | None:
    return next((r for r in table.split(";") if r.split(",", 1)[0] == key), None)


def test_a_malformed_leg_record_matches_the_bridge(tmp_path):
    """A record a native leg claims but diaggrok refuses is DROPPED, in both
    censuses (the parity rule): `unrecognized`, never decoded.

    The native census once read it as decoded (`0xB193/v1,4,4,0,0,0,s`
    against the bridge's `0xB193/v1,4,0,4,0,0,u`), because a leg returned the
    same empty list for "refused" and "parsed, nothing to emit". Every leg now
    reports a refusal exactly where diaggrok's parser returns None, and the
    census counts that dropped.

    No committed fixture holds such a record (`dropped` is 0 for every target
    code on all four), so this synthesizes one.
    """
    ab = _replay_harness()
    binary = ab._native_binary_or_skip()
    _bridge_or_skip()             # puts the tree's diaggrok on sys.path
    from diaggrok import parse
    # Preconditions fail with pytest.fail, not assert, so a broken
    # precondition reads as a broken fixture, never as a parity failure.
    if parse(0xB193, 0, _MALFORMED_B193) is not None:
        pytest.fail("the synthetic record now parses -- it no longer exercises the case")

    fx = _functional_fixture_module()
    data = b"\x7e" + b"".join(fx.log_f_frame(0xB193, 1000 + i, _MALFORMED_B193)
                              for i in range(_MALFORMED_COUNT))
    replay = tmp_path / "malformed_b193.hdlc"
    replay.write_bytes(data)

    BRIDGE_ROW = f"0xB193/v1,{_MALFORMED_COUNT},0,{_MALFORMED_COUNT},0,0,u"

    rows = {}
    for producer in ("helper", "native"):
        run = ab.CellDiagRun(binary, ab._definition(
            replay, nativedecode="shadow", census=producer), timeout=120.0).run()
        if run.open_code not in (1, None):
            pytest.fail(f"census={producer}: OPENREPORT failed: {run.open_message!r}")
        final = _final_census(ab, run)
        if final["table"] is None:
            pytest.fail(f"census={producer} relayed no table")
        rows[producer] = _census_row(final["table"], "0xB193/v1")

    # The bridge half is not what is expected to fail: if it moves, the
    # synthetic record no longer means what this test says.
    if rows["helper"] != BRIDGE_ROW:
        pytest.fail(f"the bridge's row moved -- fix the fixture: {rows}")
    assert rows["native"] == rows["helper"], rows
