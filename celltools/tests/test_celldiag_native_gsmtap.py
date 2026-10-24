"""The live GSMTAP tap (gsmtap=), native vs the decode bridge, datagram by datagram.

Who sends:
- ``nativedecode=off`` / ``shadow``: the decode bridge (``--gsmtap``,
  diagmunge's encoder and UDP sink).
- ``nativedecode=on`` and ``only``: this binary frames every eligible code
  itself (``diag_gsmtap_sink.c``): LTE RRC 0xB0C0 and the five LTE NAS codes,
  whose bridges decline what diaggrok refuses. Under ``on`` the
  bridge is then given no ``--gsmtap`` at all, so no frame goes out twice.

A UDP listener in the test plays Wireshark, and the modes are compared
datagram for datagram, in order within each GSMTAP type. No fixture carries a
NAS record, so the NAS half uses a synthetic replay built here (no captured
bytes): an EMM INFORMATION PDU (``07 61``) behind each plain code's log
wrapper, a security-protected PDU on 0xB0E1, and one truncated record -- an
Attach reject missing its mandatory EMM cause -- that diaggrok refuses, so
neither side may send it.

The bridge's tap needs ``diagmunge`` beside diaggrok. It is found next to the
tree's ``.deps/diaggrok`` (a diaggrok source checkout), or at ``$CELLDIAG_DIAGMUNGE_SRC``.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

_CAPTURES_ENV = "CELL_CAPTURES"
CAPTURES = Path(os.environ[_CAPTURES_ENV]) if os.environ.get(_CAPTURES_ENV) else None
_CELLDIAG = Path(__file__).resolve().parents[2] / "capture_cell_diag"

from celldiag_goldens import FIXTURE_BY_LABEL  # noqa: E402

FIXTURES = (
    "tmp_probe.dlf",
    "celldiag_silent_only.hdlc",
    "celldiag_mixed_lte_nr.hdlc",
    "celldiag_rm500q_cellcfg_beams.hdlc",
)
# The LM960A18 capture's corpus path is in the golden ledger
# (goldens/FIXTURES.tsv); with a corpus set, a missing entry fails loudly.
if CAPTURES is not None:
    FIXTURES += (FIXTURE_BY_LABEL["lm960"],)

GSMTAP_TYPE_LTE_RRC = 0x0D
GSMTAP_TYPE_LTE_NAS = 0x12

#: (code, payload) records of the synthetic NAS replay, and how many of them
#: diaggrok frames. EMM INFORMATION with no optional IEs, behind the
#: 01 09 05 00 log wrapper, is structurally complete on every plain code;
#: 0xB0E1 carries a security-protected PDU (sec_hdr 2 | PD 7, MAC, SQN, inner);
#: the 0xB0E2 Attach reject (07 44) lacks its mandatory EMM cause, so the TS
#: 24.301 structure check refuses it.
_SYNTH_NAS = [
    (0xB0E1, bytes.fromhex("01090500" "27" "00000000" "05" "5201c1")),
    (0xB0E2, bytes.fromhex("01090500" "0761")),
    (0xB0E3, bytes.fromhex("01090500" "0761")),
    (0xB0EC, bytes.fromhex("01090500" "0761")),
    (0xB0ED, bytes.fromhex("01090500" "0761")),
    (0xB0E2, bytes.fromhex("01090500" "0744")),     # truncated: refused
]
_SYNTH_NAS_FRAMED = 5


def _ab():
    import test_celldiag_replay_ab as ab  # same directory; conftest adds it
    return ab


def _fixture_or_skip(rel: str, tmp_path: Path) -> Path:
    if CAPTURES is None:
        pytest.skip(f"no capture directory: set {_CAPTURES_ENV}=<dir> "
                    "(replay fixtures live outside the source tree)")
    plain, zst = CAPTURES / rel, CAPTURES / f"{rel}.zst"
    if plain.exists():
        return plain
    if not zst.exists():
        pytest.skip(f"fixture {rel}[.zst] not under ${_CAPTURES_ENV}")
    if shutil.which("zstd") is None:
        pytest.skip("zstd is needed to materialize a .zst fixture from CELL_CAPTURES")
    out = tmp_path / Path(rel).name
    subprocess.run(["zstd", "-dqf", str(zst), "-o", str(out)], check=True)
    return out


def _bridge_env_or_skip() -> dict:
    """The environment that gives the bridge its GSMTAP tap: diagmunge on
    PYTHONPATH (the binary prepends the tree's diaggrok in front of it)."""
    env_src = os.environ.get("CELLDIAG_DIAGMUNGE_SRC")
    if env_src:
        src = Path(env_src)
    else:
        deps = _CELLDIAG / ".deps" / "diaggrok"
        src = Path(os.path.realpath(deps)).parent / "diagmunge" / "src"
    if not (src / "diagmunge" / "gsmtap.py").exists():
        pytest.skip(f"no diagmunge for the bridge's --gsmtap at {src} "
                    "(set CELLDIAG_DIAGMUNGE_SRC=<diagmunge>/src)")
    env = dict(os.environ)
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src}{os.pathsep}{prev}" if prev else str(src)
    return env


