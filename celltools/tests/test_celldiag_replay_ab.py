"""Offline A/B harness for the Kismet ``kismet_cap_cell_diag`` capture binary.

Drives the compiled capture binary end-to-end **without a live modem or a running
Kismet server** by speaking just enough of the Kismet ``kismet_external`` **v3**
datasource protocol (msgpack over ``--in-fd``/``--out-fd``) to:

* send an ``OPENREQ`` with a ``celldiag-<imei>:replay=<file>`` source definition,
* read back the ``PACKET`` (DATAREPORT) frames the binary relays, and
* extract the ``cell_observation`` JSON each carries.

Two core assertions:

* **replay A/B** — the binary's emitted observations equal what the reference
  helper ``tools/kismet_diag_decode.py`` produces on the same bytes. Because the
  binary *spawns that same helper* and only relays its stdout, this isolates and
  proves the **C deframe -> pipe -> relay** path is byte-faithful (no dropped,
  reordered, or corrupted observation lines).
* **rawlog round-trip** —``replay=<f> rawlog=<out>`` writes ``<out>``
  byte-identical to the replay input, and ``replay=<out>`` reproduces the same
  observations.

The whole module skips cleanly unless a built ``kismet_cap_cell_diag`` is
found -- and unless a reference decoder is reachable, since leg B *is* that
decoder. A checkout nobody has built, on a host with no reference, is not a
failure of this tree; see ``celltools/decode_oracle.py``.

**The protocol layer and the driver live in ``kismet_capture_ipc``**: the v3
framing, the msgpack subset, the mandatory PING keepalive and the
``CaptureRun`` pump. None of it is celldiag-specific, and sharing it gives
``capture_cell_at`` the same offline harness. Read that module for the wire
spec. What remains here is the celldiag half:
the ``diag_stats`` relay split, the staleness guard, the fixtures, and the tests.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

import celldiag_parity
import decode_oracle

# ── reference decoder / fixture discovery ─────────────────────────────────────
# The reference is located exactly the way the capture binary locates its
# decoder -- this tree's .deps/diaggrok, python3 off PATH -- so this harness and
# the shipped program never disagree about where the oracle is. See
# decode_oracle.
ORACLE = decode_oracle.discover()
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

# 15-digit synthetic IMEI; must match between both legs (stamped into prov.imei).
IMEI = "000000000000000"

# ── v3 kismet_external framing + the source-agnostic driver ────────────────────
# Lives in ``kismet_capture_ipc``: nothing in the protocol layer or the driver
# is celldiag-specific, and ``capture_cell_at`` drives the same code offline.
from kismet_capture_ipc import (  # noqa: E402 -- same dir; pytest prepends it
    CaptureRun, SUB_GPS_FIELD_FIX, SUB_GPS_FIELD_LAT, SUB_GPS_FIELD_LON,
)


class CellDiagRun(CaptureRun):
    """One offline replay run of ``kismet_cap_cell_diag``.

    The single celldiag-shaped thing in the driver: two streams share the
    observation relay, and conflating them is a bug.
    """

    #: Relayed JSON types that are datasource-field updates the server
    #: INTERCEPTS before phy_cell can devicify them -- not cell observations.
    #: They also carry wall-clock fields (diag_stats' last_obs_epoch), so
    #: leaving one in `observations` makes every A/B equality comparison
    #: between two runs fail for a reason that has nothing to do with decode.
    _CONTROL_TYPES = ("diag_stats", "diag_census")

    def _on_json(self, js: str) -> None:
        # Control lines are recognized by their declared type, not by asking
        # "is this diag_stats?" and treating everything else as an
        # observation. Otherwise any new control line (diag_census, for one)
        # would be counted as a cell observation and compared byte-for-byte
        # against leg B, which never produces one.
        flat = js.replace(" ", "")
        if any(f'"type":"{t}"' in flat for t in self._CONTROL_TYPES):
            self.control.append(js)
        else:
            # _record_observation, not observations.append: it keeps
            # `observation_gps` in step (kismet_capture_ipc).
            self._record_observation(js)



# ── reference leg (B) ─────────────────────────────────────────────────────────
def _reference_observations(replay_file: Path) -> list[str]:
    """Leg B: run the same helper the binary spawns, fed the raw bytes on stdin.

    ``--no-inventory`` so the output is pure ``cell_observation`` lines (the
    binary never relays the periodic ``diag_inventory`` census through
    ``cf_send_json``, so leg A is already inventory-free).

    ``gps_fix`` records are dropped for the same reason: the binary
    *intercepts* them (``capture_cell_diag.c``, the ``"type":"gps_fix"`` branch
    of ``drain_helper_lines``) and caches the position to geo-stamp subsequent
    cell_observations, rather than relaying them through ``cf_send_json``. So
    leg A never contains a gps_fix and leg B must not either -- otherwise the
    A/B compare fails on an intentional asymmetry. There is no helper flag to
    suppress them (unlike ``--no-inventory``), so filter here."""
    proc = subprocess.run(
        ORACLE.cmd("--imei", IMEI, "--no-inventory"),
        # env=ORACLE.env() primes PYTHONPATH when the oracle resolved a
        # venv-less system python3; a no-op for a venv/override oracle.
        # Without it a venv-less leg B would die at `import diaggrok` and relay
        # zero observations -- a silent-zero the A/B would read as a real decode.
        env=ORACLE.env(),
        stdin=replay_file.open("rb"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    out = []
    for ln in proc.stdout.decode().splitlines():
        if not ln.strip():
            continue
        try:
            if json.loads(ln).get("type") == "gps_fix":
                continue
        except json.JSONDecodeError:
            pass
        out.append(ln)
    return out


def _kv_int(line: str, key: str) -> int:
    """Pull ``key=<int>`` out of a ``|``-separated readout line.

    The native tap's message-bus readouts are ``key=value`` tokens separated by
    spaces and ``|`` group markers. Asserting on parsed counters rather than on
    the whole string keeps the tests pinned to the *behaviour* being reported
    and not to the line's layout.
    """
    for tok in line.replace("|", " ").split():
        if tok.startswith(f"{key}="):
            return int(tok.split("=", 1)[1])
    raise AssertionError(f"field {key!r} missing from readout: {line!r}")


def _canon(lines: list[str]) -> list[dict]:
    """Parse each observation, dropping the one inherently volatile field.

    ``prov.captured_at`` is a decode-time wall-clock stamp (differs every run);
    ``log_tick`` — the DIAG record's own timestamp — is stable and IS compared.
    Everything else (rat/pci/earfcn/rsrp/rsrq/origin/type) is content the relay
    path must preserve exactly."""
    out = []
    for ln in lines:
        obj = json.loads(ln)
        prov = obj.get("prov")
        if isinstance(prov, dict):
            prov.pop("captured_at", None)
        out.append(obj)
    return out


# ── skip gating + fixture ──────────────────────────────────────────────────────
def _binary_or_skip() -> str:
    """The compiled capture binary, discovered rather than demanded.

    A *stale* binary fails (see ``_fail_if_binary_is_stale``). An *absent* one
    skips: an unset env var falls through to discovery in this tree, and the
    skip reason says whether the binary or the reference decoder is missing."""
    found = celldiag_parity.discover("celldiag")
    if not found.ok:
        pytest.skip(found.skip_reason())
    b = str(found.path)
    if not ORACLE.ok:
        pytest.skip(ORACLE.skip_reason())
    _fail_if_binary_is_stale(Path(b))
    return b


def _native_binary_or_skip() -> str:
    """The capture binary, required to be a ``make NATIVE=1`` build.

    Every ``nativedecode=shadow`` test below needs the C++ decode legs to be IN
    the binary. They are opt-in: plain ``make`` produces the **Python-decode
    baseline** -- 11 pure-C objects plus ``diag_native_decode_stub.c``, no C++,
    no ``../diagspec/`` dependency -- and that build REJECTS
    ``nativedecode=shadow`` at source-open time rather than arming a tap with
    nothing behind it.

    This skips where its sibling ``_fail_if_binary_is_stale`` fails, on
    purpose. A stale binary is rot: testing it silently reports on code nobody
    shipped. A baseline binary is the default configuration that plain ``make``
    yields, and the suite must not go red because someone built the default.

    So the reason line names the configuration found and the command that
    produces the other one, rather than a bare skip that could equally mean the
    binary is missing.

    Detected by symbol rather than by attempting a run: a leg is either linked or
    it is not, ``nm`` answers in milliseconds, and the alternative (arm the tap
    and see whether the source refuses) would make every one of these tests pay a
    full replay to learn it should not have started.
    """
    # The gate's NATIVE=1 copy first, as _baseline_binary_or_skip does. The
    # gate restores the tree to its entry mode, so from a baseline tree the
    # in-tree binary is never native and these tests would skip beside a
    # native build the gate just made. A binary the gate NAMED as native that
    # is not one is a broken gate: fail, never skip.
    if os.environ.get(_GATE_ENV["native"]):
        b = _gate_binary_or_skip("native")
        native = _defines_native_legs(b)
        if native is None:
            pytest.skip("nm unavailable — cannot tell a NATIVE=1 build from the baseline")
        if not native:
            pytest.fail(
                f"{_GATE_ENV['native']}={b} has NO native decode legs, so it is "
                f"not a NATIVE=1 build and the shadow tests would measure nothing. "
                f"Rebuild with `{_GATE_CMD}`."
            )
        return b
    b = _binary_or_skip()
    native = _defines_native_legs(b)
    if native is None:
        pytest.skip("nm unavailable — cannot tell a NATIVE=1 build from the baseline")
    if not native:
        pytest.skip(
            f"{Path(b).name} is a Python-decode baseline build (no native decode "
            f"legs linked), so nativedecode=shadow is refused by design. Rebuild "
            f"with 'make NATIVE=1' in capture_cell_diag to run this test. "
            f"Full path: {b}"
        )
    return b


def _baseline_binary_or_skip() -> str:
    """The capture binary, required to be a plain ``make`` (baseline) build.

    The exact inverse of ``_native_binary_or_skip``, and it exists for the one
    assertion that can only be made against a build with no legs: that
    ``nativedecode=on`` is REFUSED there. Over the stub, ``on`` would suppress
    the bridge and put nothing in its place -- a source that opens cleanly and
    emits zero observations.

    Prefers ``$CELLDIAG_BASELINE_BIN`` (what ``make -C celltools replay-gate``
    produces, and the only way to have both configurations at once), then falls
    back to the discovered in-tree binary **if it happens to be a baseline
    build**. A configured tree holds exactly one configuration, so demanding the
    double build here would make this test skip on every ordinary checkout;
    accepting whichever is present makes it run about half the time, for free.
    """
    raw = os.environ.get(_GATE_ENV["baseline"])
    if raw:
        return _gate_binary_or_skip("baseline")
    b = _binary_or_skip()
    native = _defines_native_legs(b)
    if native is None:
        pytest.skip("nm unavailable — cannot tell a baseline build from NATIVE=1")
    if native:
        pytest.skip(
            f"{Path(b).name} is a NATIVE=1 build, and this test needs a build "
            f"with NO decode legs. Either build both configurations with "
            f"`{_GATE_CMD}` (which sets {_GATE_ENV['baseline']}), or rebuild "
            f"this tree with a plain `make -C capture_cell_diag`. "
            f"Full path: {b}"
        )
    return b


def _fail_if_binary_is_stale(binary: Path) -> None:
    """FAIL (not skip) when the binary predates its own sources.

    This harness's entire value is that it exercises the COMPILED
    ``kismet_cap_cell_diag``, not a model of it. A stale binary that disagrees
    with current source shows up as decode failures (*"mask=full diverged from
    the unset default"*) that accuse the decode path for what is purely a build
    artifact.

    The inverse is the dangerous direction and the reason this fails loudly: a
    stale binary that happens to still agree reports GREEN while testing code
    nobody shipped. Skipping would hide that, so a stale binary is an error
    with a rebuild instruction, not a skip.

    **What counts as "its sources".** Not a directory glob, which is wrong in
    both directions: it would flag ``celldiag_probe.c`` and the
    ``test_diag_*.c`` drivers, which are linked into nothing here, and miss
    ``diag_native_decode.cpp`` and the C++ decode legs under ``../diagspec/``,
    which are linked in via ``NATIVE_OBJS``. The set comes from the binary's
    real link rule + ``.d`` files; see ``celldiag_parity.link_dependencies``.

    **Where the predicate lives.** The staleness question is answered by
    ``celldiag_parity.staleness()``, not here, so that any other caller (a
    post-sync check, for example) asks the same question. Two copies of "is it
    stale?" drift, and drift means one says current while pytest says stale.

    **The message leads with the rebuild command.** pytest's short summary
    shows the first line, so the fix must come first or the summary reads as a
    decode failure with no hint that the cause is a build artifact.
    """
    st = celldiag_parity.staleness("celldiag", binary)
    if st.stale:
        pytest.fail(
            f"REBUILD NEEDED: {st.rebuild_cmd()} — "
            f"{binary.name} is older than its sources ({st.summary()}). "
            f"Usually means something restamped the sources without rebuilding. "
            f"This harness only means anything against a current binary — a "
            f"stale one silently tests code nobody shipped. "
            f"[dependency set: {st.deps.mode} — {st.deps.note}] "
            f"Full path: {binary}"
        )


def _materialize(src: Path, dst: Path) -> Path:
    """Copy ``src`` to ``dst``, decompressing a ``.zst`` on the way."""
    if src.suffix == ".zst":
        try:
            import zstandard  # noqa: F401
        except ImportError:  # pragma: no cover
            pytest.skip("zstandard needed to decompress the fixture")
        import zstandard as zstd
        with src.open("rb") as fp, dst.open("wb") as out:
            zstd.ZstdDecompressor().copy_stream(fp, out)
    else:
        shutil.copyfile(src, dst)
    return dst


def _silent_only_fixture_or_skip(tmp_path: Path) -> Path:
    """The EG25-G capture whose census has SILENT keys and NO unrecognized ones.

    The default replay fixture carries BOTH kinds of finding, so the ERROR flag
    on its census line cannot be attributed to `silent`: a control deleting the
    `silent` term still passes, because `unrecognized` escalates anyway. This
    fixture closes that gap.

    A passive (no forced sweep) DIAG capture from a SIM-less EG25-G censuses
    as::

        celldiag inventory: 70 (code,ver) key(s), 0 unrecognized, 1 silent
                            silent[0xB0C0/v20]

    and it is the *interesting* shape rather than a contrived one: `0xB0C0` is a
    cell-contract code, it decoded on every record, and it emitted nothing because
    an idle-searching receiver never picked up a SIB1. That is precisely the
    "a code Kismet depends on went quiet, and it looks exactly like no cells in
    range" scenario the escalation exists for.

    Prefers ``$CELLDIAG_SILENT_FIXTURE``; else the canonical
    ``$CELL_CAPTURES/celldiag_silent_only.hdlc[.zst]``.
    """
    env = os.environ.get("CELLDIAG_SILENT_FIXTURE")
    if env:
        src = Path(env)
        if not src.exists():
            pytest.skip(f"CELLDIAG_SILENT_FIXTURE={env!r} not found")
    else:
        plain = _captures_or_skip() / "celldiag_silent_only.hdlc"
        zst = _captures_or_skip() / "celldiag_silent_only.hdlc.zst"
        if plain.exists():
            src = plain
        elif zst.exists():
            src = zst
        else:
            pytest.skip(
                "no silent-only fixture "
                "($CELL_CAPTURES/celldiag_silent_only.hdlc[.zst])")

    return _materialize(src, tmp_path / "silent_only.hdlc")


def _mixed_lte_nr_fixture_or_skip(tmp_path: Path) -> Path:
    """The mixed LTE+NR capture the per-(code,version) census tests need.

    The first 20 MB of an RM520N-GL wardrive capture. That prefix is
    the *interesting* shape rather than a contrived one — it exercises every
    distinction the census draws, in one file::

        0xB0C0/v27  count=12  decoded=12  emitted=0   silent=True
        0xB192/v1   count=15  decoded=15  emitted=20
        0xB193/v1   count=22  decoded=22  emitted=22
        0xB821/v17  count=5   decoded=5   emitted=0
        0xB97F/v9   count=15  decoded=15  emitted=35   <- NR
        ... 242 distinct keys, 2 unrecognized, 3 silent

    So it carries **both RATs emitting**, **both identity codes present**
    (LTE ``0xB0C0`` and NR ``0xB821``), and **both census findings at once** —
    which is exactly why it cannot replace the silent-only fixture above: there,
    the point is `silent` with NO `unrecognized`, so the ERROR escalation can be
    attributed. Here the point is coverage breadth. Two fixtures, two jobs.

    Prefers ``$CELLDIAG_MIXED_FIXTURE``; else the canonical
    ``$CELL_CAPTURES/celldiag_mixed_lte_nr.hdlc[.zst]``.
    """
    env = os.environ.get("CELLDIAG_MIXED_FIXTURE")
    if env:
        src = Path(env)
        if not src.exists():
            pytest.skip(f"CELLDIAG_MIXED_FIXTURE={env!r} not found")
    else:
        plain = _captures_or_skip() / "celldiag_mixed_lte_nr.hdlc"
        zst = _captures_or_skip() / "celldiag_mixed_lte_nr.hdlc.zst"
        if plain.exists():
            src = plain
        elif zst.exists():
            src = zst
        else:
            pytest.skip(
                "no mixed LTE+NR fixture "
                "($CELL_CAPTURES/celldiag_mixed_lte_nr.hdlc[.zst])")

    return _materialize(src, tmp_path / "mixed_lte_nr.hdlc")


def _cellcfg_beams_fixture_or_skip(tmp_path: Path) -> Path:
    """The capture that exercises 0xB197 cell config AND beam-bearing 0xB97F.

    The other replay fixtures predate 0xB197 in the mask and carry no 0xB97F
    version the bridge decodes beams from, so on them the off-vs-on gate cannot
    see a cell-config or beam divergence. This is 8 minutes of an RM500Q-AE
    (SDX55) raw stream, LTE-pinned and PDP-cycled:

        0xB197/v2   16   one cell: EARFCN 66786 truncated to 1250, PCI 236
        0xB97F/v7   43   serving + neighbour rows, every one with a beam
        0xB193 1414, 0xB17F 723, 0xB195 415, 0xB0C0 208, 0xB192 167

    The truncated 0xB197 EARFCN is the point: the config is keyed on the low 16
    bits and must still reach the 66786 rows, through the alias guard.

    Prefers ``$CELLDIAG_CELLCFG_FIXTURE``; else the canonical
    ``$CELL_CAPTURES/celldiag_rm500q_cellcfg_beams.hdlc[.zst]``.
    """
    env = os.environ.get("CELLDIAG_CELLCFG_FIXTURE")
    if env:
        src = Path(env)
        if not src.exists():
            pytest.skip(f"CELLDIAG_CELLCFG_FIXTURE={env!r} not found")
    else:
        plain = _captures_or_skip() / "celldiag_rm500q_cellcfg_beams.hdlc"
        zst = _captures_or_skip() / "celldiag_rm500q_cellcfg_beams.hdlc.zst"
        if plain.exists():
            src = plain
        elif zst.exists():
            src = zst
        else:
            pytest.skip(
                "no cell-config + beams fixture "
                "($CELL_CAPTURES/celldiag_rm500q_cellcfg_beams.hdlc[.zst])")
    return _materialize(src, tmp_path / "cellcfg_beams.hdlc")


def _fixture_or_skip(tmp_path: Path) -> Path:
    """Materialize the raw-HDLC replay fixture (uncompressed) or skip.

    Prefers ``$CELLDIAG_REPLAY_FIXTURE``; else the canonical
    capture in ``$CELL_CAPTURES`` (decompressing the ``.zst`` if needed).
    Captures live outside the repo, so this can only run where they are present."""
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
            pytest.skip("no replay fixture ($CELL_CAPTURES/tmp_probe.dlf[.zst])")

    return _materialize(src, tmp_path / "replay.hdlc")


def _definition(replay_file: Path, imei: str = IMEI, **flags) -> str:
    parts = [f"replay={replay_file}"]
    parts += [f"{k}={v}" for k, v in flags.items()]
    return f"celldiag-{imei}:" + ",".join(parts)


# ── tests ──────────────────────────────────────────────────────────────────────
def test_replay_ab(tmp_path):
    """Leg A (binary over datasource IPC) == leg B (reference helper direct)."""
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    ref = _reference_observations(replay)
    assert ref, "reference decode produced 0 observations - bad fixture"

    run = CellDiagRun(binary, _definition(replay)).run()
    assert run.open_code in (1, None), (
        f"OPENREPORT failed (code={run.open_code}); errors={run.errors}; "
        f"open message={run.open_message!r}")
    assert not run.errors, f"binary reported errors: {run.errors}"

    assert _canon(run.observations) == _canon(ref), (
        f"A/B mismatch: binary relayed {len(run.observations)} obs, "
        f"reference {len(ref)}")


def test_replay_refuses_a_compressed_container(tmp_path):
    """A ``.zst`` handed to ``replay=`` is REFUSED loudly, not decoded.

    Captures are commonly stored as ``.dlf.zst`` / ``.hdlc.zst``. Without the
    check, the C deframer walks the COMPRESSED bytes: high-entropy data is full
    of ``0x7E`` flags, so frames get synthesized from compression noise and the
    occasional one passes CRC16. The source then opens cleanly with no errors
    and reports ``RX-NO-OBS | bytes=<n>``: the file read fine, and the health
    line reads as *"no cells in range"*, a working source on a quiet radio.

    The assertion is on the ERROR, not on the observation count: 0 observations
    is exactly what the unchecked path produces, so a test asserting
    ``obs == 0`` would pass against it."""
    binary = _binary_or_skip()
    plain = _fixture_or_skip(tmp_path)

    zstandard = pytest.importorskip("zstandard")
    comp = tmp_path / "replay.hdlc.zst"
    with plain.open("rb") as fp, comp.open("wb") as out:
        zstandard.ZstdCompressor().copy_stream(fp, out)

    run = CellDiagRun(binary, _definition(comp), timeout=30.0).run()

    assert run.errors, (
        "compressed replay= produced NO error - the binary decoded the "
        "compressed bytes silently; "
        f"observations={len(run.observations)}, messages={run.messages}")
    joined = " ".join(run.errors)
    assert "compressed container" in joined, (
        f"error does not name the cause; errors={run.errors}")
    assert "zstd -d" in joined, (
        f"error does not name the remedy; errors={run.errors}")
    assert not run.observations, (
        f"refused source still relayed {len(run.observations)} observations")


def test_enabled_false_is_idle(tmp_path):
    """enabled=false leaves the source DEFINED-but-idle: OPENREPORT
    succeeds, but no helper/port is touched and zero observations are relayed
    even though a replay= file is supplied."""
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    # Disabled sources never process the replay, so they never self-exit on EOF;
    # a short timeout lets the harness close the in-fd to spin the binary down.
    run = CellDiagRun(binary, _definition(replay, enabled="false"),
                      timeout=4.0).run()
    assert run.open_code in (1, None), (
        f"disabled source failed to open (code={run.open_code}); "
        f"errors={run.errors}; "
        f"open message={run.open_message!r}")
    assert run.observations == [], (
        f"enabled=false relayed {len(run.observations)} observations; "
        "expected an idle source")
    assert any("enabled=false" in m or "disabled" in m for m in run.messages), (
        f"no disabled-source status message seen; messages={run.messages}")


def _bridge_helper_pids() -> set[int]:
    """PIDs of any running Python bridge helper.

    The helper is `python3 <decoder root>/kismet_diag_decode.py`
    (spawn_helper, capture_cell_diag.c). Matching shares
    ``_bridge_helper_procs`` with the spawn watcher, whose positive control
    (``test_the_watcher_can_actually_see_a_helper``) covers both. A bare
    ``kismet_diag_decode`` stem would also match a pytest runner whose argv
    names ``test_kismet_diag_decode.py``.
    """
    return set(_bridge_helper_procs())


def _bridge_helper_procs() -> dict[int, str]:
    """``{pid: command line}`` of every running bridge helper, portably.

    Not ``pgrep -af``: on procps (Linux) ``-a`` means "list the full command
    line", but on BSD/macOS it means "include process ANCESTORS" and the output
    is bare PIDs, which leaves the argv allowlist below blind.
    ``ps axww -o pid=,args=`` means the same thing on both (``ww`` = no width
    truncation).

    An ALLOWLIST on argv: some token after argv[0] must END with
    ``/kismet_diag_decode.py``. That excludes a pytest runner running
    ``test_kismet_diag_decode.py`` (underscore, not slash, before ``kismet``)
    and a ``sh -c`` line (one long token) by construction. A denylist (skip
    lines naming ``pgrep`` or a shell) lets the pytest runner's own argv
    through, and a DISABLED source is then reported as having spawned a
    helper.
    """
    out = subprocess.run(["ps", "axww", "-o", "pid=,args="],
                         capture_output=True, text=True).stdout
    found: dict[int, str] = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        argv = parts[1].split()
        if any(tok.endswith("/kismet_diag_decode.py") for tok in argv[1:]):
            found[int(parts[0])] = parts[1]
    return found


def _no_new_helpers_within(before: set[int], seconds: float = 3.0) -> set[int]:
    """Wait up to `seconds` for the helper set to return to `before`.

    Reaping is ASYNCHRONOUS: the harness's ``run()`` returns when the capture
    binary exits, and the kernel may not have finished tearing down its child by
    then. A bare check immediately after ``run()`` would flag a transient as a
    leak. Poll instead, and let the assertion mean "still there after 3 s",
    which is a leak.
    """
    deadline = time.monotonic() + seconds
    extra = _bridge_helper_pids() - before
    while extra and time.monotonic() < deadline:
        time.sleep(0.1)
        extra = _bridge_helper_pids() - before
    return extra


def test_enabled_false_zero_is_caused_by_disabled_not_by_the_fixture(tmp_path):
    """The control that `test_enabled_false_is_idle` cannot run for itself.

    That test asserts `observations == []`. On its own, zero observations carries
    NO information about the cause. A broken fixture, a bad IMEI, or a parse
    regression all produce the same empty list, and the disabled path would
    look healthy through every one of them.

    So assert the DIFFERENCE, on the same fixture, in the same test: flipping ONE
    option must be the whole reason the stream is empty.
    """
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    enabled = CellDiagRun(binary, _definition(replay, enabled="true")).run()
    assert enabled.observations, (
        "the enabled control relayed 0 observations -- the fixture or binary is "
        "broken, and test_enabled_false_is_idle would pass for the wrong reason"
    )

    disabled = CellDiagRun(binary, _definition(replay, enabled="false"),
                           timeout=4.0).run()
    assert disabled.observations == [], (
        f"enabled=false relayed {len(disabled.observations)} observations")


class _HelperWatcher:
    """Samples for bridge-helper processes for the duration of a run.

    Why sampling, and not "is one still alive afterwards": a helper that is
    spawned and then reaped leaves nothing behind, so a leak detector cannot
    see it. The question is whether the disabled path does device work AT ALL
    ("registered but DISABLED, no DIAG port opened, no helper spawned"). That
    is a question about the spawn, so sample for it.
    """

    def __init__(self):
        self.seen: set[str] = set()
        self._stop = False
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        time.sleep(0.3)          # let a late spawn show up before we stop
        self._stop = True
        if self._thread:
            self._thread.join(timeout=5)
        return False

    def _poll(self):
        while not self._stop:
            # Match on the BASENAME, not a parent directory: the allowlist in
            # _bridge_helper_procs is what keeps this from over-matching, and
            # pinning the directory layout here would break on any move of the
            # bridge (test_the_watcher_can_actually_see_a_helper catches that).
            # The portable process listing also lives there; see its docstring
            # for why it is an allowlist and not `pgrep -af`.
            for _pid, cmdline in _bridge_helper_procs().items():
                self.seen.add(cmdline)
            time.sleep(0.03)


def test_enabled_false_spawns_no_bridge_helper(tmp_path):
    """A disabled source opens no DIAG port and spawns no helper.

    ``enabled=false`` sets ``diag_fd = -1`` and the ``disabled`` mask preset, but
    it does NOT clear ``local->replay_path``: the option was parsed, and
    parsing is not opening. The replay bring-up block must therefore check
    ``disabled`` itself, or an ``enabled=false,replay=<file>`` source forks
    ``kismet_diag_decode.py`` while its status message reports "no helper
    spawned".

    Scoped to replay: a LIVE disabled source has no ``replay_path`` and never
    reaches that block. Replay is the configuration used to verify the disabled
    path offline, which is why this must hold there.
    """
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    with _HelperWatcher() as w:
        CellDiagRun(binary, _definition(replay, enabled="false"),
                    timeout=4.0).run()
    assert not w.seen, (
        "a DISABLED source spawned the bridge helper: "
        f"{sorted(w.seen)!r} -- the open message claims none is spawned")


def test_the_watcher_can_actually_see_a_helper(tmp_path):
    """The control for the test above, without which it is unfalsifiable.

    An empty ``w.seen`` proves nothing unless the same watcher, on the same
    binary and fixture, DOES observe a helper when one is genuinely spawned. An
    ENABLED replay source spawns one by design, so it is the natural positive
    control -- and if a future refactor made the helper unobservable to `ps`
    (renamed, re-exec'd, moved in-process), this fails rather than letting the
    disabled assertion pass vacuously forever.
    """
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    with _HelperWatcher() as w:
        run = CellDiagRun(binary, _definition(replay, enabled="true")).run()
    assert run.observations, "the enabled control decoded nothing"
    assert w.seen, (
        "the watcher saw NO helper during an enabled run -- it cannot detect a "
        "spawn, so test_enabled_false_spawns_no_bridge_helper is vacuous")


def test_no_bridge_helper_outlives_a_run(tmp_path):
    """Separately from the spawn question: nothing may SURVIVE teardown.

    No leaked PIDs, and so no helpers accumulating across restarts -- distinct
    from the spawn question above, and it needs the settle poll because reaping
    is asynchronous.
    """
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    before = _bridge_helper_pids()
    CellDiagRun(binary, _definition(replay, enabled="true")).run()
    assert _no_new_helpers_within(before) == set(), (
        "a bridge helper outlived an ENABLED source -- the `done:` teardown did "
        "not reap it, so a restart accumulates helpers")


def test_restart_reproduces_the_first_run_exactly(tmp_path):
    """Restart = close -> reopen, handshake re-run, no degradation.

    The capture framework has no in-process pause/resume callback, so runtime
    control is server-mediated: the server REAPS this process to disable and
    RESPAWNS it to re-enable. That means a restart is exactly two sequential
    processes over the same definition -- which this harness can drive offline.

    Identical output is the assertion, not merely non-empty output: a second open
    that re-ran the handshake WRONG (stale mask, half-applied config) would still
    produce plausible observations, just fewer or different ones.
    """
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    first = CellDiagRun(binary, _definition(replay)).run()
    assert first.observations, "first open relayed 0 observations"

    second = CellDiagRun(binary, _definition(replay)).run()
    assert _canon(second.observations) == _canon(first.observations), (
        f"restart changed the output: {len(first.observations)} obs then "
        f"{len(second.observations)}"
    )


def test_a_disabled_source_does_not_perturb_a_sibling(tmp_path):
    """Control is independent per source instance.

    Each source is its own process, so this is close to true by construction --
    which is why it is worth a cheap check rather than an expensive one. The
    failure it would catch is shared MUTABLE state outside the process: a rawlog
    path, a lock file, or a device node one source grabs and the other then
    cannot. Run a disabled sibling on a DIFFERENT IMEI first, then the enabled
    source, and require byte-identical output to the solo run.
    """
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    solo = CellDiagRun(binary, _definition(replay)).run()
    assert solo.observations, "solo run relayed 0 observations"

    CellDiagRun(binary, _definition(replay, imei="000000000000000",
                                    enabled="false"), timeout=4.0).run()
    after = CellDiagRun(binary, _definition(replay)).run()

    assert _canon(after.observations) == _canon(solo.observations), (
        "a disabled sibling source changed this source's output"
    )


def test_rawlog_roundtrip(tmp_path):
    """rawlog tee is byte-identical to the replay input, and re-replaying it
    reproduces the same observations."""
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)
    raw_out = tmp_path / "tee.raw"

    first = CellDiagRun(binary, _definition(replay, rawlog=raw_out)).run()
    assert first.open_code in (1, None), (
        f"OPENREPORT failed (code={first.open_code}); errors={first.errors}; "
        f"open message={first.open_message!r}")
    assert raw_out.exists(), "rawlog file was not created"
    assert raw_out.read_bytes() == replay.read_bytes(), (
        "rawlog tee is not byte-identical to the replay input")

    second = CellDiagRun(binary, _definition(raw_out)).run()
    assert _canon(second.observations) == _canon(first.observations), (
        "re-replaying the rawlog tee did not reproduce identical observations")


def test_inventory_relay(tmp_path):
    """The per-(code,version) inventory census reaches Kismet as a
    MESSAGE, never as a cell_observation.

    The C binary spawns the decode helper *without* ``--no-inventory``, so the
    helper emits a periodic ``diag_inventory`` line (distinct from
    ``cell_observation`` by its ``type`` field). ``drain_helper_lines`` routes
    that line to the message bus via its pre-rendered ``status`` one-liner
    (``capture_cell_diag.c`` ~L1356) rather than to ``cf_send_json``. This
    exercises the helper-map -> C-relay path end-to-end on the real binary."""
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay)).run()
    assert run.open_code in (1, None), (
        f"OPENREPORT failed (code={run.open_code}); errors={run.errors}; "
        f"open message={run.open_message!r}")

    # The census surfaces as a status MESSAGE ("celldiag inventory: N (code,ver)
    # key(s), ...") — not as an observation.
    inv_msgs = [m for m in run.messages if "inventory:" in m and "(code,ver)" in m]
    assert inv_msgs, (
        f"no diag_inventory status message relayed; messages={run.messages}")

    # It must NOT leak into the observation stream as a fake cell_observation.
    for js in run.observations:
        assert '"type":"diag_inventory"' not in js.replace(" ", ""), (
            "diag_inventory census leaked into the cell_observation relay path")


def _census_lines(run) -> "list[dict]":
    """The relayed ``diag_census`` control objects, oldest first."""
    out = []
    for js in run.control:
        try:
            o = json.loads(js)
        except json.JSONDecodeError:
            continue
        if isinstance(o, dict) and o.get("type") == "diag_census":
            out.append(o)
    return out


def test_the_census_table_reaches_kismet_on_its_own_control_line(tmp_path):
    """The census table, asserted on its CONTENT, not on the line arriving.

    The assertion that matters is that a row parses into the seven documented
    fields. "A diag_census line came back" stays green when the wiring is right
    and every value is empty. So this parses a row.

    Its own line rather than a key on diag_stats because the table does not FIT:
    24 rows of a realistic census measure 619 bytes against a 768-byte extras
    budget, and adding it degrades by silently dropping ``rawlog_path``.
    """
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay, stats_interval=1),
                      timeout=180.0).run()
    assert run.open_code in (1, None), (
        f"OPENREPORT failed (code={run.open_code}); errors={run.errors}; "
        f"open message={run.open_message!r}")

    census = _census_lines(run)
    assert census, (
        f"no diag_census control line relayed; control types="
        f"{[c[:40] for c in run.control[:5]]}")

    table = census[-1].get("table", "")
    assert table, f"the census line carried an EMPTY table: {census[-1]}"

    rows = table.split(";")
    for row in rows:
        cols = row.split(",")
        assert len(cols) == 7, (
            f"row {row!r} has {len(cols)} columns, expected the documented 7 "
            f"(code/vN,count,decoded,dropped,emitted,enrich_failed,flags)")
        assert cols[0].startswith("0x") and "/v" in cols[0], (
            f"row key {cols[0]!r} is not the documented `0xNNNN/vN` form")
        for i, c in enumerate(cols[1:6], start=1):
            assert c.isdigit(), f"column {i} of {row!r} is not a count: {c!r}"
        assert set(cols[6]) <= set("sue-"), (
            f"row {row!r} has flags outside the documented alphabet: {cols[6]!r}")

    # And it must NOT be counted as a cell observation, or it would be compared
    # byte-for-byte against a leg B that never produces one.
    for js in run.observations:
        assert "diag_census" not in js, (
            "the census table leaked into the cell_observation relay path")


def test_the_census_table_is_bounded_and_says_so(tmp_path):
    """A bounded table that looks complete is worse than no table.

    The cap rides beside ``inventory_distinct_codes`` on the stats line
    precisely so a panel can render "showing N of M". This asserts the two
    numbers are both present and consistent -- rows never EXCEED the cap, and
    never exceed the distinct count either (a table longer than the census it
    summarises would mean the two are measuring different things).
    """
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay, stats_interval=1),
                      timeout=180.0).run()
    census = _census_lines(run)
    stats = _diag_stats_lines(run)
    if not census or not stats:
        pytest.skip("no census/stats pair relayed on this fixture")

    rows = [r for r in census[-1]["table"].split(";") if r]
    distinct = stats[-1].get("inventory_distinct_codes", 0)

    assert len(rows) <= 24, f"table exceeded the producer's 24-row cap: {len(rows)}"
    assert distinct > 0, (
        "the census reported a table but inventory_distinct_codes is 0 -- the "
        "panel's 'showing N of M' denominator is missing, so the cap would be "
        "SILENT")
    assert len(rows) <= distinct, (
        f"{len(rows)} table rows against {distinct} distinct keys -- the table "
        f"and the count are not measuring the same census")


def test_inventory_spans_both_rats(tmp_path):
    """The mixed **LTE + NR** census, offline.

    Why the breadth matters rather than just the count: the census's job is to
    say *which* (code, version) keys a source is seeing and which went quiet, and
    an LTE-only capture cannot exercise the NR half of the cell contract at all.
    On this fixture the census reaches **both** measurement families
    (``0xB193``/``0xB192`` LTE, ``0xB97F`` NR) **and** both identity codes
    (``0xB0C0`` LTE, ``0xB821`` NR) — where the LTE identity code is *silent*
    (12 records decoded, 0 emitted, no SIB1) while the NR one decodes and, by
    design, self-emits nothing. Those are three different reasons a code can
    contribute zero observations, all present at once.

    This does **not** replace the reference fixture or the silent-only one.
    The silent-only fixture exists precisely because it has `silent` keys and NO
    `unrecognized` ones, so the ERROR escalation can be attributed to `silent`;
    this fixture has both, and would re-open that ambiguity if used for it.
    """
    binary = _binary_or_skip()
    replay = _mixed_lte_nr_fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay, stats_interval=1),
                      timeout=180.0).run()
    assert run.open_code in (1, None), (
        f"OPENREPORT failed (code={run.open_code}); errors={run.errors}; "
        f"open message={run.open_message!r}")
    assert not run.errors, f"binary reported errors: {run.errors}"

    obs = _canon(run.observations)
    rats = {o.get("rat") for o in obs}
    assert rats == {"LTE", "NR"}, (
        f"fixture did not produce BOTH rats — got {rats}. This is the whole "
        "point of the mixed fixture; an LTE-only capture leaves the NR half of "
        "the cell contract unexercised.")

    origins = {o["prov"]["origin"] for o in obs}
    assert "0xB97F" in origins, (
        f"no NR measurement observations (0xB97F); origins={sorted(origins)}")
    assert origins & {"0xB193", "0xB192", "0xB195"}, (
        f"no LTE measurement observations; origins={sorted(origins)}")

    # The census must reach the datasource with a real key count, not a
    # permanent 0.
    stats = _diag_stats_lines(run)
    assert stats, "no diag_stats object relayed"
    last = stats[-1]
    assert last.get("inventory_distinct_codes", 0) > 0, (
        f"inventory_distinct_codes is 0 on a mixed-RAT fixture: {last}")

    # And the human census line must actually name a silent key here: 0xB0C0
    # decodes on every record and emits nothing (no SIB1 in this window), which
    # is the "a code Kismet depends on went quiet" case, on the LTE identity
    # code, in a capture whose NR side is healthy.
    inv_msgs = [m for m in run.messages if "inventory:" in m and "(code,ver)" in m]
    assert inv_msgs, f"no census status message relayed; messages={run.messages[:8]}"
    assert any("silent[" in m for m in inv_msgs), (
        f"census reported no silent key on a fixture that has one: {inv_msgs[-1]}")


def test_mask_invalid_rejected(tmp_path):
    """An unknown ``mask=`` preset is a hard error at open, rejected
    before any live/replay work — surfaced as an OPENREPORT failure carrying the
    'Unknown mask preset' text, with zero observations relayed."""
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay, mask="bogus"), timeout=6.0).run()
    assert run.open_code == 0, (
        f"mask=bogus should fail open (code 0); got {run.open_code}")
    assert run.observations == [], (
        f"rejected source relayed {len(run.observations)} observations")
    assert run.open_message and "mask preset" in run.open_message.lower(), (
        f"open failure did not name the bad mask preset: {run.open_message!r}")


def test_enabled_invalid_rejected(tmp_path):
    """An unknown ``enabled=`` value is a hard error at open, rejected in the
    same pre-live/replay-split parse as ``mask=``/``f3=`` (capture_cell_diag.c:
    ``Unknown enabled value '%s' (expected true|false)``). A parse regression
    that silently accepted a bogus ``enabled=`` (defaulting it to enabled, or
    worse to disabled) would otherwise go uncaught. Bogus value -> OPENREPORT
    failure naming the bad value, zero observations."""
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay, enabled="bogus"),
                      timeout=6.0).run()
    assert run.open_code == 0, (
        f"enabled=bogus should fail open (code 0); got {run.open_code}")
    assert run.observations == [], (
        f"rejected source relayed {len(run.observations)} observations")
    assert run.open_message and "enabled value" in run.open_message.lower(), (
        f"open failure did not name the bad enabled value: {run.open_message!r}")


def test_mask_wardrive_explicit_is_default(tmp_path):
    """Explicit ``mask=wardrive`` relays exactly the same observations as the
    unset default (wardrive is the default, byte-identical path).
    On the replay path the LOG_CONFIG handshake is inert (no modem), so this
    proves the *option parse* itself doesn't perturb the relay stream."""
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    default = CellDiagRun(binary, _definition(replay)).run()
    explicit = CellDiagRun(binary, _definition(replay, mask="wardrive")).run()
    assert _canon(explicit.observations) == _canon(default.observations), (
        "explicit mask=wardrive diverged from the unset default")


def test_mask_full_opens_and_relays(tmp_path):
    """The ``mask=full`` heavy preset parses and opens cleanly on the
    compiled binary, relaying the same observations as the default.

    ``full`` is the only mask preset whose *effect* is live-only: the broad
    LOG_CONFIG handshake it triggers runs in the non-replay ``else`` branch, so
    on the replay path it is inert and the observation stream is identical to
    ``wardrive``. But its **parse** (``capture_cell_diag.c`` L1013-1024) runs
    *before* the live/replay split, so a broken ``full`` parse would fail open
    here even without a modem. The sibling ``test_mask_invalid_rejected`` proves
    a bad preset is refused; this proves the accepted heavy preset is not — the
    two together pin the whole ``mask=`` parse surface on the built binary."""
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    default = CellDiagRun(binary, _definition(replay)).run()
    full = CellDiagRun(binary, _definition(replay, mask="full")).run()
    assert full.open_code in (1, None), (
        f"mask=full failed to open (code={full.open_code}); errors={full.errors}; "
        f"open message={full.open_message!r}")
    assert not full.errors, f"mask=full reported errors: {full.errors}"
    assert _canon(full.observations) == _canon(default.observations), (
        "mask=full diverged from the unset default on the replay stream "
        "(the LOG handshake is live-only, so replay observations must match)")


def test_mask_full_log_alias(tmp_path):
    """``mask=full-log`` is an accepted ALIAS for ``mask=full``
    (``capture_cell_diag.c``: ``strcmp(v,"full")||strcmp(v,"full-log")``).
    The alias is validated in the same pre-split parse as ``full``; a refactor
    that dropped it would turn a documented, accepted option value into a hard
    OPENREPORT failure. Pin it:
    ``full-log`` must open cleanly (NOT rejected like a bogus preset) and, since
    the heavy handshake is live-only, relay the same replay stream as the
    default -- identical to how ``test_mask_full_opens_and_relays`` pins ``full``."""
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    default = CellDiagRun(binary, _definition(replay)).run()
    alias = CellDiagRun(binary, _definition(replay, mask="full-log")).run()
    assert alias.open_code in (1, None), (
        "mask=full-log (alias of full) failed to open "
        f"(code={alias.open_code}); errors={alias.errors}; "
        f"open message={alias.open_message!r}")
    assert not alias.errors, f"mask=full-log reported errors: {alias.errors}"
    # Must NOT be refused the way a bogus preset is (test_mask_invalid_rejected).
    assert not any("Unknown mask preset" in m
                   for m in alias.messages + [alias.open_message or ""]), (
        "mask=full-log was rejected as an unknown preset; the alias regressed")
    assert _canon(alias.observations) == _canon(default.observations), (
        "mask=full-log diverged from the unset default on the replay stream")


def test_f3_invalid_rejected(tmp_path):
    """An unknown ``f3=`` preset is a hard error at open, same pre-split
    validation as mask=. OPENREPORT fails naming the bad f3 preset; no obs."""
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay, f3="bogus"), timeout=6.0).run()
    assert run.open_code == 0, (
        f"f3=bogus should fail open (code 0); got {run.open_code}")
    assert run.observations == [], (
        f"rejected source relayed {len(run.observations)} observations")
    assert run.open_message and "f3 preset" in run.open_message.lower(), (
        f"open failure did not name the bad f3 preset: {run.open_message!r}")


