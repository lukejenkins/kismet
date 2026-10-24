# SPDX-License-Identifier: Apache-2.0
"""A throwaway source tree around a COPY of the celldiag binary.

celldiag has nothing to configure: its decoder is whatever
``.deps/diaggrok`` its own tree carries, its bridge is the
``kismet_diag_decode.py`` beside it, and its interpreter is ``python3`` off
``$PATH``. So the only way a test can hand it a particular bridge or decoder is
the way a user would -- build the tree -- and the only way to hand it a
particular interpreter is ``$PATH``.

The binary is COPIED, not run in place: the decoder root is found from
``/proc/self/exe`` (``_NSGetExecutablePath`` on Darwin), so running it in place
would test whatever ``.deps`` this host's real tree happens to hold.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import celldiag_parity  # noqa: E402

#: A bridge stand-in that stays alive long enough for the open to be judged
#: alive (an exited helper reads as `helper=dead`). It imports nothing, so a test
#: using it is about RESOLUTION, not decode.
SLEEPING_BRIDGE = "import time\ntime.sleep(3)\n"

#: This tree's real capture_cell_diag/ -- the bridge and fetched decoder a stub
#: tree can borrow when a test needs a real decode.
REAL_CAPDIR = Path(celldiag_parity.__file__).resolve().parents[1] / "capture_cell_diag"


def binary_or_skip() -> Path:
    found = celldiag_parity.discover("celldiag")
    if not found.ok:
        pytest.skip(found.skip_reason())
    return Path(found.path)


#: A compiled-in installed data dir: a whole C string ending in /kismet/cell
#: (CELLDIAG_DATADIR, QMIFEED_DATADIR = "@datadir@/kismet/cell").
_DATADIR_RE = re.compile(rb"(?<=\x00)/[^\x00]*/kismet/cell(?=\x00)")


def forget_installed_datadir(binary: Path) -> list[str]:
    """Point a COPIED binary's compiled-in installed data dir at nowhere.

    An autoconf build compiles ``<datadir>/kismet/cell`` in as the LAST place
    to look for the decoder and the QMI feed. On a host that ran
    ``make install`` that directory is real, so a "nothing beside this binary"
    test finds the INSTALLED copy and the open is accepted: a false RED that
    reads as a regression. Rewriting the path in the test's own copy (same
    length, so nothing moves) acts as a build-time ``CELLDIAG_DATADIR`` without
    a second link and without a runtime override in the shipped binary, which
    deliberately has none. The binary under test is
    otherwise byte-identical. Returns the paths it replaced; a build with none
    compiled in (``standalone.mk``) returns [].
    """
    data = binary.read_bytes()
    found = [m.group(0).decode() for m in _DATADIR_RE.finditer(data)]
    if not found:
        return []
    out = _DATADIR_RE.sub(lambda m: b"/" + b"_" * (len(m.group(0)) - 1), data)
    assert not Path("/" + "_" * (len(found[0]) - 1)).exists()
    binary.write_bytes(out)
    if sys.platform == "darwin":
        # A patched Mach-O loses its ad-hoc signature; arm64 kills it on exec.
        if shutil.which("codesign") is None:
            pytest.skip("cannot re-sign a patched binary: no codesign")
        subprocess.run(["codesign", "--force", "-s", "-", str(binary)],
                       check=True, capture_output=True)
    return found


def stub_tree(tmp_path: Path, *, bridge: str | Path | None = SLEEPING_BRIDGE,
              deps: bool | Path = True, name: str = "tree") -> Path:
    """Lay out ``<tmp_path>/<name>/capture_cell_diag/`` and return its binary.

    ``bridge``: the bridge's source text, a Path to symlink in (the real
    bridge), or None for no bridge. ``deps``: True for an empty decoder root
    (enough to be FOUND, not to decode), a Path to symlink in as
    ``.deps/diaggrok/src`` (a real decoder), or False for none.
    """
    capdir = tmp_path / name / "capture_cell_diag"
    capdir.mkdir(parents=True)
    binary = capdir / "kismet_cap_cell_diag"
    shutil.copy2(binary_or_skip(), binary)
    # A stub tree is judged by its tree alone, never by this host's install.
    forget_installed_datadir(binary)
    script = capdir / "kismet_diag_decode.py"
    if isinstance(bridge, Path):
        script.symlink_to(bridge)
    elif bridge is not None:
        script.write_text(bridge)
    src = capdir / ".deps" / "diaggrok" / "src"
    if isinstance(deps, Path):
        src.parent.mkdir(parents=True)
        src.symlink_to(deps)
    elif deps:
        (src / "diaggrok").mkdir(parents=True)
    return binary


def python3_shim(tmp_path: Path, body: str, name: str = "shim") -> Path:
    """A directory holding an executable ``python3`` whose text is ``body``.

    Put the directory FIRST on the run's ``PATH`` and celldiag execs it as the
    bridge interpreter. Returns the directory."""
    d = tmp_path / name
    d.mkdir()
    shim = d / "python3"
    shim.write_text(body)
    shim.chmod(0o755)
    return d


def run_env(*path_first: Path) -> dict[str, str]:
    """An allow-listed environment for ``CaptureRun(env=...)``: ``PATH`` with
    ``path_first`` in front of this process's own, and nothing else, so nothing
    ambient can steer the decoder or the interpreter."""
    path = os.environ.get("PATH", "/usr/bin:/bin")
    return {"PATH": os.pathsep.join([*(str(p) for p in path_first), path])}
