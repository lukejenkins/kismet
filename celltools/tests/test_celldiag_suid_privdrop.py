# SPDX-License-Identifier: Apache-2.0
"""A suid-installed celldiag runs its caller-chosen bridge as the caller.

``make suidinstall`` installs ``kismet_cap_cell_diag`` setuid root, mode 4550.
The bridge interpreter it execs is the caller's choice: ``python3`` off the
caller's ``$PATH``. Unless the elevated ids are dropped first, a member of the
suid group could put any program first on ``$PATH`` and get it run as root.

The binary is started in the exact id state a suid-root exec by ``nobody``
produces: real uid/gid 65534, effective and saved 0. The interpreter is a shim
that records the ids it REALLY holds (from ``/proc/self/status``), so the test
reads what the kernel says, not what the code meant to do. Both children are
covered: the ``--help`` capability probe, which only runs with ``f3=`` armed,
and the bridge spawn itself.

Linux and root only: only root can build that id state. The tree lives under a
world-traversable directory because, after the drop, the shim and its output
file are touched as ``nobody``.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import celldiag_parity  # noqa: E402
from kismet_capture_ipc import CaptureRun  # noqa: E402

IMEI = "123456789012345"
NOBODY = 65534


SYSTEM_PYTHON = "/usr/bin/python3"


def _binary_or_skip() -> Path:
    if not os.access(SYSTEM_PYTHON, os.X_OK):
        pytest.skip(f"the id-recording shim needs {SYSTEM_PYTHON}")
    if not sys.platform.startswith("linux"):
        pytest.skip("the suid-state simulation needs Linux setresuid + /proc")
    if os.geteuid() != 0:
        pytest.skip("only root can start a process in a suid install's id state")
    found = celldiag_parity.discover("celldiag")
    if not found.ok:
        pytest.skip(found.skip_reason())
    return Path(found.path)


def _as_suid_root_exec_by_nobody() -> None:
    # Group first: once the uid is not 0 the gid can no longer be set.
    os.setresgid(NOBODY, 0, 0)
    os.setresuid(NOBODY, 0, 0)


@pytest.fixture
def world_dir():
    d = Path(tempfile.mkdtemp(prefix="celldiag-4988-", dir="/tmp"))
    d.chmod(0o755)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _setup(root: Path) -> tuple[Path, Path, Path]:
    """The binary + a stand-in bridge + a bundled-decoder dir (the self-tree
    shape), and an interpreter shim that appends its real ids to ``ids``."""
    capdir = root / "tree" / "capture_cell_diag"
    capdir.mkdir(parents=True)
    shutil.copy2(_binary_or_skip(), capdir / "kismet_cap_cell_diag")
    (capdir / "kismet_diag_decode.py").write_text("# stand-in; the shim never reads it\n")
    (capdir / ".deps" / "diaggrok" / "src" / "diaggrok").mkdir(parents=True)

    out = root / "out"
    out.mkdir()
    out.chmod(0o777)
    ids = out / "ids.txt"
    ids.touch()
    ids.chmod(0o666)

    shim = root / "shim" / "python3"
    shim.parent.mkdir()
    # Not a /bin/sh script: bash and dash both drop privileges by themselves
    # when started with euid != ruid (privileged mode, no -p), so a shell shim
    # reads the caller's ids whether or not celldiag dropped them, and the
    # positive control below would fail. Python does not drop, which is exactly
    # why celldiag's execv(python) needs celldiag to drop first.
    # System python3, because after the drop the shim runs as nobody.
    # Record ids, then answer the probe (`--help`, listing --f3 so the real
    # spawn is asked for it) or stay alive as the bridge would.
    shim.write_text(
        f"#!{SYSTEM_PYTHON}\n"
        "import sys, time\n"
        f"with open({str(ids)!r}, 'a') as out:\n"
        "    for ln in open('/proc/self/status'):\n"
        "        if ln.startswith(('Uid:', 'Gid:')):\n"
        "            out.write(' '.join(ln.split()) + '\\n')\n"
        "if '--help' in sys.argv:\n"
        "    print('  --f3')\n"
        "    sys.exit(0)\n"
        "time.sleep(3)\n")
    shim.chmod(0o755)
    for d in (root / "tree", capdir, shim.parent):
        d.chmod(d.stat().st_mode | stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP
                | stat.S_IROTH | stat.S_IXOTH)
    return capdir / "kismet_cap_cell_diag", shim, ids


def _run(binary: Path, shim: Path, root: Path, extra: str,
         preexec=_as_suid_root_exec_by_nobody) -> CaptureRun:
    replay = root / "empty.hdlc"
    replay.write_bytes(b"")
    definition = f"celldiag-{IMEI}:replay={replay}{extra}"
    # An allow-listed environment: nothing ambient can pick the decoder.
    env = {"PATH": f"{shim.parent}:" + os.environ.get("PATH", "/usr/bin:/bin")}
    return CaptureRun(str(binary), definition, timeout=20,
                      preexec_fn=preexec, env=env).run()


def _id_lines(ids: Path) -> list[str]:
    return [ln.strip() for ln in ids.read_text().splitlines() if ln.strip()]


@pytest.mark.parametrize("extra,children", [
    ("", 1),               # the bridge spawn only
    (",f3=all", 2),        # the --help probe, then the bridge spawn
])
def test_a_suid_installs_bridge_runs_with_the_callers_ids(world_dir, extra, children):
    binary, shim, ids = _setup(world_dir)
    run = _run(binary, shim, world_dir, extra)
    lines = _id_lines(ids)
    assert len(lines) == 2 * children, (
        f"expected {children} interpreter start(s) (Uid+Gid each), saw {lines!r}; "
        f"open code={run.open_code} msg={run.open_message!r} errors={run.errors!r}")
    want = {f"Uid: {NOBODY} {NOBODY} {NOBODY} {NOBODY}",
            f"Gid: {NOBODY} {NOBODY} {NOBODY} {NOBODY}"}
    kept = [ln for ln in lines if ln not in want]
    assert not kept, (
        f"the caller-chosen interpreter ran with a suid install's elevated ids: {kept!r}")


def test_the_gauge_sees_elevated_ids_when_nothing_drops_them(world_dir):
    """Positive control. The same shim, exec'd straight from the suid state with
    no capture binary in between, must read the ELEVATED ids -- otherwise the
    test above could pass because the simulation never elevated anything."""
    _, shim, ids = _setup(world_dir)
    subprocess.run([str(shim), "--help"], preexec_fn=_as_suid_root_exec_by_nobody,
                   stdout=subprocess.DEVNULL, check=True, timeout=10)
    lines = _id_lines(ids)
    assert f"Uid: {NOBODY} 0 0 0" in lines, lines
    assert f"Gid: {NOBODY} 0 0 0" in lines, lines


def test_without_suid_the_bridge_is_unaffected(world_dir):
    """Not suid: nothing to drop, and the bridge must still start (as root here,
    the harness's own ids). A drop that refused here would break every ordinary
    install."""
    binary, shim, ids = _setup(world_dir)
    run = _run(binary, shim, world_dir, "", preexec=None)
    assert run.open_code == 1, (run.open_message, run.errors)
    lines = _id_lines(ids)
    assert "Uid: 0 0 0 0" in lines, lines