def test_f3_off_explicit_is_default(tmp_path):
    """Explicit ``f3=off`` (the default) relays the same observations as unset
    (default off, composes with mask=). Proves the f3 parse is inert on
    the relay stream when off."""
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    default = CellDiagRun(binary, _definition(replay)).run()
    explicit = CellDiagRun(binary, _definition(replay, f3="off")).run()
    assert _canon(explicit.observations) == _canon(default.observations), (
        "explicit f3=off diverged from the unset default")


@functools.lru_cache(maxsize=1)
def _helper_relays_f3() -> bool:
    """Does the resolved bridge advertise ``--f3``?

    A BRANCH, NOT A SKIP. ``f3_relay`` and ``f3_levels`` are separate
    mechanisms, and only the relay half depends on the bridge -- so a checkout
    whose bridge has no ``--f3`` does not make these tests unmeasurable, it
    makes them measure the OTHER branch: that the source armed the mask anyway,
    said so in the readout as ``<preset>+norelay``, and did not pretend to be
    relaying. Skipping here would convert a real, checkable contract into a
    silence, on the majority configuration.

    Probed the same way the C side probes it, for the same reason
    ``HELPER_SCRIPT_RELS`` is shared: two legs that answer "can this helper
    relay F3" differently are testing a different program than they run.
    """
    oracle = decode_oracle.discover()
    if not oracle.ok:
        return False
    out = subprocess.run(oracle.cmd("--help"), env=oracle.env(),
                         capture_output=True, text=True)
    if out.returncode != 0:
        return False
    # Match the option COLUMN. argparse prints the module docstring as its
    # description, and this bridge's docstring contains the sentence "There is
    # no `--f3` flag" — so a substring test answers YES on precisely the helper
    # that cannot relay. Same trap, same fix, as the C side's probe.
    return any(line.strip().startswith("--f3")
               for line in (out.stdout + out.stderr).splitlines())