class _Listener:
    """A local UDP socket standing in for Wireshark on the GSMTAP port."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.datagrams: list[bytes] = []
        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop:
            try:
                self.datagrams.append(self.sock.recv(65535))
            except socket.timeout:
                continue

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        time.sleep(0.3)            # the last datagrams are still in flight
        self._stop = True
        self._t.join(timeout=2)
        self.sock.close()
        return False


def _run(mode: str, replay: Path, env: dict, tmp_path: Path | None = None):
    """One replay with a listener on the gsmtap= port. `only` runs the way it
    ships: a helperless-capable binary in an environment with no python3, so
    a datagram can only have come from this process."""
    ab = _ab()
    if mode == "only":
        binary = ab._helperless_binary_or_skip()
        env = ab._no_python_env(tmp_path)
    else:
        binary = ab._native_binary_or_skip()
    with _Listener() as lst:
        run = ab.CellDiagRun(binary, ab._definition(
            replay, nativedecode=mode, gsmtap=f"127.0.0.1:{lst.port}"),
            env=env, timeout=300.0).run()
    assert run.open_code in (1, None), (
        f"nativedecode={mode}: OPENREPORT failed: {run.open_message!r}")
    return run, lst.datagrams


def _of_type(datagrams, gsmtap_type):
    return [d for d in datagrams if len(d) >= 3 and d[2] == gsmtap_type]


def _by_type(datagrams):
    """The datagrams split by GSMTAP type, each list in arrival order. Under
    `on` two senders share the port, so order is only meaningful within the
    frames one sender sends -- one GSMTAP type each."""
    out: dict[int, list[bytes]] = {}
    for d in datagrams:
        out.setdefault(d[2] if len(d) >= 3 else -1, []).append(d)
    return out


def _native_report(run):
    r = [m for m in run.messages if " gsmtap 127.0.0.1:" in m and "sent=" in m]
    return r[-1] if r else None


@pytest.mark.parametrize("fixture", FIXTURES)
def test_native_rrc_datagrams_equal_the_bridges(fixture, tmp_path):
    env = _bridge_env_or_skip()
    replay = _fixture_or_skip(fixture, tmp_path)
    off_run, off = _run("off", replay, env)
    assert not any("gsmtap" in m and "NOT SENT" in m for m in off_run.messages), (
        "the bridge refused --gsmtap: there is no reference to compare with")
    on_run, on = _run("on", replay, env)
    off_rrc = _of_type(off, GSMTAP_TYPE_LTE_RRC)
    assert off_rrc, f"{fixture}: the bridge sent no LTE RRC datagram"
    assert _by_type(on) == _by_type(off), (
        f"{fixture}: `on` delivered {len(on)} datagram(s), `off` {len(off)}")
    # And the RRC frames under `on` are the native ones, not the bridge's:
    # the native sink counted every one of them.
    report = _native_report(on_run)
    assert report and f"sent={len(off_rrc)} " in report and "errors=0" in report, report
    assert not any("NOT SENT" in m for m in on_run.messages if "gsmtap" in m)


def test_shadow_leaves_the_tap_to_the_bridge(tmp_path):
    """Shadow does not send: the bridge's datagrams, once each."""
    env = _bridge_env_or_skip()
    replay = _fixture_or_skip(FIXTURES[3], tmp_path)
    _, off = _run("off", replay, env)
    _, shadow = _run("shadow", replay, env)
    assert shadow == off and off


def _synthetic_nas_replay(tmp_path: Path) -> Path:
    spec = importlib.util.spec_from_file_location(
        "make_functional_fixture", _CELLDIAG / "fixtures" / "make_functional_fixture.py")
    fx = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fx)
    data = b"\x7e" + b"".join(fx.log_f_frame(code, 1000 + i, payload)
                              for i, (code, payload) in enumerate(_SYNTH_NAS))
    p = tmp_path / "synthetic_nas.hdlc"
    p.write_bytes(data)
    return p


