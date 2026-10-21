# SPDX-License-Identifier: Apache-2.0
"""Tests that expect "nothing found" must not find this host's install.

An autoconf build compiles ``<datadir>/kismet/cell`` in as the last place to
look for the decoder and the QMI feed. After ``make install`` that directory is
real, so a "nothing beside this binary" test would open instead of refusing: a
false RED on any host that has run ``make install``.
``celldiag_stub_tree.forget_installed_datadir`` rewrites that path in a test's
COPY of the binary. These tests pin what it does, and show on an installed host
that the false RED is real without it.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from celldiag_stub_tree import binary_or_skip, forget_installed_datadir, run_env
from kismet_capture_ipc import CaptureRun

IMEI = "000000000000000"


def _lone_copy(tmp_path: Path) -> Path:
    lone = tmp_path / "bin" / "kismet_cap_cell_diag"
    lone.parent.mkdir()
    shutil.copy2(binary_or_skip(), lone)
    return lone


def _open(binary: Path, tmp_path: Path) -> CaptureRun:
    missing = tmp_path / "no-such-replay.hdlc"
    return CaptureRun(str(binary), f"celldiag-{IMEI}:replay={missing}",
                      timeout=20, env=run_env()).run()


def test_the_copy_no_longer_names_the_installed_dir_and_the_original_is_untouched(tmp_path):
    original = binary_or_skip()
    before = original.read_bytes()
    lone = _lone_copy(tmp_path)
    replaced = forget_installed_datadir(lone)
    if not replaced:
        pytest.skip("this build compiles in no installed data dir (standalone.mk)")
    patched = lone.read_bytes()
    assert len(patched) == len(before), "the rewrite must not move anything"
    for path in replaced:
        assert path.endswith("/kismet/cell"), path
        assert path.encode() + b"\x00" not in patched, f"{path} survived the rewrite"
    assert original.read_bytes() == before, "the tree's own binary was modified"


def test_a_binary_with_no_data_dir_compiled_in_is_left_alone(tmp_path):
    blob = tmp_path / "blob"
    data = b"\x00ELF-ish\x00/usr/local/share/kismet\x00cell\x00" + os.urandom(64)
    blob.write_bytes(data)
    assert forget_installed_datadir(blob) == []
    assert blob.read_bytes() == data


def test_a_patched_copy_outside_any_tree_is_refused(tmp_path):
    lone = _lone_copy(tmp_path)
    forget_installed_datadir(lone)
    run = _open(lone, tmp_path)
    assert run.open_code != 1, (
        f"a lone copy with its install forgotten still opened: {run.open_message!r}")
    assert "decoder not found" in (run.open_message or ""), run.open_message


def test_positive_control_an_unpatched_copy_finds_this_hosts_install(tmp_path):
    """The false RED is real here: without the rewrite the lone copy OPENS.

    Skipped on a host with no install.
    """
    lone = _lone_copy(tmp_path)
    probe = tmp_path / "probe"
    shutil.copy2(lone, probe)
    installed = [p for p in forget_installed_datadir(probe)
                 if (Path(p) / ".deps" / "diaggrok" / "src").is_dir()]
    if not installed:
        pytest.skip("no installed decoder on this host (no `make install` ran)")
    run = _open(lone, tmp_path)
    assert run.open_code == 1, (
        f"expected the unpatched copy to find {installed[0]}, but it was "
        f"refused: {run.open_message!r}")