def _f3_relay_messages(run) -> list[str]:
    """The relayed F3 records: message-bus lines that are not the census/stats
    control lines the source emits on its own."""
    return [
        m for m in run.messages
        if "inventory:" not in m and "celldiag stats:" not in m
        and "nativedecode" not in m and "ARMED BUT NOT RELAYED" not in m
    ]


def _assert_armed_but_not_relayed(run, default, preset: str):
    """The other branch: armed, honest about not relaying, and still decoding.

    Reporting ``f3_preset`` as the bare preset name while relaying nothing is a
    quiet lie, and passing an unsupported ``--f3`` kills the bridge at argparse
    and takes the whole decode with it. Every clause here guards one of those.
    """
    stats = _diag_stats_lines(run)
    assert stats, "no diag_stats object relayed"
    assert all(s.get("f3_preset") == f"{preset}+norelay" for s in stats), (
        f"a source that cannot relay F3 reported its preset as if it could — "
        f"expected {preset!r}+norelay: {stats}")
    # Against the `f3=off` CONTROL, not against an empty set. The filter above
    # is a denylist of the control lines this source emits on its own, which is
    # enough to answer "is there anything here" (the relay branch's question)
    # and NOT enough to answer "is there nothing extra here" — the open message
    # and the per-fix GPS lines are neither F3 nor listed in it. The honest form
    # of "no F3 was relayed" is "the same message traffic as with F3 off".
    assert len(_f3_relay_messages(run)) == len(_f3_relay_messages(default)), (
        f"an unrelayable f3={preset} still added message traffic: "
        f"{len(_f3_relay_messages(run))} vs {len(_f3_relay_messages(default))} "
        f"with F3 off")
    announced = [m for m in run.messages if "ARMED BUT NOT RELAYED" in m]
    assert announced, (
        f"the armed-but-unrelayable state was never announced: "
        f"{run.messages[:10]}")
    # The remedy has to be one the operator can carry out: no bridge ships
    # --f3 (see the bridge's own docstring), so it must not point at one.
    for m in announced:
        assert "ships --f3" not in m, f"remedy names a bridge that does not exist: {m}"
        assert "rawlog=" in m and "rawpackets" in m, (
            f"the announcement does not say where the F3 prints DO go: {m}")


