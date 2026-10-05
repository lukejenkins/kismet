# SPDX-License-Identifier: Apache-2.0
"""celldiag finds its decoder with nothing to configure.

The contract, and every test below pins one clause of it:

* **The decoder root is the binary's own tree** -- the nearest
  ``.deps/diaggrok/src`` walking up at most ``HELPER_SCRIPT_MAX_UP`` levels from
  the binary -- else the installed data dir. There is no option,
  environment variable or compiled-in path that names another one. A developer
  who wants a different decoder makes ``.deps/diaggrok`` be it.
* **Keyed on the fetched decoder**, not on the tree merely existing. A tree that
  never ran ``make -f standalone.mk deps`` is refused at open, loudly, rather
  than starting a bridge that dies at ``import diaggrok`` and relays nothing --
  a silent ``obs=0``.
* **The bridge comes from the same root** as the decoder it imports.
* **The interpreter is ``python3`` off ``$PATH``**, with the decoder root's
  ``src`` FIRST on the child's ``PYTHONPATH``.
* **``helper=`` is refused**, not ignored: it used to select the decoder, and
  Kismet drops unknown options silently, so ignoring it would quietly switch an
  existing definition to a different decoder.

Each test builds its own tree around a copy of the binary (``celldiag_stub_tree``).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from celldiag_stub_tree import (REAL_CAPDIR, python3_shim, run_env,  # noqa: E402
                                stub_tree)
from kismet_capture_ipc import CaptureRun  # noqa: E402

IMEI = "123456789012345"


def _open(binary: Path, tmp_path: Path, *, extra: str = "",
          env: dict | None = None, replay: Path | None = None,
          timeout: float = 20) -> CaptureRun:
    if replay is None:
        replay = tmp_path / "empty.hdlc"
        replay.write_bytes(b"")
    definition = f"celldiag-{IMEI}:replay={replay}{extra}"
    return CaptureRun(str(binary), definition, timeout=timeout,
                      env=env if env is not None else run_env()).run()


def _blob(run: CaptureRun) -> str:
    return (run.open_message or "") + " " + " ".join(run.errors)


# ── the decoder root ─────────────────────────────────────────────────────────

def test_a_tree_with_its_fetched_decoder_opens(tmp_path):
    """The gate: nothing configured, ``.deps/diaggrok/src`` beside the binary."""
    run = _open(stub_tree(tmp_path), tmp_path)
    assert run.open_code == 1, (
        f"a tree WITH its fetched decoder was refused (code={run.open_code}): "
        f"{_blob(run)!r}")


def test_a_binary_in_a_build_subdirectory_finds_the_decoder_above_it(tmp_path):
    """The bounded upward walk: ``capture_cell_diag/build-baseline/`` is where
    ``standalone.mk baseline`` puts the binary, one level below the decoder."""
    binary = stub_tree(tmp_path)
    sub = binary.parent / "build-baseline"
    sub.mkdir()
    moved = sub / binary.name
    binary.rename(moved)
    run = _open(moved, tmp_path)
    assert run.open_code == 1, _blob(run)


def test_without_the_fetched_decoder_it_refuses_and_names_the_fix(tmp_path):
    """Negative control, and the reason the lookup is keyed on ``.deps``: no
    decoder, no bridge started. The refusal names the ONE fix and the directory
    it searched, and nothing that points anywhere else."""
    binary = stub_tree(tmp_path, deps=False)
    run = _open(binary, tmp_path)
    assert run.open_code == 0, (
        f"a tree with no fetched decoder was accepted (code={run.open_code}) -- "
        "this is the silent obs=0 shape")
    blob = _blob(run)
    assert "decoder not found" in blob, blob
    assert "make -f standalone.mk deps" in blob, blob
    assert str(binary.parent.resolve()) in blob, (
        f"the refusal does not name where it looked: {blob!r}")


def test_a_decoder_without_a_bridge_beside_it_is_refused(tmp_path):
    run = _open(stub_tree(tmp_path, bridge=None), tmp_path)
    assert run.open_code == 0, _blob(run)
    assert "decoder script not found" in _blob(run), _blob(run)


def test_helper_is_refused_not_silently_ignored(tmp_path):
    """The removed option must not become a no-op: a definition that still
    names one decoder would otherwise quietly decode with another."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    run = _open(stub_tree(tmp_path), tmp_path, extra=f",helper={other}")
    assert run.open_code == 0, (
        f"helper= was accepted (code={run.open_code}) -- it is being ignored")
    assert "helper= source option was removed" in _blob(run), _blob(run)


# ── the interpreter and PYTHONPATH ───────────────────────────────────────────

