# SPDX-License-Identifier: Apache-2.0
"""An installed celldiag finds its bridge and its pinned decoder.

``make install`` puts ``kismet_cap_cell_diag`` in ``$(BINDIR)``, where nothing
sits beside it. Both resolvers walk up from the binary -- the bridge script
(``resolve_helper_script``) and the decoder root (``self_tree_decoder_root``) --
so on their own they would find neither from an installed binary.

So ``make install`` runs ``standalone.mk install-decoder``, which installs the
bridge and the fetched decoder under ``<datadir>/kismet/cell`` -- the decoder
ONLY if ``.deps/diaggrok`` is at the pinned SHA, and never fetching -- and the
autoconf build compiles that directory in as both resolvers' LAST candidate.

These tests build a standalone baseline with ``CELLDIAG_DATADIR`` pointed at a
temp "share" dir, install into it with the real target, and replay the tree's
own ``functional`` fixture through the binary from a separate "bin" dir. The
interpreter is forced to the system ``python3``, which cannot import diaggrok on
its own: any observation relayed proves the installed decoder was used.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kismet_capture_ipc import CaptureRun  # noqa: E402

TREE = Path(__file__).resolve().parents[2]
CELLDIAG = TREE / "capture_cell_diag"
FIXTURE = CELLDIAG / "fixtures" / "functional.hdlc"
IMEI = "000000000000000"          # standalone.mk FUNCTIONAL_IMEI
SYSTEM_PYTHON = "/usr/bin/python3"


def _pin() -> str:
    for line in (CELLDIAG / "standalone.mk").read_text().splitlines():
        if line.startswith("DIAGGROK_SHA ?="):
            return line.split("=", 1)[1].strip()
    raise AssertionError("no DIAGGROK_SHA in standalone.mk")


def _fetched_and_pinned() -> bool:
    deps = CELLDIAG / ".deps" / "diaggrok"
    r = subprocess.run(["git", "-C", str(deps), "rev-parse", "HEAD"],
                       capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() == _pin()


def _system_python_lacks_diaggrok() -> bool:
    if not os.access(SYSTEM_PYTHON, os.X_OK):
        return False
    r = subprocess.run([SYSTEM_PYTHON, "-c", "import diaggrok"],
                       capture_output=True, env={"PATH": "/usr/bin:/bin"})
    return r.returncode != 0


def _install_decoder(share: Path, **make_vars: str) -> subprocess.CompletedProcess:
    args = ["make", "--no-print-directory", "-C", str(CELLDIAG), "-f", "standalone.mk",
            "install-decoder", f"CELL_SHARE={share}"]
    args += [f"{k}={v}" for k, v in make_vars.items()]
    return subprocess.run(args, capture_output=True, text=True, timeout=120)


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    """One baseline build with CELLDIAG_DATADIR baked in, shared by the module."""
    if not sys.platform.startswith("linux"):
        pytest.skip("the baseline build here is exercised on Linux")
    if shutil.which("make") is None or shutil.which("cc") is None:
        pytest.skip("needs make and cc")
    if not _fetched_and_pinned():
        pytest.skip("capture_cell_diag/.deps/diaggrok is not fetched at the pin "
                    "(run `make -f standalone.mk deps`)")
    if not _system_python_lacks_diaggrok():
        pytest.skip(f"needs a {SYSTEM_PYTHON} that cannot import diaggrok by itself")
    root = tmp_path_factory.mktemp("inst4989")
    share = root / "share" / "kismet" / "cell"
    build = root / "build"
    r = subprocess.run(
        ["make", "--no-print-directory", "-C", str(CELLDIAG), "-f", "standalone.mk",
         "baseline", f"BASELINE_DIR={build}",
         f'CFLAGS=-O1 -DCELLDIAG_DATADIR=\\"{share}\\"'],
        capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    bindir = root / "bin"
    bindir.mkdir()
    binary = bindir / "kismet_cap_cell_diag"
    shutil.copy2(build / "kismet_cap_cell_diag", binary)
    shutil.rmtree(build)       # nothing may resolve from the build dir
    return root, share, binary


def _run(binary: Path) -> CaptureRun:
    definition = f"celldiag-{IMEI}:replay={FIXTURE}"
    # An allow-listed environment: nothing ambient can reach the decoder, and
    # the python3 on this PATH is the system one, which cannot import diaggrok
    # by itself (the module checks SYSTEM_PYTHON is that interpreter).
    env = {"PATH": "/usr/bin:/bin"}
    return CaptureRun(str(binary), definition, timeout=60, env=env).run()


def _blob(run: CaptureRun) -> str:
    return (run.open_message or "") + " " + " ".join(run.errors)


def test_without_an_install_the_binary_refuses_and_names_the_data_dir(installed):
    """Nothing installed: the refusal must name where an installed binary
    looked, and the build-time fix. The module's install (if another test made
    it first -- order is not fixed) is set aside for the run."""
    _, share, binary = installed
    aside = share.with_name(share.name + ".aside")
    if share.exists():
        share.rename(aside)
    try:
        run = _run(binary)
    finally:
        if aside.exists():
            aside.rename(share)
    assert run.open_code == 0, _blob(run)
    blob = _blob(run)
    assert str(share) in blob, f"refusal does not name the data dir: {blob!r}"
    assert "make -f standalone.mk deps" in blob and "make install" in blob, blob


def test_install_decoder_installs_the_bridge_and_the_pinned_decoder(installed):
    _, share, _ = installed
    r = _install_decoder(share)
    assert r.returncode == 0, r.stderr
    assert (share / "kismet_diag_decode.py").is_file()
    assert (share / ".deps" / "diaggrok" / "src" / "diaggrok" / "__init__.py").is_file()
    assert (share / ".deps" / "diaggrok" / "PINNED_SHA").read_text().strip() == _pin()
    assert not list((share / ".deps").rglob("__pycache__")), "a bytecode cache was installed"


def test_an_installed_binary_decodes_with_the_installed_decoder(installed):
    """The claim: binary in bin/, nothing beside it, nothing configured, a
    system python3 that cannot import diaggrok -- and observations arrive."""
    _, share, binary = installed
    if not (share / ".deps").exists():
        assert _install_decoder(share).returncode == 0
    run = _run(binary)
    assert run.open_code == 1, _blob(run)
    assert len(run.observations) > 0, (
        f"opened on the installed layout but relayed nothing: {_blob(run)!r}")


def test_an_unpinned_decoder_is_never_installed(tmp_path):
    """The pin is what keeps the installed decoder honest: a tree whose
    .deps/diaggrok is not a clone at DIAGGROK_SHA installs the bridge only,
    and says why. No network either way."""
    fake = tmp_path / "fake-diaggrok"
    (fake / "src" / "diaggrok").mkdir(parents=True)
    (fake / "src" / "diaggrok" / "__init__.py").write_text("")
    share = tmp_path / "share"
    r = _install_decoder(share, DIAGGROK_DIR=str(fake))
    assert r.returncode == 0, r.stderr
    assert (share / "kismet_diag_decode.py").is_file()
    assert not (share / ".deps").exists(), "an unpinned decoder was installed"
    assert "NO DECODER INSTALLED" in r.stderr and _pin() in r.stderr, r.stderr


def test_an_unfetched_decoder_installs_the_bridge_and_says_so(tmp_path):
    share = tmp_path / "share"
    r = _install_decoder(share, DIAGGROK_DIR=str(tmp_path / "never-fetched"))
    assert r.returncode == 0, r.stderr
    assert (share / "kismet_diag_decode.py").is_file()
    assert not (share / ".deps").exists()
    assert "never fetched" in r.stderr, r.stderr