def test_f3_all_opens_and_relays(tmp_path):
    """The ``f3=all`` heavy preset opens cleanly AND actually relays F3.

    The option drives two different mechanisms. **F3 arming**
    (``SET_ALL_RT_MASKS`` sent to a modem) is live-only; you cannot arm a file.
    **F3 relay** (``--f3`` to the helper, which decodes F3 records already
    present in the byte stream) must work on replay, because a capture taken
    with F3 armed *contains* those records. Comparing ``f3=all`` output against
    the default alone cannot tell a working relay from a disabled one, so this
    asserts the relay actually happens.

    It also asserts observation equality with F3 off. F3 output is far heavier
    than the cell stream, and the C side must drain helper stdout to
    ``EAGAIN`` on every poll pass: draining a fixed chunk while
    blocking-writing stdin lets the helper's stdout pipe fill, the helper stops
    reading stdin, and both processes stall (a mutual pipe deadlock). On this
    fixture that shows up as fewer observations, not as slowness: the first
    cell record lies well into the file.
    """
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    default = CellDiagRun(binary, _definition(replay)).run()
    allf3 = CellDiagRun(
        binary, _definition(replay, f3="all", stats_interval=1), timeout=60.0
    ).run()
    assert allf3.open_code in (1, None), (
        f"f3=all failed to open (code={allf3.open_code}); errors={allf3.errors}; "
        f"open message={allf3.open_message!r}")
    assert not allf3.errors, f"f3=all reported errors: {allf3.errors}"

    if not _helper_relays_f3():
        # NOT a skip — the other branch, fully asserted. See
        # _helper_relays_f3. Clause 4 below still runs for both.
        _assert_armed_but_not_relayed(allf3, default, "all")
        assert _canon(allf3.observations) == _canon(default.observations), (
            "f3=all changed the cell stream even with the relay unavailable")
        return

    # 1. The source knows it armed F3, on replay too.
    stats = _diag_stats_lines(allf3)
    assert stats, "no diag_stats object relayed"
    assert all(s.get("f3_preset") == "all" for s in stats), (
        f"f3=all did not reach the source's own state: {stats}")

    # 2. F3 records actually reach the message bus. The helper renders each into
    #    a `diag_f3` line whose pre-rendered "status" is relayed as a message —
    #    so a non-empty set here is the proof `--f3` was passed at all.
    f3_msgs = _f3_relay_messages(allf3)
    assert f3_msgs, (
        "f3=all relayed no F3 record — the helper was spawned without --f3. "
        f"messages={allf3.messages[:10]}")

    # 3. And it must never leak into the cell stream as a fake observation.
    for js in allf3.observations:
        assert '"type":"diag_f3"' not in js.replace(" ", ""), (
            "a diag_f3 record leaked into the cell_observation relay path")

    # 4. Arming F3 must not cost a single cell observation. A pipe deadlock
    #    between the C side and the helper shows up here as the f3=all run
    #    finishing early with fewer observations, not as a timeout.
    assert _canon(allf3.observations) == _canon(default.observations), (
        f"f3=all changed the cell stream: {len(allf3.observations)} observations "
        f"vs {len(default.observations)} with F3 off. Arming a debug log preset "
        "must not cost cell decode.")


@pytest.mark.parametrize("preset", ["high", "error"])
def test_f3_severity_floor_presets(tmp_path, preset):
    """The subtractive severity floors ``f3=high`` / ``f3=error``.

    These are the option surface for a runtime-level mask other than all-ones:
    ``high`` sends ``0xFFFFFFFC`` and ``error`` sends ``0xFFFFFFF8``, each
    clearing low *ladder* bits while retaining every per-SSID custom bit.

    **On a replay source a floor changes nothing about the relayed stream,
    and that is correct, not a gap.** The mask is enforced by the *modem*, at
    arm time — there is no helper-side re-filter, so the floor a capture was
    taken with is a property of the capture. What this test can pin offline is
    that the source resolves, reports, and carries a *distinct* preset rather
    than collapsing every armed state to "all".

    The `f3_preset` assertion is the load-bearing one. A boolean re-rendered as
    a name (``f3_relay ? "all" : "off"``) would report a floored stream as
    ``all`` on the web-UI panel, telling the operator they are seeing the full
    trace while part of it is silenced at the modem.
    """
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    default = CellDiagRun(binary, _definition(replay)).run()
    run = CellDiagRun(
        binary, _definition(replay, f3=preset, stats_interval=1), timeout=60.0
    ).run()
    assert run.open_code in (1, None), (
        f"f3={preset} failed to open (code={run.open_code}); errors={run.errors}; "
        f"open message={run.open_message!r}")
    assert not run.errors, f"f3={preset} reported errors: {run.errors}"

    if not _helper_relays_f3():
        # The floor still has to survive into the readout as its OWN name —
        # `high+norelay`, never `all+norelay` — so a floor collapsing to "all"
        # is caught on this branch too, not just the relay one.
        _assert_armed_but_not_relayed(run, default, preset)
        assert _canon(run.observations) == _canon(default.observations), (
            f"f3={preset} changed the cell stream with the relay unavailable")
        return

    stats = _diag_stats_lines(run)
    assert stats, "no diag_stats object relayed"
    assert all(s.get("f3_preset") == preset for s in stats), (
        f"f3={preset} did not reach the source's own state as its own name "
        f"(a floor must not be reported as 'all'): {stats}")

    # Relay is on for every non-off preset — a floor is not a mute.
    f3_msgs = _f3_relay_messages(run)
    assert f3_msgs, (
        f"f3={preset} relayed no F3 record — the helper was spawned without "
        f"--f3, i.e. the floor was read as 'off'. messages={run.messages[:10]}")

    # And, as with f3=all, arming must not cost a cell observation.
    assert _canon(run.observations) == _canon(default.observations), (
        f"f3={preset} changed the cell stream: {len(run.observations)} "
        f"observations vs {len(default.observations)} with F3 off.")


@pytest.mark.parametrize("bad", ["0xFFFFFFF8", "24", "fatal", "errors"])
def test_f3_rejects_raw_masks_and_selective_spellings(tmp_path, bad):
    """``f3=`` takes NAMED presets only — no raw mask, no invented severity word.

    This is a deliberate narrowing of the option surface: the intuitive
    *selective* floor ``0x00000018`` ("ERROR|FATAL") keeps only about 1% of a
    real F3 stream, because most records come from sites carrying only
    per-SSID custom bits. It would be a correct-looking option that silences
    the log.

    Accepting a raw hex/decimal mask would hand an operator exactly that footgun
    with no way for the source to warn about it; accepting ``fatal`` would imply
    a ladder-only selection the presets deliberately do not offer. Both fail at
    open, naming the value.
    """
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay, f3=bad), timeout=6.0).run()
    assert run.open_code == 0, (
        f"f3={bad} should fail open (code 0); got {run.open_code}")
    assert run.observations == [], (
        f"rejected source relayed {len(run.observations)} observations")
    assert run.open_message and "f3 preset" in run.open_message.lower(), (
        f"open failure did not name the bad f3 preset: {run.open_message!r}")


def test_rawlog_dir_templating(tmp_path):
    """A trailing-slash ``rawlog=<dir>/`` spec synthesizes a per-modem
    ``celldiag-<imei>-<ts>.hdlc`` file inside the directory, so concurrent per-modem sources on one host don't collide.
    The synthesized file is byte-identical to the replay input."""
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    outdir = tmp_path / "rawcaps"
    outdir.mkdir()
    # trailing slash => directory mode => auto-filename celldiag-<imei>-<ts>.hdlc
    run = CellDiagRun(binary, _definition(replay, rawlog=f"{outdir}/")).run()
    assert run.open_code in (1, None), (
        f"OPENREPORT failed (code={run.open_code}); errors={run.errors}; "
        f"open message={run.open_message!r}")

    produced = list(outdir.glob(f"celldiag-{IMEI}-*.hdlc"))
    assert len(produced) == 1, (
        f"expected exactly one auto-named rawlog in {outdir}, got {produced}")
    assert produced[0].read_bytes() == replay.read_bytes(), (
        "dir-templated rawlog is not byte-identical to the replay input")