def test_python3_off_PATH_runs_with_the_trees_decoder_first(tmp_path):
    """The interpreter is the first ``python3`` on ``$PATH``, and the child's
    PYTHONPATH starts with THIS tree's decoder -- ahead of an ambient entry."""
    binary = stub_tree(tmp_path)
    seen = tmp_path / "pythonpath.txt"
    shim = python3_shim(tmp_path,
                        f'#!/bin/sh\necho "$PYTHONPATH" > {seen}\n'
                        f'exec {sys.executable} "$@"\n')
    env = run_env(shim)
    env["PYTHONPATH"] = "/ambient/first"
    run = _open(binary, tmp_path, env=env)
    assert run.open_code == 1, _blob(run)
    assert seen.exists(), "the python3 first on PATH never ran"
    got = seen.read_text().strip().split(":")
    want = str(binary.parent / ".deps" / "diaggrok" / "src")
    assert got[0] == want, f"the tree's decoder is not FIRST on PYTHONPATH: {got}"
    assert "/ambient/first" in got, f"the ambient PYTHONPATH was dropped: {got}"


def test_a_real_decode_under_a_bare_system_python3(tmp_path):
    """The end-to-end proof, and the only test that shows the PYTHONPATH
    wiring lets the child import diaggrok and decode (obs=0 is the absence of
    exactly this). The real bridge and this tree's real fetched decoder, under
    a bare ``/usr/bin/python3`` that cannot import diaggrok by itself -- so the
    C's PYTHONPATH is the only route to a successful import. (With an ambient
    venv on PATH, a mutation that cut the wiring would survive.)"""
    bare = "/usr/bin/python3"
    fixture = REAL_CAPDIR / "fixtures" / "functional.hdlc"
    real_src = REAL_CAPDIR / ".deps" / "diaggrok" / "src"
    if not fixture.is_file() or not (real_src / "diaggrok").is_dir():
        pytest.skip("run `make -f standalone.mk deps` first: no fetched decoder / fixture")
    if not os.access(bare, os.X_OK):
        pytest.skip(f"no bare interpreter at {bare}")
    probe = subprocess.run([bare, "-c", "import diaggrok"],
                           env={"PATH": "/usr/bin:/bin"}, capture_output=True)
    if probe.returncode == 0:
        pytest.skip(f"{bare} already imports diaggrok; cannot isolate the C's PYTHONPATH")

    binary = stub_tree(tmp_path, bridge=REAL_CAPDIR / "kismet_diag_decode.py",
                       deps=real_src.resolve())
    run = _open(binary, tmp_path, env={"PATH": "/usr/bin:/bin"},
                replay=fixture, timeout=30)
    assert run.open_code == 1, _blob(run)
    stats = " ".join(run.messages)
    assert "RX-NO-OBS" not in stats, (
        "the bridge relayed NO observations -- it did not import the decoder, so "
        f"the C did not wire it onto the child's PYTHONPATH. messages={run.messages}")
    assert len(run.observations) >= 4, (
        f"only {len(run.observations)} object(s) relayed; the fixture's 4 cell "
        f"observations did not come through. errors={run.errors}")


# ── the --f3 capability probe ────────────────────────────────────────────────

#: An argparse-shaped options block that really does offer the flag.
_HELP_WITH_F3 = (
    "usage: kismet_diag_decode.py [-h] [--f3 {all,high,error}]\n"
    "\noptions:\n"
    "  -h, --help            show this help message and exit\n"
    "  --f3 {all,high,error} relay F3 prints as diag_f3 records\n")

#: The public bridge's shape: the flag appears only in PROSE, saying it is gone.
_HELP_WITHOUT_F3 = (
    "usage: kismet_diag_decode.py [-h]\n"
    "\nF3 relay is ABSENT, not disabled.\n"
    "There is no `--f3` flag and no `f3=` parameter to find. An earlier\n"
    "revision had both, and the flag was never the guard it looked like.\n"
    "\noptions:\n"
    "  -h, --help            show this help message and exit\n")


def _f3_probe_run(tmp_path: Path, help_text: str) -> Path:
    """A bridge that prints ``help_text`` for --help and otherwise records its
    argv and lingers. Returns the argv marker."""
    marker = tmp_path / "argv.txt"
    bridge = ("import sys, pathlib, time\n"
              "if '--help' in sys.argv:\n"
              f"    sys.stdout.write({help_text!r})\n"
              "    raise SystemExit(0)\n"
              f"pathlib.Path({str(marker)!r}).write_text(' '.join(sys.argv))\n"
              "time.sleep(3)\n")
    _open(stub_tree(tmp_path, bridge=bridge), tmp_path, extra=",f3=all")
    return marker


def test_a_bridge_that_offers_f3_is_given_it(tmp_path):
    """The probe's POSITIVE control: without it the prose-trap test below would
    pass against a probe hardwired to "no", and the relay would be silently
    dead on every bridge."""
    marker = _f3_probe_run(tmp_path, _HELP_WITH_F3)
    assert marker.exists(), "the bridge was never spawned"
    assert "--f3" in marker.read_text().split(), marker.read_text()


def test_the_f3_prose_trap_does_not_read_as_support(tmp_path):
    """argparse prints the docstring, and the public bridge's docstring says
    *"There is no `--f3` flag."* A probe that matches prose passes the flag and
    kills the bridge at argparse -- the bug it exists to prevent."""
    marker = _f3_probe_run(tmp_path, _HELP_WITHOUT_F3)
    assert marker.exists(), "the bridge was never spawned"
    assert "--f3" not in marker.read_text().split(), marker.read_text()
