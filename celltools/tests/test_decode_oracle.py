# SPDX-License-Identifier: Apache-2.0
"""Direct tests for ``decode_oracle.discover()``.

``decode_oracle`` decides whether the celldiag parity contract -- the A/B that
*defines* whether this tree's C decode path agrees with the reference decoder --
runs at all. If it answers wrongly, the whole harness skips silently.

The oracle has nothing to configure: it finds the reference exactly where the
binary finds its decoder. Every test here lays out a synthetic tree in
``tmp_path``, so the answers are the same on a dev host and on a bare CI box.

The status strings are asserted, not just ``ok``: a skip with no reason
reports zero coverage as health, so a test that checked only
``ok is False`` would pass on a wrong-but-silent answer too.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

import decode_oracle

_CAPTURE_C = Path(decode_oracle.__file__).resolve().parents[1] / \
    "capture_cell_diag" / "capture_cell_diag.c"


def _tree(root: Path, *, decoder: bool = True, bridge: bool = True) -> Path:
    """A synthetic tree with any subset of the decoder and the bridge."""
    capdir = root / "capture_cell_diag"
    capdir.mkdir(parents=True)
    if decoder:
        (root / decode_oracle.DECODER_SRC_REL / "diaggrok").mkdir(parents=True)
    if bridge:
        (root / decode_oracle.HELPER_SCRIPT_RELS[0]).write_text("# bridge\n")
    return root


@pytest.fixture
def python3(monkeypatch, tmp_path):
    """A known python3 first on PATH, so the interpreter leg is deterministic."""
    d = tmp_path / "bin"
    d.mkdir()
    (d / "python3").symlink_to(sys.executable)
    monkeypatch.setenv("PATH", str(d))
    return d / "python3"


def test_a_tree_with_decoder_and_bridge_resolves(python3, tmp_path):
    root = _tree(tmp_path / "t")
    ora = decode_oracle.discover(root)
    assert ora.status == "tree" and ora.ok, ora.detail
    assert ora.script == root / decode_oracle.HELPER_SCRIPT_RELS[0]
    assert ora.python == python3
    assert ora.pythonpath == root / decode_oracle.DECODER_SRC_REL
    assert ora.cmd("--x") == [str(python3), str(ora.script), "--x"]


def test_no_fetched_decoder_is_no_decoder_and_names_the_fix(python3, tmp_path):
    ora = decode_oracle.discover(_tree(tmp_path / "t", decoder=False))
    assert ora.status == "no-decoder" and not ora.ok
    assert "make -f standalone.mk deps" in ora.detail, ora.detail
    assert "NOT a pass" in ora.detail, ora.detail


def test_a_decoder_without_a_bridge_is_no_bridge(python3, tmp_path):
    ora = decode_oracle.discover(_tree(tmp_path / "t", bridge=False))
    assert ora.status == "no-bridge" and not ora.ok, ora.detail


def test_no_python3_anywhere_is_no_interpreter(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(decode_oracle.os, "access", lambda p, m: False)
    ora = decode_oracle.discover(_tree(tmp_path / "t"))
    assert ora.status == "no-interpreter" and not ora.ok, ora.detail


def test_env_puts_the_trees_decoder_FIRST(python3, tmp_path):
    """Same wiring as the C's ``child_prepend_decoder_pythonpath``: first, so
    the tree's decoder beats an ambient PYTHONPATH and the interpreter's own
    site-packages."""
    ora = decode_oracle.discover(_tree(tmp_path / "t"))
    env = ora.env({"PYTHONPATH": "/ambient"})
    assert env["PYTHONPATH"].split(":") == [str(ora.pythonpath), "/ambient"]
    assert ora.env({})["PYTHONPATH"] == str(ora.pythonpath)


def test_skip_reason_names_the_status(python3, tmp_path):
    ora = decode_oracle.discover(_tree(tmp_path / "t", decoder=False))
    assert ora.skip_reason().startswith("[no-decoder] ")


def test_cmd_refuses_when_there_is_no_oracle(python3, tmp_path):
    ora = decode_oracle.discover(_tree(tmp_path / "t", decoder=False))
    with pytest.raises(RuntimeError, match="no decode oracle"):
        ora.cmd()


def test_import_diaggrok_prefers_the_trees_decoder(python3, tmp_path):
    """The tree's decoder is the reference the binary decodes with, so an
    in-process import must reach it before any ambient copy."""
    root = _tree(tmp_path / "t")
    pkg = root / decode_oracle.DECODER_SRC_REL / "oracle_probe_3498"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("WHERE = 'tree'\n")
    src = str(root / decode_oracle.DECODER_SRC_REL)
    try:
        mod = decode_oracle.import_diaggrok("oracle_probe_3498", tree=root)
        assert mod is not None and mod.WHERE == "tree"
    finally:
        sys.modules.pop("oracle_probe_3498", None)
        while src in sys.path:
            sys.path.remove(src)


def test_the_oracle_looks_where_the_binary_looks():
    """The two A/B legs must resolve the same decoder. Read from the C
    source rather than restated here, so an edit to either side that the other
    does not follow fails this test instead of splitting the legs silently."""
    c = _CAPTURE_C.read_text()
    m = re.search(r'#define\s+HELPER_DEPS_SRC_REL\s+"([^"]+)"', c)
    assert m, "HELPER_DEPS_SRC_REL not found in capture_cell_diag.c"
    assert decode_oracle.DECODER_SRC_REL == f"capture_cell_diag/{m.group(1)}"
    block = re.search(r"helper_script_rels\[\]\s*=\s*\{(.*?)\};", c, re.S)
    assert block, "helper_script_rels not found in capture_cell_diag.c"
    c_rels = re.findall(r'"([^"]+)"', block.group(1))
    for rel in decode_oracle.HELPER_SCRIPT_RELS:
        assert rel in c_rels, f"{rel} is not one of the binary's {c_rels}"