def test_enabled_false_ignores_rawlog(tmp_path):
    """A disabled source opens NOTHING: even with a ``rawlog=`` sink
    requested, the disabled path never resolves the path nor spawns the helper,
    so no capture file is written. Guards the 'DEFINED-but-idle opens no
    fds/sinks' contract against a future refactor that resolves rawlog too early
    (``capture_cell_diag.c`` gates rawlog on ``!local->disabled``, ~L1265)."""
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    outdir = tmp_path / "should_stay_empty"
    outdir.mkdir()
    run = CellDiagRun(binary, _definition(replay, enabled="false",
                                          rawlog=f"{outdir}/"), timeout=4.0).run()
    assert run.open_code in (1, None), (
        f"disabled source failed to open (code={run.open_code}); "
        f"errors={run.errors}; "
        f"open message={run.open_message!r}")
    assert run.observations == [], "disabled source relayed observations"
    produced = list(outdir.glob("*"))
    assert produced == [], (
        f"disabled source wrote rawlog file(s) it should not have: {produced}")


def test_two_sources_independent(tmp_path):
    """Independent per source instance (multi-modem host): two
    celldiag sources with DISTINCT IMEIs, run CONCURRENTLY off the same replay,
    each relay the full observation set attributed to their OWN imei with zero
    cross-talk.

    Kismet spawns one capture-helper process per source, so isolation is
    architecturally a per-process property — but that is exactly why an explicit
    concurrent test is worth keeping: the day a shared resource is introduced (a
    fixed ``/tmp`` rawlog/scratch path, a hardcoded helper socket, a singleton
    lockfile), process isolation silently breaks and only a two-source run
    catches it. Observation COUNT is IMEI-independent (same fixture bytes → same
    records), so both streams must match the single-source reference count, and
    every observation must carry its own source's ``prov.imei`` and no other."""
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    ref_n = len(_reference_observations(replay))
    assert ref_n > 0, "reference decode produced 0 observations - bad fixture"

    imei_a, imei_b = "111111111111111", "222222222222222"
    runs: dict[str, CellDiagRun] = {}

    def _go(imei: str):
        runs[imei] = CellDiagRun(binary, _definition(replay, imei=imei)).run()

    threads = [threading.Thread(target=_go, args=(imei,))
               for imei in (imei_a, imei_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)
    assert not any(t.is_alive() for t in threads), "a source run hung"

    for imei in (imei_a, imei_b):
        run = runs.get(imei)
        assert run is not None, f"source {imei} produced no run object"
        assert run.open_code in (1, None), (
            f"source {imei} OPENREPORT failed (code={run.open_code}); "
            f"errors={run.errors}; "
        f"open message={run.open_message!r}")
        assert not run.errors, f"source {imei} reported errors: {run.errors}"
        obs = [json.loads(o) for o in run.observations]
        assert len(obs) == ref_n, (
            f"source {imei} relayed {len(obs)} observations; "
            f"expected {ref_n} (the single-source reference count)")
        seen_imeis = {o.get("prov", {}).get("imei") for o in obs}
        assert seen_imeis == {imei}, (
            f"source {imei} relayed foreign imei(s) {seen_imeis - {imei}} — "
            "concurrent sources are cross-contaminating attribution")


# ── nativedecode= shadow tap ──────────────────────────────────────────────────
def test_nativedecode_shadow_does_not_change_emitted_data(tmp_path):
    """Arming the in-process native decode tap changes NOTHING that is emitted.

    ``nativedecode=shadow`` runs the same byte stream through the C extractor
    (``diag_logstream``) and the native C++ legs (``diag_native_decode``) to
    MEASURE what a python-free helper would cover -- while the Python bridge
    stays the sole source of emitted observations.

    This is the safety property that makes the tap deployable on live hardware
    before the switch-over exists: if arming it perturbed the output by even one
    observation, the measurement it produces could not be trusted to describe the
    unarmed path. Asserted against the SAME run's unarmed twin, not against a
    stored expectation, so it also cannot pass by both sides being broken the
    same way in a way a fixture update would hide.
    """
    binary = _native_binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    off = CellDiagRun(binary, _definition(replay)).run()
    shadow = CellDiagRun(binary, _definition(replay, nativedecode="shadow")).run()

    assert not off.errors, f"unarmed run reported errors: {off.errors}"
    assert not shadow.errors, f"shadow run reported errors: {shadow.errors}"
    assert off.observations, "unarmed run produced 0 observations - bad fixture"

    assert _canon(shadow.observations) == _canon(off.observations), (
        f"nativedecode=shadow perturbed the emitted data: "
        f"{len(shadow.observations)} obs armed vs {len(off.observations)} unarmed")


def test_nativedecode_shadow_reports_real_native_coverage(tmp_path):
    """The tap actually decodes: it reports a non-trivial native share.

    Without this, the test above would pass just as well against a tap that
    silently did nothing -- which is precisely the failure mode being guarded,
    since "changed nothing" is also what a no-op looks like.

    The readout is a message-bus line of the form::

        <src> nativedecode=shadow | records=N native=M (P%) obs=K declined=D
              bridge-only=B | frames=F crc_bad=C wrapped=W

    Asserting on the parsed counters rather than the string keeps this pinned to
    the routing behaviour, not to the format.
    """
    binary = _native_binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay, nativedecode="shadow")).run()
    assert not run.errors, f"shadow run reported errors: {run.errors}"

    lines = [m for m in run.messages if "nativedecode=shadow |" in m]
    assert lines, (
        f"no nativedecode shadow readout on the message bus; messages={run.messages}")

    final = lines[-1]      # the teardown line: post-flush totals
    records = _kv_int(final, "records")
    native = _kv_int(final, "native")
    obs = _kv_int(final, "obs")
    bridge_only = _kv_int(final, "bridge-only")

    assert records > 0, f"the tap saw no LOG records at all: {final!r}"
    assert native > 0, (
        f"the tap recovered {records} records but routed NONE to a native leg -- "
        f"the legs are linked but never reached: {final!r}")
    assert obs > 0, (
        f"native legs ran on {native} records but produced 0 observations: {final!r}")
    assert native + bridge_only == records, (
        f"routing does not account for every record ({native} + {bridge_only} != "
        f"{records}) -- records are being lost between the two paths: {final!r}")


def test_nativedecode_shadow_routes_gnss_natively(tmp_path):
    """GNSS records reach the native leg, and are not counted as bridge-only.

    The tap must consult ``diag_native_has_gps_leg``; otherwise every 0x1476
    record falls through to ``fallback_records`` and reads as bridge-only.
    That is not a cosmetic miscount. Position is the last
    load-bearing job the Python bridge does -- Kismet geo-tags every DIAG cell
    observation from the cached fix -- so a shadow readout that showed the
    remaining migration surface as "the cell codes" was measuring one domain and
    reporting it as the whole.

    The assertions are on counters, not on a coverage number, because the
    reference fixture is a passive probe dump with six cell records total; what
    is pinned here is that the GNSS routing decision happens at all and that its
    arithmetic closes.
    """
    binary = _native_binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay, nativedecode="shadow")).run()
    assert not run.errors, f"shadow run reported errors: {run.errors}"

    lines = [m for m in run.messages if "nativedecode=shadow |" in m]
    assert lines, f"no nativedecode shadow readout; messages={run.messages}"

    def _field(line: str, key: str) -> int:
        for tok in line.replace("|", " ").split():
            if tok.startswith(f"{key}="):
                return int(tok.split("=", 1)[1])
        raise AssertionError(f"field {key!r} missing from readout: {line!r}")

    final = lines[-1]
    native = _field(final, "native")
    gnss = _field(final, "gnss")
    fixes = _field(final, "fixes")
    nofix = _field(final, "nofix")

    assert gnss > 0, (
        "the tap routed NO record to the GNSS leg. Either the reference capture "
        f"lost its 0x1476 records, or has_gps_leg is never consulted: {final!r}")
    assert fixes > 0, (
        f"the GNSS leg ran on {gnss} records and produced NO usable fix. On this "
        "fixture every 0x1476 record yields one, so 0 means the leg is declining "
        f"everything -- a silent failure: {final!r}")
    assert fixes + nofix == gnss, (
        f"GNSS outcomes do not account for every GNSS record ({fixes} + {nofix} "
        f"!= {gnss}): {final!r}")
    assert native >= gnss, (
        f"native={native} excludes the {gnss} GNSS records -- they are being "
        f"routed natively but reported as though they were not: {final!r}")

    # The cross-check that makes the number mean something. An independent
    # Python-side census of this same capture decomposes its 20 emitted records
    # as 16 gps_fix + 1 (0xB192) + 3 (0xB193). So the native leg must produce
    # exactly 16 fixes -- a DIFFERENT tool, over a DIFFERENT code path, landing
    # on the same figure. A drift here means one of the two paths changed
    # behaviour, which is precisely what a shadow tap is for.
    assert fixes == 16, (
        f"expected 16 native GNSS fixes on the reference capture (the Python "
        f"bridge emits 16 gps_fix records from it), got {fixes}: {final!r}")

    # 0x14D8 must NOT have acquired a leg. Its parser hardcodes lat/lon/alt to
    # 0.0, so a leg for it would be a decode path that is provably always empty;
    # its 53 records staying in bridge-only is correct, not an omission.
    armed = [m for m in run.messages if "shadow armed" in m]
    assert armed and "0x14D8" not in armed[0], (
        f"0x14D8 has acquired a GNSS leg. It cannot emit a fix (its parser "
        f"hardcodes position to 0.0): {armed!r}")


def test_native_enrichment_matches_the_bridge(tmp_path):
    """Native cross-record SIB1 enrichment == the bridge's ``_enrich``.

    **This is the one check no per-record harness can make.**

    The per-record byte-parity harnesses compare a decode LEG against
    ``result_to_observations(..., sib1_map={})`` — both pure functions of ONE
    record. The bridge is not: ``_LogEmitter.feed`` calls ``update_sib1_map``
    first, so a bare measurement inherits MCC/MNC/TAC/CellID from a SIB1 seen
    EARLIER in the stream. That is stream-level, so it lives in the shim
    (``diag_native_sib1_*``) above both sides of every per-record compare.

    Why it has to be a counter and not an observation diff: the shadow tap does
    not emit. But ``enriched`` is the right counter precisely because the failure
    mode is invisible to the others — enrichment changes no observation COUNT,
    only which FIELDS an observation carries. ``obs`` is byte-identical whether
    enrichment runs or not, so a regression here shows up in exactly one number.

    The bridge-side expectation subtracts the 0xB0C0 rows: those carry identity
    from their OWN record's SIB1, and ``result_to_observations`` deliberately does
    not call ``_enrich`` on that branch. Cross-record identity is
    ``with-identity minus 0xB0C0``, and that is what the native counter must equal.

    Runs on the mixed LTE+NR fixture so BOTH halves are covered — the LTE half
    (``0xB0C0`` seeding) and the NR half (``0xB821`` seeding, which is where
    all of this fixture's enrichment comes from).
    """
    binary = _native_binary_or_skip()
    replay = _mixed_lte_nr_fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay, nativedecode="shadow"),
                      timeout=180.0).run()
    assert not run.errors, f"shadow run reported errors: {run.errors}"

    lines = [m for m in run.messages if "nativedecode=shadow |" in m]
    assert lines, f"no nativedecode shadow readout; messages={run.messages}"

    def _field(line: str, key: str) -> int:
        for tok in line.replace("|", " ").split():
            if tok.startswith(f"{key}="):
                return int(tok.split("=", 1)[1])
        raise AssertionError(f"field {key!r} missing from readout: {line!r}")

    final = lines[-1]
    native_obs = _field(final, "obs")
    enriched = _field(final, "enriched")
    sib1_records = _field(final, "sib1")

    obs = _canon(run.observations)
    with_identity = [o for o in obs if "mcc" in o]
    own_sib1 = [o for o in with_identity
                if (o.get("prov") or {}).get("origin") == "0xB0C0"]
    cross_record = len(with_identity) - len(own_sib1)

    # Guard the guard: on a fixture with no cross-record identity at all, every
    # assertion below is 0 == 0 and certifies nothing. The mixed fixture has 5.
    assert cross_record > 0, (
        f"the bridge produced NO cross-record identity on this fixture "
        f"({len(with_identity)} identity rows, all own-SIB1) -- this test would "
        f"pass against a shim that never enriches. Use a SIB1-bearing capture.")
    assert sib1_records > 0, (
        f"the tap saw no identity-source records (0xB0C0/0xB821) at all: {final!r}")

    assert native_obs == len(obs), (
        f"native decoded {native_obs} observations, the bridge relayed {len(obs)} "
        f"-- the two paths saw different streams, so the enrichment compare below "
        f"would be meaningless: {final!r}")
    assert enriched == cross_record, (
        f"native enriched {enriched} observations, the bridge enriched "
        f"{cross_record} ({len(with_identity)} identity rows minus "
        f"{len(own_sib1)} own-SIB1 0xB0C0 rows). Native cross-record SIB1 "
        f"enrichment has diverged from _enrich(obs, sib1_map) -- and note the "
        f"observation COUNT is unaffected either way, so no other assertion in "
        f"this file would have caught it: {final!r}")


def test_nativedecode_shadow_arming_message_lists_the_legs(tmp_path):
    """Arming announces WHICH log codes have native legs.

    An operator reading "shadow armed" needs to know the migration's current
    surface without consulting the source; and a leg that silently drops out of
    the build (compiled, passing, in no shipping binary)
    shows up here as a shorter list rather than as an unexplained coverage drop.
    """
    binary = _native_binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay, nativedecode="shadow")).run()
    armed = [m for m in run.messages if "shadow armed" in m]
    assert armed, f"no arming message; messages={run.messages}"
    # 0x1476 is listed as its own GNSS group rather than appended to the cell
    # list: the two sets route to different entry points, so an operator
    # reading one flat list of five would reasonably expect diag_native_has_leg()
    # to answer true for all five, and it does not.
    for code in ("0xB192", "0xB195", "0xB0C0", "0xB97F", "0x1476"):
        assert code in armed[0], f"{code} missing from the arming list: {armed[0]!r}"
    assert "cell leg(s)" in armed[0] and "GNSS leg(s)" in armed[0], (
        f"the arming list no longer separates the cell and GNSS surfaces: {armed[0]!r}")


def test_nativedecode_rejects_an_unknown_mode(tmp_path):
    """An unknown nativedecode= value is a hard open error, not a silent default.

    Same contract as ``mask=``/``f3=``/``enabled=``. Silently defaulting a typo
    to ``off`` would mean an operator who asked for a measurement gets a clean
    run with no measurement in it and no indication why.

    Probe with a value that is not a mode in any future. A value that later
    becomes a real mode would leave this test vacuously green: the assertion
    is an ``or`` chain whose last clause matches any message mentioning
    ``nativedecode``.
    """
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay, nativedecode="perhaps"),
                      timeout=6.0).run()
    assert run.open_code == 0 or run.errors or any(
        "nativedecode" in m for m in run.messages), (
        "an unknown nativedecode mode opened successfully and said nothing")