def _nas_reference(tmp_path):
    env = _bridge_env_or_skip()
    replay = _synthetic_nas_replay(tmp_path)
    _, off = _run("off", replay, env)
    off_nas = _of_type(off, GSMTAP_TYPE_LTE_NAS)
    assert len(off_nas) == _SYNTH_NAS_FRAMED, (
        f"the bridge framed {len(off_nas)} of the synthetic NAS records, "
        f"expected {_SYNTH_NAS_FRAMED}")
    return env, replay, off


@pytest.mark.parametrize("mode", ["on", "only"])
def test_native_nas_datagrams_equal_the_bridges(mode, tmp_path):
    """Every NAS frame the bridge sends, natively, byte- and order-equal --
    and the truncated record left unsent by both."""
    env, replay, off = _nas_reference(tmp_path)
    run, got = _run(mode, replay, env, tmp_path)
    assert got == off, f"`{mode}` delivered {len(got)} NAS datagram(s), `off` {len(off)}"
    report = _native_report(run) or ""
    assert (f"sent={_SYNTH_NAS_FRAMED} declined=1 errors=0" in report
            and "no native framer" not in report), report
    assert not any("NOT SENT" in m for m in run.messages if "gsmtap" in m)


def test_on_gives_the_bridge_no_tap_when_native_frames_everything(tmp_path):
    """With a native framer for every eligible code, `on` leaves the bridge
    nothing to send, so it is started without --gsmtap: the INFO line says so,
    and the datagrams above came from one sender."""
    env, replay, _ = _nas_reference(tmp_path)
    run, _ = _run("on", replay, env)
    assert any("all framed natively" in m for m in run.messages), (
        [m for m in run.messages if "gsmtap" in m])


def test_only_rrc_datagrams_equal_the_bridges(tmp_path):
    """`only` frames 0xB0C0 exactly as the bridge does under `off`."""
    env = _bridge_env_or_skip()
    replay = _fixture_or_skip(FIXTURES[3], tmp_path)
    _, off = _run("off", replay, env)
    run, got = _run("only", replay, env, tmp_path)
    assert _of_type(got, GSMTAP_TYPE_LTE_RRC) == _of_type(off, GSMTAP_TYPE_LTE_RRC) != []


def test_the_eligible_set_matches_diagmunge():
    """DIAG_GSMTAP_ELIGIBLE_CODES is the bridge's eligible_codes(), member for
    member. A code diagmunge learns to frame must become a native candidate,
    or the native tap stops counting it -- not even as declined."""
    env = _bridge_env_or_skip()
    src = env["PYTHONPATH"].split(os.pathsep)[0]
    import sys
    sys.path.insert(0, src)
    try:
        from diagmunge import gsmtap
    except ImportError as e:
        pytest.skip(f"diagmunge at {src} does not import here: {e}")
    finally:
        sys.path.remove(src)
    c_src = (_CELLDIAG / "diag_gsmtap_sink.c").read_text()
    body = c_src.split("DIAG_GSMTAP_ELIGIBLE_CODES[] = {", 1)[1].split("}", 1)[0]
    c_set = {int(t, 16) for t in body.replace(",", " ").split()}
    assert c_set == set(gsmtap.eligible_codes())


#: The datagrams the synthetic NAS replay must produce, in order: pasted from
#: diagmunge.gsmtap.encode(code, diaggrok.parse(...)).to_bytes() for each
#: record above. The truncated 0xB0E2 produces none. Pinned, so the `only`
#: run -- which starts no Python -- is checked against the bridge's bytes
#: without needing the bridge. If diaggrok or diagmunge changes these frames,
#: regenerate them and the bridge-vs-native test above says whether native
#: followed.
_SYNTH_NAS_GOLDEN = [bytes.fromhex(h) for h in (
    "020412004000000000000000010000002700000000055201c1",   # 0xB0E1 protected UL
    "020412000000000000000000000000000761",                 # 0xB0E2 DL
    "020412004000000000000000000000000761",                 # 0xB0E3 UL
    "020412000000000000000000000000000761",                 # 0xB0EC DL
    "020412004000000000000000000000000761",                 # 0xB0ED UL
)]


def test_only_nas_datagrams_equal_the_pinned_golden(tmp_path):
    """`only`, Python-free, against pinned bytes: a golden, not an A/B."""
    replay = _synthetic_nas_replay(tmp_path)
    _, got = _run("only", replay, {}, tmp_path)
    assert got == _SYNTH_NAS_GOLDEN, [d.hex() for d in got]


def test_the_golden_is_what_the_bridge_sends(tmp_path):
    """The golden stays honest: it is the bridge's output for the same bytes."""
    _, _, off = _nas_reference(tmp_path)
    assert off == _SYNTH_NAS_GOLDEN