# ── nativedecode=on: the runtime off-vs-on A/B ────────────────────────────────
def test_nativedecode_on_emits_the_same_observations_as_off(tmp_path):
    """THE GATE. ``nativedecode=on`` emits what the Python bridge emits.

    This is the offline half of the off-vs-on A/B, and it needs no hardware:
    a replay drives the identical byte stream through both configurations of the same binary, and
    the only difference is **which layer renders the cell_observation line** --
    ``result_to_observations`` in the forked helper (``off``) or
    ``diag_native_obs_to_json`` in-process (``on``).

    The comparison is against the SAME fixture's ``off`` run rather than a
    stored expectation, for the reason its shadow-mode sibling gives: a stored
    expectation can be updated to match a regression.

    Both halves have to be right for this to pass, and they fail in opposite
    directions:

    * if the native emit path is missing or declines a code, ``on`` is **short**;
    * if the bridge's ``cell_observation`` is not suppressed, ``on`` is
      **doubled** -- every observation emitted twice, once by each layer. That
      second failure is the one worth having a test for, because a doubled
      observation stream still looks busy and healthy on a live source.
    """
    binary = _native_binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    off = CellDiagRun(binary, _definition(replay, nativedecode="off")).run()
    on = CellDiagRun(binary, _definition(replay, nativedecode="on")).run()

    assert not off.errors, f"off run reported errors: {off.errors}"
    assert not on.errors, f"on run reported errors: {on.errors}"
    assert off.observations, "off run produced 0 observations - bad fixture"

    c_off, c_on = _canon(off.observations), _canon(on.observations)
    assert c_on == c_off, (
        f"nativedecode=on diverged from the bridge: on emitted {len(c_on)} "
        f"observations, off emitted {len(c_off)}"
        + (" -- exactly DOUBLE, so the bridge's cell_observation line is not "
           "being suppressed" if len(c_on) == 2 * len(c_off) else ""))


def test_nativedecode_on_matches_off_on_cell_config_and_beams(tmp_path):
    """The gate above, on the one fixture that carries 0xB197 and beams.

    It also asserts the fixture still EXERCISES both: an off run with no
    bandwidth_rb or no beams row would make the equality vacuous and let a
    native lag hide behind a green suite."""
    binary = _native_binary_or_skip()
    replay = _cellcfg_beams_fixture_or_skip(tmp_path)

    off = CellDiagRun(binary, _definition(replay, nativedecode="off")).run()
    on = CellDiagRun(binary, _definition(replay, nativedecode="on")).run()
    assert not off.errors, f"off run reported errors: {off.errors}"
    assert not on.errors, f"on run reported errors: {on.errors}"

    c_off, c_on = _canon(off.observations), _canon(on.observations)
    assert any("bandwidth_rb" in o for o in c_off), "fixture no longer carries 0xB197 config"
    assert any("beams" in o for o in c_off), "fixture no longer carries 0xB97F beams"
    differing = sum(1 for a, b in zip(c_off, c_on) if a != b)
    assert len(c_on) == len(c_off) and differing == 0, (
        f"nativedecode=on diverged from the bridge on the cell-config/beams "
        f"fixture: on {len(c_on)} vs off {len(c_off)} rows, {differing} differ")


def test_nativedecode_on_actually_emits_from_the_native_path(tmp_path):
    """``on`` emits from the C legs -- not by quietly falling back to the bridge.

    The equality test above passes just as well against an ``on`` mode that
    accepted the flag and changed nothing, which is both the cheapest wrong
    implementation and indistinguishable from the right one by output alone.
    So read the tap's own readout: it must name the mode it ran in, and report
    a non-zero count of observations **it** emitted.
    """
    binary = _native_binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay, nativedecode="on")).run()
    lines = [m for m in run.messages if "nativedecode=on |" in m]
    assert lines, (
        f"no nativedecode=on readout on the message bus; the mode must name "
        f"itself (a readout hard-coded to 'shadow' would report the wrong "
        f"configuration). messages={run.messages}")

    readout = lines[-1]
    emitted = _kv_int(readout, "emitted")
    assert emitted > 0, (
        f"nativedecode=on emitted 0 observations of its own, so every "
        f"observation seen came from somewhere else: {readout!r}")


def test_nativedecode_on_reports_the_bridge_divergence_it_measured(tmp_path):
    """``on`` keeps the bridge running and COUNTS what it suppressed.

    The switch-over's real risk is not a wrong field, which the offline
    byte-parity test pins -- it is a log code the native legs do not claim, on
    firmware nobody has replayed. In ``on`` mode the bridge is still fed and
    still decodes, so the two renderings of the same stream are both available
    at once and the source can compare them itself. That makes the A/B a
    property of every live run rather than of a fixture somebody remembered to
    capture.

    Asserted here on a replay: suppressed-bridge-observations must equal
    natively-emitted ones, and the readout must carry both numbers so the
    equality is checkable by an operator, not only by this test.
    """
    binary = _native_binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay, nativedecode="on")).run()
    lines = [m for m in run.messages if "nativedecode=on |" in m]
    assert lines, f"no nativedecode=on readout; messages={run.messages}"

    readout = lines[-1]
    emitted = _kv_int(readout, "emitted")
    suppressed = _kv_int(readout, "bridge-suppressed")
    assert suppressed > 0, (
        f"the bridge produced no observations to suppress, so nothing was "
        f"compared: {readout!r}")
    assert emitted == suppressed, (
        f"the native emit path and the still-running bridge disagree on the "
        f"same bytes: native emitted {emitted}, bridge produced {suppressed} "
        f"-- {readout!r}")


def test_nativedecode_on_geotags_identically_to_off(tmp_path):
    """The A/B above compares JSON. The position is NOT in the JSON.

    A ``cell_observation``'s geo-tag rides in the DATAREPORT's **GPS sub-block**,
    beside the JSON, not inside it -- so ``_canon`` equality is fully consistent
    with ``on`` emitting every observation ungeotagged (for example, a native
    tap that counts GNSS fixes without feeding ``last_gps``).

    Two properties:

    1. **presence** -- ``on`` geo-tags exactly the observations ``off`` does.
    2. **value** -- the positions are *equal*. There are two writers into the
       one ``last_gps`` cache (the bridge's ``gps_fix`` JSON line and the native
       GNSS leg). The helper rounds lat/lon to 6 dp to stay byte-for-byte with
       ``dlf_to_wigle``, so the native leg must round the same way; a raw
       double differs only until the bridge's line overwrites the cache, which
       makes the error tiny, intermittent, and easy to miss.
    """
    binary = _native_binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    off = CellDiagRun(binary, _definition(replay, nativedecode="off")).run()
    on = CellDiagRun(binary, _definition(replay, nativedecode="on")).run()

    assert off.observations, "off run produced 0 observations - bad fixture"
    assert any(g is not None for g in off.observation_gps), (
        "no observation was geo-tagged in either mode, so this compares "
        "nothing -- the fixture must carry GNSS records inside the staleness "
        "window")

    def _pos(g):
        if g is None:
            return None
        return (g.get(SUB_GPS_FIELD_LAT), g.get(SUB_GPS_FIELD_LON),
                g.get(SUB_GPS_FIELD_FIX))

    p_off = [_pos(g) for g in off.observation_gps]
    p_on = [_pos(g) for g in on.observation_gps]

    assert [q is None for q in p_on] == [q is None for q in p_off], (
        f"nativedecode=on geo-tagged a different SET of observations than off: "
        f"on={p_on} off={p_off}")
    assert p_on == p_off, (
        f"nativedecode=on geo-tagged the same observations with DIFFERENT "
        f"positions -- the two last_gps writers disagree: on={p_on} off={p_off}")


def test_nativedecode_on_is_refused_by_a_baseline_build(tmp_path):
    """A build with no legs REFUSES ``on``, for a stronger reason than ``shadow``.

    ``shadow`` over the stub reports a wrong measurement. ``on`` over
    the stub would **suppress the bridge and emit nothing in its place**: a
    source that opens cleanly, runs, and produces zero observations,
    indistinguishable from "no cells in range".

    Skips unless a baseline binary is actually available to test, since the
    configured tree may hold either configuration.
    """
    binary = _baseline_binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay, nativedecode="on"),
                      timeout=6.0).run()
    assert run.open_code == 0 or run.errors or any(
        "nativedecode" in m for m in run.messages), (
        "a baseline build accepted nativedecode=on, so it suppressed the only "
        "decoder it has and emitted nothing in its place, silently")


# ── the census's severity escalation ──────────────────────────────────────────
MSGFLAG_INFO = 2
MSGFLAG_ERROR = 4


def test_census_line_escalates_to_ERROR_when_it_has_something_to_flag(tmp_path):
    """The diag_inventory line is relayed at ERROR severity when a key is
    unrecognized or SILENT, and it names both.

    `silent` is the field that answers the operator's actual question — a code the
    emit contract DEPENDS on that parsed fine and produced nothing. On a live source
    that is indistinguishable from "no cells in range", which is the failure this
    census exists to make visible. Relaying it at ERROR on the message bus makes
    it visible on every surface, not only a dedicated panel.

    On the reference capture the escalation is not hypothetical: 3 unrecognized keys
    and 2 silent ones (0x14D8/v1 and 0xB0C0/v27), both real.

    This capture carries BOTH kinds of finding, so the ERROR flag *here* cannot
    attribute the escalation to `silent`: a control deleting the `silent` term still
    passes on this fixture, because `unrecognized` escalates anyway. What is
    silent-specific and asserted below is the STATUS STRING — `silent[...]`
    appears only when the census found silent keys.

    The attribution gap itself is closed by
    ``test_census_escalates_on_a_SILENT_key_alone`` below, which uses a fixture with
    silent keys and NO unrecognized ones. This test is kept as the both-findings
    case: the two shapes exercise different terms, and a census that reported only
    one of them would still pass one of these tests.
    """
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay)).run()
    assert run.open_code in (1, None), (
        f"OPENREPORT failed (code={run.open_code}); errors={run.errors}; "
        f"open message={run.open_message!r}")

    idx = [i for i, m in enumerate(run.messages)
           if "inventory:" in m and "(code,ver)" in m]
    assert idx, f"no diag_inventory status message relayed; messages={run.messages}"

    i = idx[-1]
    line, flag = run.messages[i], run.message_flags[i]
    assert "unrecognized" in line and "silent" in line, (
        f"the census one-liner must name BOTH findings — they are different "
        f"(new firmware vs a depended-on code going quiet): {line!r}")
    assert "silent[" in line, (
        f"the census flagged silent keys but did not name which: {line!r}")
    assert flag == MSGFLAG_ERROR, (
        f"the census line has findings to flag but was relayed at severity {flag} "
        f"(expected {MSGFLAG_ERROR}=ERROR). An INFO line scrolls past among routine "
        f"status output, which is the same as not reporting it: {line!r}")


def test_census_escalates_on_a_SILENT_key_alone(tmp_path):
    """A census with silent keys and ZERO unrecognized ones still relays at ERROR.

    The reference capture carries both findings, so its ERROR flag proves only
    that *something* escalated — delete the `silent` term from the C relay and
    the other tests stay green, because `unrecognized` covers for it.

    The fixture (see ``_silent_only_fixture_or_skip``) is a passive DIAG capture
    from a SIM-less EG25-G::

        celldiag inventory: 70 (code,ver) key(s), 0 unrecognized, 1 silent
                            silent[0xB0C0/v20]

    With `unrecognized` empty, the ERROR flag can only come from the `silent` term —
    so this test fails if that term is removed. It is also the scenario that matters operationally: `0xB0C0` is a
    cell-contract code that decoded on every record and emitted nothing, and on a
    live source that is indistinguishable from "no cells in range".

    **The zero-unrecognized premise is a property of the (fixture, decoder)
    pair, not of the fixture.** A decoder with fewer parsers (the public
    diaggrok, for example) reads many of the same 70 keys as unrecognized.
    Nothing regressed in that case; the decoders simply differ in coverage.

    The attribution is not unique to this test, which is what makes relaxing
    the premise safe. ``test_celldiag_census_bound.test_silent_total_escalates_the_same_way``
    feeds a *synthesised* census — ``0 unrecognized, 5 silent`` — through a stub
    helper, so it pins the C relay's ``silent`` term with no decoder involved at
    all. With ``has_silent`` forced to 0, **both** tests fail, and the stub one
    fails on every decoder.

    So the strong claim is asserted when the resolved decoder can supply it, and
    the honest weaker one otherwise — a branch, never a skip. What the weaker
    branch still proves is the thing the stub test
    cannot: that a **real capture through a real decoder** produces a silent key
    and escalates on it.
    """
    binary = _binary_or_skip()
    replay = _silent_only_fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay)).run()
    assert run.open_code in (1, None), (
        f"OPENREPORT failed (code={run.open_code}); errors={run.errors}; "
        f"open message={run.open_message!r}")

    idx = [i for i, m in enumerate(run.messages)
           if "inventory:" in m and "(code,ver)" in m]
    assert idx, f"no diag_inventory status message relayed; messages={run.messages}"

    i = idx[-1]
    line, flag = run.messages[i], run.message_flags[i]

    # The premise, measured rather than assumed. See the docstring: a decoder
    # with partial coverage of this capture is a legitimate configuration, not a
    # regression, so its census is read for what it can support.
    attributable = "0 unrecognized" in line
    if not attributable:
        # Name the actual cause. Blaming "a parser change" here would send the
        # reader to look for a regression that does not exist, and a wrong
        # diagnosis costs more than the missing coverage because it is followed.
        assert "unrecognized[" in line, (
            f"census is neither 0-unrecognized nor lists any unrecognized key, "
            f"so it cannot be read at all: {line!r}")
    assert "silent[" in line, (
        f"the fixture must produce at least one silent key, or there is nothing "
        f"for the silent term to escalate on: {line!r}")
    # The assertion is the same either way; the CLAIM is not, and the message
    # says which one is being made. With unrecognized keys present, `silent`
    # demonstrably cannot be shown to have caused the escalation — asserting it
    # anyway is precisely what would make this test quietly wrong, and the
    # attribution is carried by the stub-helper sibling regardless.
    claim = ("a census whose ONLY finding is a silent key" if attributable else
             "a census carrying a silent key (attribution is not available on "
             "this decoder's coverage; see test_silent_total_escalates_the_same_way)")
    assert flag == MSGFLAG_ERROR, (
        f"{claim} was relayed at severity {flag} (expected "
        f"{MSGFLAG_ERROR}=ERROR). A depended-on code going quiet is the "
        f"census's most actionable finding and it scrolled past as routine "
        f"status: {line!r}")


def test_routine_status_messages_stay_INFO(tmp_path):
    """The escalation must be selective, or it is not an escalation.

    If everything were ERROR the operator learns to ignore ERROR, and the census
    line — the one that exists to be noticed — goes with it. This pins that ordinary
    source status stays INFO in the same run that produces an ERROR census line.
    """
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay)).run()
    routine = [f for m, f in zip(run.messages, run.message_flags)
               if "inventory:" not in m]
    assert routine, f"no non-census messages to check; messages={run.messages}"
    assert all(f == MSGFLAG_INFO for f in routine), (
        f"non-census messages relayed at non-INFO severities "
        f"{sorted(set(routine))}: an always-on ERROR trains the operator to ignore "
        f"the census line this escalation exists to highlight")


# ── diag_stats: every registered stats field carries a real value ─────────────
def _diag_stats_lines(run: "CellDiagRun") -> list[dict]:
    """Every `diag_stats` control object the run emitted, parsed.

    They arrive on the SAME relay as cell_observations (cf_send_json), tagged by
    their "type" field — which is what the datasource keys on to intercept them
    before phy_cell can devicify one.

    FILTERED on that type: the control channel also carries `diag_census`
    lines, which have no f3_preset and must not be read as stats. Like
    ``CellDiagRun._on_json``, this must not assume the control channel carries
    exactly one kind of thing.
    """
    out = []
    for js in run.control:
        obj = json.loads(js)
        if isinstance(obj, dict) and obj.get("type") == "diag_stats":
            out.append(obj)
    return out


def test_diag_stats_json_is_wellformed_and_not_an_observation(tmp_path):
    """The structured stats object parses, and never leaks into the cell stream.

    Parsing is the whole point of the assertion, not a formality.
    diag_stats_format_json REFUSES an object that would not fit rather than
    clamp it, because a clipped object does not parse, so the consumer's try/catch drops
    it and every field in that tick goes unupdated — a silent, total failure that
    a "did we emit something?" check cannot see.
    """
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay, stats_interval=1)).run()
    assert run.open_code in (1, None), (
        f"OPENREPORT failed (code={run.open_code}); errors={run.errors}; "
        f"open message={run.open_message!r}")

    stats = _diag_stats_lines(run)
    assert stats, (
        "no diag_stats control object relayed — the datasource has nothing to "
        f"SET its tracker fields from; observations={len(run.observations)}")

    # It is a control line, not a cell. phy_cell must never see it as a device.
    for s in stats:
        assert "pci" not in s and "earfcn" not in s, (
            f"a diag_stats object carries cell_observation fields: {s}")
    for js in run.observations:
        assert '"type":"diag_stats"' not in js.replace(" ", ""), (
            "a diag_stats control object reached the cell_observation stream")


def test_diag_stats_populates_the_dead_fields(tmp_path):
    """Every registered field carries a REAL value.

    A kismet.datasource.celldiag.* field that is registered but emitted by
    nobody sits at a permanent zero/"" that is indistinguishable from a healthy
    reading — "rawlog_bytes: 0" reads as "tee on, nothing written yet". A
    check on *registration* stays true while the values are dead.

    This asserts the values are NON-ZERO where the run makes them non-zero. A
    presence-only check would pass against the exact bug it is here to prevent.
    """
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)
    raw_out = tmp_path / "stats-tee.raw"

    run = CellDiagRun(
        binary, _definition(replay, stats_interval=1, rawlog=raw_out)
    ).run()
    assert run.open_code in (1, None), (
        f"OPENREPORT failed (code={run.open_code}); errors={run.errors}; "
        f"open message={run.open_message!r}")

    stats = _diag_stats_lines(run)
    assert stats, "no diag_stats object relayed"
    last = stats[-1]

    # Config-owned. f3=all is exercised separately — it is high-volume
    # enough to change this run's timing, and the point here is the values.
    assert last.get("rawlog_path", "").endswith("stats-tee.raw"), (
        f"rawlog_path missing or wrong: {last}")
    assert last.get("rawlog_bytes", 0) > 0, (
        f"rawlog_bytes is {last.get('rawlog_bytes')} while the tee file holds "
        f"{raw_out.stat().st_size if raw_out.exists() else 'no'} bytes — this is "
        f"a dead field reading as a healthy zero: {last}")

    # Helper census. distinct > 0 is guaranteed by any decoding fixture;
    # unrecognized/silent are counts that may legitimately be 0, so only their
    # PRESENCE is asserted — a zero there is a real reading once the census has
    # reported, which is what the inventory_valid gate guarantees.
    assert last.get("inventory_distinct_codes", 0) > 0, (
        f"inventory_distinct_codes is 0 on a fixture that decodes records: {last}")
    assert "inventory_unrecognized" in last, f"inventory_unrecognized absent: {last}"
    assert "inventory_silent" in last, (
        f"inventory_silent absent — the census escalates on `silent` for a "
        f"STRONGER reason than `unrecognized`: {last}")


def test_diag_stats_omits_unreported_sources(tmp_path):
    """A source that has not reported OMITS its keys rather than sending zeros.

    "0 bytes written" and "no tee configured" are opposite readings, and once
    both are the number 0 on the wire no panel can tell them apart. Running with
    NO rawlog= must therefore leave both rawlog keys absent — the datasource's
    "missing key leaves the prior value intact" then keeps the field at its
    default instead of stamping a healthy-looking measurement.
    """
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay, stats_interval=1)).run()
    stats = _diag_stats_lines(run)
    assert stats, "no diag_stats object relayed"

    for s in stats:
        assert "rawlog_path" not in s, (
            f"no rawlog= configured, yet rawlog_path was emitted: {s}")
        assert "rawlog_bytes" not in s, (
            f"no rawlog= configured, yet rawlog_bytes was emitted as "
            f"{s['rawlog_bytes']} — that renders as 'tee on, nothing written': {s}")
    # f3 is always known (on or off), so it is always emitted.
    assert stats[-1].get("f3_preset") == "off", (
        f"f3_preset should read 'off' when unset: {stats[-1]}")


_NATIVE_STATS_KEYS = (
    "native_total_records", "native_records", "native_obs",
    "native_declined", "native_enriched", "native_gps_fixes",
    "native_fallback_records",
)


def test_diag_stats_carries_the_native_tap_when_armed(tmp_path):
    """The native tap's counters must reach the SERVER, not just a log line.

    The shadow tap's numbers are what the "can the Python bridge go?" decision
    rests on. Rendered only into a human status STRING, they leave the server
    with no machine-readable copy, so no panel or consumer can read the one
    measurement the switch-over turns on.

    The values are asserted NON-ZERO, not merely present. A presence-only check
    passes against the precise bug this prevents: a key emitted with a dead
    zero.
    """
    binary = _native_binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(
        binary, _definition(replay, stats_interval=1, nativedecode="shadow")
    ).run()
    assert run.open_code in (1, None), (
        f"OPENREPORT failed (code={run.open_code}); errors={run.errors}; "
        f"open message={run.open_message!r}")

    stats = _diag_stats_lines(run)
    assert stats, "no diag_stats object relayed"
    last = stats[-1]

    missing = [k for k in _NATIVE_STATS_KEYS if k not in last]
    assert not missing, (
        f"nativedecode=shadow is armed but the stats object omits {missing}. "
        f"The tap's counters reach the message bus only, so the server has no "
        f"machine-readable copy. Got: {sorted(last)}")

    # The denominator is the point: coverage is a RATIO, and a consumer handed
    # native_records alone can render a numerator as a percentage.
    assert last["native_total_records"] > 0, (
        f"native_total_records is 0 on a fixture that decodes — the coverage "
        f"denominator is dead: {last}")
    assert last["native_records"] > 0, f"no record was claimed natively: {last}"
    assert last["native_records"] <= last["native_total_records"], (
        f"more records claimed natively than were extracted at all: {last}")
    assert last["native_obs"] > 0, f"native legs produced no observations: {last}"
    assert last["native_fallback_records"] > 0, (
        "this fixture carries codes outside the cell contract, so the bridge is "
        f"still the only decoder for some of it — a 0 here would mean the "
        f"Python bridge could already go, which is not true: {last}")


def test_diag_stats_omits_the_native_tap_when_it_is_not_armed(tmp_path):
    """An UNARMED tap omits its keys — it must not publish a measured-looking 0.

    This is the control that decides whether the group above means anything.
    "native decoded nothing" and "native was never asked" are opposite readings
    of the native-coverage question, and once both are the number 0 on the wire
    no panel can tell them apart — it would report that the native path covers
    0% of the stream, confidently, having measured nothing.

    Same contract as rawlog_* and inventory_*: a missing key
    leaves the datasource's tracker field at its default.
    """
    binary = _binary_or_skip()
    replay = _fixture_or_skip(tmp_path)

    run = CellDiagRun(binary, _definition(replay, stats_interval=1)).run()
    stats = _diag_stats_lines(run)
    assert stats, "no diag_stats object relayed"

    for s in stats:
        present = [k for k in _NATIVE_STATS_KEYS if k in s]
        assert not present, (
            f"nativedecode is unset, yet the native tap emitted {present} — "
            f"a 0 here reads as 'native covers nothing', which was never "
            f"measured: {s}")


# ── the two-build replay equality gate ────────────────────────────────────────
# Both configurations must decode a capture into the SAME observations. Not a
# native-vs-Python comparison: BOTH sides run nativedecode=off, so both decode
# via the Python bridge. What it proves is that *linking* the C++ closure does
# not perturb the bridge's output -- so neither build mode can rot unnoticed
# while the other is the one everybody happens to build.
#
# Why two env vars and a make target rather than building here: a double relink
# is minutes of compiling and it would mutate the very tree under measurement.
# The build belongs in make; the comparison belongs here. Unset, these skip and
# name the target. `replay-gate` restores the build mode the tree entered in
# (including the unbuilt case), so running it leaves the tree as it was.
_GATE_ENV = {
    "baseline": "CELLDIAG_BASELINE_BIN",
    "native": "CELLDIAG_NATIVE_BIN",
}
_GATE_CMD = "make -C celltools replay-gate"

#: The symbol that distinguishes the two builds -- the same one the shadow tests
#: key on. A NATIVE=1 binary defines the C++ observation legs; a baseline
#: binary defines none of them (its stub supplies only the extern "C" surface).
_NATIVE_MARKER = "_observation::observations"


def _defines_native_legs(binary: str):
    """True/False, or None when nm cannot answer."""
    try:
        out = subprocess.run(
            ["nm", "-C", "-g", "--defined-only", binary],
            capture_output=True, text=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return _NATIVE_MARKER in out


def _gate_binary_or_skip(mode: str) -> str:
    env = _GATE_ENV[mode]
    raw = os.environ.get(env)
    if not raw:
        pytest.skip(f"{env} unset — build both configurations with `{_GATE_CMD}`")
    path = Path(raw)
    if not path.exists():
        # A named-but-absent path is a wrong-cause skip; say which it is.
        pytest.skip(f"{env}={raw} does not exist — rebuild with `{_GATE_CMD}`")
    if not ORACLE.ok:
        pytest.skip(ORACLE.skip_reason())
    # Staleness is ROT, not configuration: fail, exactly as the sibling does.
    _fail_if_binary_is_stale(path)
    return str(path)


def test_baseline_and_native_builds_decode_a_capture_identically(tmp_path):
    """The replay equality gate: baseline output == NATIVE=1 output.

    **The control comes first, and without it this test proves nothing.**
    Two binaries that are secretly the same build will always agree, and this
    would report a green having compared a thing to itself -- the same shape as
    a shadow tap reporting ``bridge-only=100%`` on a binary that has no legs at
    all. So the modes are verified to actually differ *before* the outputs are
    compared, and if ``nm`` cannot answer, this skips rather than guessing.
    """
    baseline = _gate_binary_or_skip("baseline")
    native = _gate_binary_or_skip("native")

    b_native = _defines_native_legs(baseline)
    n_native = _defines_native_legs(native)
    if b_native is None or n_native is None:
        pytest.skip("nm unavailable — cannot prove the two builds differ")
    assert b_native is False, (
        f"{_GATE_ENV['baseline']} points at a binary that DEFINES native decode "
        f"legs, so it is not the Python-decode baseline. Comparing it to the "
        f"native build would compare two identical configurations and pass "
        f"vacuously. Rebuild with `{_GATE_CMD}`."
    )
    assert n_native is True, (
        f"{_GATE_ENV['native']} points at a binary with NO native decode legs, "
        f"so it is not a NATIVE=1 build (see the sibling assertion for why a "
        f"vacuous pass is the danger). Rebuild with `{_GATE_CMD}`."
    )

    replay = _fixture_or_skip(tmp_path)

    runs = {}
    for label, binary in (("baseline", baseline), ("native", native)):
        run = CellDiagRun(binary, _definition(replay)).run()
        assert run.open_code in (1, None), (
            f"{label}: OPENREPORT failed (code={run.open_code}); "
            f"errors={run.errors}; "
        f"open message={run.open_message!r}")
        assert not run.errors, f"{label}: binary reported errors: {run.errors}"
        runs[label] = _canon(run.observations)

    # An empty-vs-empty comparison is the other vacuous pass: a fixture that
    # decodes to nothing agrees with itself perfectly.
    assert runs["baseline"], (
        "the baseline build produced 0 observations — nothing was compared")

    assert runs["baseline"] == runs["native"], (
        f"the two build configurations disagree: baseline produced "
        f"{len(runs['baseline'])} observations, NATIVE=1 produced "
        f"{len(runs['native'])}. Both run nativedecode=off and decode through "
        f"the same Python bridge, so linking the C++ closure has changed "
        f"behaviour it must not touch.")



# ── paced replay, for runtime-command tests ───────────────────────────
class _PacedReplay:
    """Feed a replay through a FIFO so the HARNESS decides when bytes flow.

    Every runtime-setter assertion here has the form "the counter moved AFTER
    the knob was applied", and against a plain file that is unprovable: the
    capture loop reads 64 KiB per poll pass with no pacing, so a 200 KiB
    fixture is fully consumed in a handful of iterations, long before the
    helper has relayed its first observation. The knob applies cleanly and
    there is nothing left to tee. A test against a plain file could only
    assert "the command returned OK", and an ack is not a value.

    A FIFO also passes the `replay=` container sniff for free: it is a `pread`
    at offset 0, which returns ESPIPE on a pipe and is read as "no container".

    The feeder writes `head_bytes`, then BLOCKS until `release_when()` holds —
    a gate, not a sleep, so a loaded host makes the test slower and never
    flakier.
    """

    def __init__(self, path: Path, data: bytes, head_bytes: int,
                 release_when, release_timeout: float = 20.0):
        self.path = path
        self.data = data
        self.head_bytes = min(head_bytes, len(data))
        self.release_when = release_when
        self.release_timeout = release_timeout
        self.error: BaseException | None = None
        os.mkfifo(path)
        self._thread = threading.Thread(target=self._feed, daemon=True)

    def start(self) -> "_PacedReplay":
        self._thread.start()
        return self

    def join(self, timeout: float = 5.0) -> None:
        self._thread.join(timeout)

    def _feed(self) -> None:
        try:
            # Blocks until the capture binary opens the read end.
            with open(self.path, "wb") as f:
                f.write(self.data[:self.head_bytes])
                f.flush()
                deadline = time.monotonic() + self.release_timeout
                while time.monotonic() < deadline and not self.release_when():
                    time.sleep(0.02)
                f.write(self.data[self.head_bytes:])
                f.flush()
        except BrokenPipeError:
            pass                       # the binary finished early; not our bug
        except BaseException as exc:   # pragma: no cover - surfaced by the test
            self.error = exc


def _paced_definition(tmp_path: Path, **flags) -> "tuple[Path, bytes]":
    """The replay fixture's bytes plus a FIFO path to serve them through."""
    src = _fixture_or_skip(tmp_path)
    return tmp_path / "paced.fifo", src.read_bytes()


# ── runtime option setter ─────────────────────────────────────────────────────
# celldiag's opt-in to Kismet's ONE generic runtime setter. The server reaches
# it through `POST /datasource/by-uuid/:uuid/set_channel`; here it arrives as a
# CONFIGURE frame, which is the same code path with the HTTP layer removed.
#
# THE ASSERTION THAT MATTERS IS "rawlog_bytes MOVED", not "the command
# returned success". A registration/ack check stays green when the wiring is
# correct and the values are never written; a runtime tee that answers OK and
# writes nothing is exactly that.

def _paced_run(tmp_path, knob, *, rawlog=None, head_bytes=4096, timeout=40.0):
    """One replay run whose bytes are gated on `knob` having been answered.

    Returns (run, fifo_path). The knob is sent as soon as the source is open and
    the first bytes are in flight; the REST of the capture is released only once
    a CONFIGREPORT has come back, so anything the setter changed is guaranteed to
    have a stream left to act on.
    """
    binary = _binary_or_skip()
    fifo, data = _paced_definition(tmp_path)
    flags = {"stats_interval": 1}
    if rawlog is not None:
        flags["rawlog"] = rawlog

    run = CellDiagRun(
        binary, _definition(fifo, **flags),
        timeout=timeout,
        # Fired on the first pump pass: the OPENREQ is already written, so the
        # framework's command thread handles this as soon as the source opens.
        configure=[(lambda r: True, knob)],
        # The replay's EOF is what ends the run; without a stop condition the
        # harness would burn its whole timeout after the last byte.
        stop_when=lambda r: False,
    )
    feeder = _PacedReplay(fifo, data, head_bytes,
                          release_when=lambda: bool(run.config_results)).start()
    run.run()
    feeder.join()
    assert feeder.error is None, f"replay feeder failed: {feeder.error!r}"
    return run, fifo


def test_runtime_rawlog_toggle_moves_the_byte_counter(tmp_path):
    """`rawlog=` applied at RUNTIME, asserted on bytes on disk.

    The source opens with NO rawlog= at all, so the tee cannot have been running:
    a file that exists and is non-empty here exists because of the runtime
    command. Asserted on the BYTES, not on the command returning success.
    """
    late = tmp_path / "runtime-tee.hdlc"
    run, _ = _paced_run(tmp_path, f"rawlog={late}")

    assert run.config_results, (
        "no CONFIGREPORT came back - the binary either did not register both "
        "chan callbacks or never saw the frame")
    _ok, msg = run.config_results[0]
    assert "now writing to" in msg, f"runtime rawlog= was refused: {msg!r}"
    # Applied, and NOT recorded as the source's channel -- the server would
    # display it as one and replay it after a re-open.
    assert run.config_channels == [None], (
        f"a runtime setting came back as the channel: {run.config_channels}")

    assert late.exists(), f"runtime rawlog= reported success and created no file: {msg!r}"
    assert late.stat().st_size > 0, (
        "the runtime tee file is empty - the command was accepted and no byte "
        "reached the sink (an ack is not a value)")

    stats = _diag_stats_lines(run)
    assert stats, "no diag_stats object relayed"
    assert str(late) in [s.get("rawlog_path") for s in stats], (
        f"no stats line reports the runtime path: "
        f"{[s.get('rawlog_path') for s in stats]}")
    assert any(s.get("rawlog_bytes", 0) > 0 for s in stats), (
        f"rawlog_bytes never advanced past 0 while {late.stat().st_size} bytes "
        f"landed on disk: {[s.get('rawlog_bytes') for s in stats]}")

    # The sink is OPEN, and says so in its own field. The path cannot
    # carry this -- see the sibling stop test, where the path survives a close.
    assert stats[-1].get("rawlog_active") is True, (
        f"the tee is writing and the source does not report it as active: "
        f"{stats[-1]}")


def test_runtime_rawlog_off_stops_the_tee_and_keeps_the_path(tmp_path):
    """`rawlog=off` closes the sink - and does NOT forget where it wrote.

    Clearing rawlog_path would make a stopped tee indistinguishable from one
    never configured, so the operator loses the answer to "where did the bytes I
    already captured go?".

    That retention means the path cannot answer "is it writing?", so a
    consumer that reads it for liveness is permanently wrong after the first
    stop (a web-UI toggle would offer only "Stop tee" for the rest of the
    source's life). `rawlog_active` is the separate field that makes the two
    questions separately answerable, and the assertion below is the pair --
    path retained AND state false, in the same object.
    """
    tee = tmp_path / "stop-me.hdlc"
    run, _ = _paced_run(tmp_path, "rawlog=off", rawlog=tee)

    assert run.config_results, "no CONFIGREPORT came back"
    _ok, msg = run.config_results[0]
    assert "stopped" in msg, f"rawlog=off was refused or silent: {msg!r}"
    assert run.config_channels == [None], run.config_channels   # not a channel

    stats = _diag_stats_lines(run)
    assert stats, "no diag_stats object relayed"
    assert stats[-1].get("rawlog_path", "").endswith("stop-me.hdlc"), (
        f"rawlog_path was cleared by the stop - a stopped tee is now "
        f"indistinguishable from one that was never configured: {stats[-1]}")
    assert stats[-1].get("rawlog_active") is False, (
        f"the sink is closed and the source still reports it as active - a "
        f"consumer cannot tell a stopped tee from a running one: {stats[-1]}")

    # The half a success code cannot tell you: bytes stopped landing. The run
    # released the REST of the capture only after this command was answered, so
    # a tee that stayed open would show a file larger than the byte count the
    # source reported at the moment it closed.
    assert tee.exists()
    assert tee.stat().st_size == stats[-1].get("rawlog_bytes"), (
        f"the tee file is {tee.stat().st_size} bytes but the source reports "
        f"{stats[-1].get('rawlog_bytes')} - bytes reached the sink after it was "
        f"closed, or the counter advanced without them")


def test_a_declined_runtime_key_is_refused_with_its_remedy(tmp_path):
    """`mask=` / `f3=` are recognised and NOT settable - and say so.

    The three outcomes must stay distinguishable at the operator, because the
    next action differs: reopen the source, fix the value, fix the spelling.
    A refusal that reads like "unknown option" sends someone hunting for a typo
    in a correctly-spelled option.
    """
    run, _ = _paced_run(tmp_path, "f3=high")
    assert run.config_results, "no CONFIGREPORT came back"
    _ok, msg = run.config_results[0]
    assert "not settable while the source is running" in msg, (
        f"f3= was ACCEPTED at runtime and silently did nothing: {msg!r}")
    assert "reopen" in msg, (
        f"the refusal does not name the way forward (close/reopen the source): "
        f"{msg!r}")
    # The refusal must also be LOUD, not only in a response nobody reads: the
    # wire success bit says 1 for a refusal (see celldiag_chancontrol), so the
    # message bus is where an operator actually learns this did nothing.
    assert any(f == MSGFLAG_ERROR and "not settable" in m
               for m, f in zip(run.messages, run.message_flags)), (
        f"the refusal never reached the message bus as an ERROR: "
        f"{list(zip(run.messages, run.message_flags))[-5:]}")


def test_an_unknown_runtime_key_is_refused_differently(tmp_path):
    """The control for the test above: unknown must not read like declined."""
    run, _ = _paced_run(tmp_path, "nativedecode=on")
    assert run.config_results, "no CONFIGREPORT came back"
    _ok, msg = run.config_results[0]
    assert "no celldiag option named" in msg, (
        f"an unknown key was accepted, or refused with the wrong text: {msg!r}")
    # ...and it must still point at what IS settable, or the operator has
    # nothing to try next.
    assert "rawlog" in msg, f"the refusal names no settable option: {msg!r}"


def test_a_runtime_path_that_cannot_be_opened_keeps_the_existing_tee(tmp_path):
    """A typo must not cost you the capture you already had.

    Open-then-close, not close-then-open: the new sink is opened BEFORE the old
    one is dropped, so a bad path is a refusal rather than a silent stop.
    """
    good = tmp_path / "keep-me.hdlc"
    run, _ = _paced_run(tmp_path, "rawlog=/nonexistent-dir-3384/x.hdlc", rawlog=good)

    assert run.config_results, "no CONFIGREPORT came back"
    _ok, msg = run.config_results[0]
    assert "cannot open rawlog file" in msg, (
        f"an unopenable path was accepted: {msg!r}")
    assert "untouched" in msg, (
        f"the refusal does not tell the operator their existing tee survived: "
        f"{msg!r}")

    stats = _diag_stats_lines(run)
    assert stats[-1].get("rawlog_path", "").endswith("keep-me.hdlc"), (
        f"the failed redirect changed rawlog_path anyway: {stats[-1]}")
    assert stats[-1].get("rawlog_bytes", 0) > 0, (
        f"the original tee stopped writing after a REFUSED redirect: {stats[-1]}")


def test_a_refused_runtime_knob_does_not_kill_the_capture(tmp_path):
    """The safety property, and the reason the return code is what it is.

    `cf_callback_chancontrol`'s contract offers -1 (fatal), 0 (cannot tune) and
    1+ (success), and CONFIGRESP is sent as `cbret < 0 ? 0 : 1` — so the ONLY
    return that reports failure on the wire is also the one that sets
    `spindown`. Reporting a mistyped option honestly through the status code
    would end a running wardrive over a typo.

    This pins the trade celldiag makes: a refused knob is reported in the
    MESSAGE, and the capture keeps going. Written as observations arriving
    AFTER the refusal, because the run releases the rest of the replay only once
    the command has been answered — so any observation counted here decoded from
    bytes that flowed after the source was told something it did not accept.
    """
    run, _ = _paced_run(tmp_path, "f3=high")

    assert run.config_results, "no CONFIGREPORT came back"
    assert run.observations, (
        "the source produced no observations at all after a refused runtime "
        "knob — a bad option string tore down the capture, which is exactly "
        "what returning -1 from chancontrol_cb does")
    # And it did not report itself as broken.
    assert not run.errors, (
        f"a refused runtime knob raised a protocol-level ERROR frame: {run.errors}")


def test_a_refused_FIRST_runtime_knob_does_not_crash_the_binary(tmp_path):
    """A refused FIRST channel-set must not crash the upstream capture framework.

    `cf_send_configresp()` computes its buffer estimate with
    `est_len += strlen(caph->channel)`, and `caph->channel` is assigned in
    exactly ONE place, the CONFIGREQ handler, and only when `chancontrol_cb`
    returns > 0. Unguarded, a source whose **first** channel-set is REFUSED
    reaches that line with it still NULL and **segfaults while composing the
    message that explains the refusal**.

    A crash presents as total silence — no CONFIGREPORT, no message, no further
    observations — because the process is gone, so the server sees a source that
    vanished rather than a setting that was declined. Invisible to a wifi source,
    which succeeds on its first tune and has had a channel ever since; reachable
    by any source using set_channel as a generic option setter, for which "no" is
    the first thing it says to a typo.

    The distinguishing assertion is that the capture SURVIVES and keeps
    producing. `run.config_results` alone would not catch it: a crash and a
    correctly-dropped frame look identical from there.
    """
    run, _ = _paced_run(tmp_path, "definitely-not-an-option=1")

    assert run.config_results, (
        "no CONFIGREPORT after a refused FIRST knob — the binary died in "
        "cf_send_configresp before it could answer")
    assert run.observations, (
        "no observations after a refused FIRST knob: the capture binary is gone")
    _ok, msg = run.config_results[0]
    assert "no celldiag option named" in msg, f"unexpected reply: {msg!r}"


def test_an_unresolvable_decoder_names_this_binarys_real_directory(tmp_path):
    """``exe_dir()`` resolves this binary's real directory on every platform.

    ``/proc/self/exe`` does not exist on Darwin. If ``exe_dir()`` relied on it,
    the "tree this binary shipped in" lookup could never find anything beside
    the binary, every replay open on a Mac would be refused, and the
    OPENREPORT would name the searched directory as ``(unknown)``.

    That lookup is the ONLY way to a decoder, so its blindness would be total.
    A copy of the binary in a directory with no fetched decoder above
    it: the open must be refused, the reason must be in the OPENREPORT, and it
    must name the directory the binary really runs from.
    """
    import shutil
    binary = _binary_or_skip()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    copy = bindir / Path(binary).name
    shutil.copy2(binary, copy)
    from celldiag_stub_tree import forget_installed_datadir
    forget_installed_datadir(copy)   # else an installed decoder is found
    replay = tmp_path / "empty.hdlc"
    replay.write_bytes(b"")

    run = CellDiagRun(str(copy), f"celldiag-{IMEI}:replay={replay}").run()
    assert run.open_code == 0, (
        f"a binary with no decoder above it opened "
        f"(code={run.open_code}); messages={run.messages[:3]}")
    msg = run.open_message or ""
    assert "decoder not found" in msg, msg
    assert "(unknown)" not in msg, (
        f"exe_dir() cannot see this binary's own directory on this platform, so "
        f"the decoder beside it can never be found: {msg}")
    assert str(bindir.resolve()) in msg, msg
