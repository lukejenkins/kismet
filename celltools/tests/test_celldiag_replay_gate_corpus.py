# SPDX-License-Identifier: Apache-2.0
"""``make -C celltools replay-gate`` must not pass without the replay corpus.

The target exists for one measurement: the two-build replay A/B in
``test_celldiag_replay_ab.py``. Its fixtures are captures kept outside any source
tree, found through ``CELL_CAPTURES``. A gate that sets the two binary paths but
not the corpus skips nearly every A/B test on a host that has not exported the
variable and still exits 0 -- a green that measured almost nothing, behind which
real replay regressions can hide.

What is pinned here:

* with ``CELL_CAPTURES`` unset, or naming no directory, the gate **fails**, names
  the variable, and fails **before** building anything -- two relinks are not the
  price of learning the gate could never have run;
* with it set, the suite is started with ``CELL_CAPTURES_REQUIRED=1``, so a
  single missing fixture is a failed session, not a quiet ``s`` (the
  ``corpus_skip_banner`` plugin's strict mode);
* ``CELL_CAPTURES=`` given as a **make variable** reaches the suite, as well as
  one from the environment.

The build half runs against the same stub tree as
``test_celldiag_replay_gate_nonmutating.py``, and ``PYTHON`` is a stub that
records the environment and arguments it was started with -- so this runs with no
compiler, no configured tree and no corpus.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_celldiag_replay_gate_nonmutating import STUB_MAKEFILE

CELLTOOLS = Path(__file__).resolve().parent.parent
MAKE = shutil.which("make")

pytestmark = pytest.mark.skipif(MAKE is None, reason="no make(1) on this host")


@pytest.fixture()
def stub_tree(tmp_path: Path) -> Path:
    d = tmp_path / "capture_cell_diag"
    d.mkdir()
    (d / "Makefile").write_text(STUB_MAKEFILE)
    return d


@pytest.fixture()
def stub_python(tmp_path: Path) -> tuple[Path, Path]:
    """A PYTHON that records what the gate handed the suite, then exits 0."""
    out = tmp_path / "suite_saw"
    out.mkdir()
    py = tmp_path / "fake_python"
    py.write_text("#!/bin/sh\n"
                  f"env > '{out}/env'\n"
                  f"printf '%s\\n' \"$@\" > '{out}/args'\n")
    py.chmod(0o755)
    return py, out


def _env_without_corpus() -> dict:
    env = dict(os.environ)
    for k in ("CELL_CAPTURES", "CELL_CAPTURES_REQUIRED", "MAKEFLAGS", "MFLAGS"):
        env.pop(k, None)
    return env


def _gate(stub_tree: Path, gate: Path, python: Path, *extra: str,
          env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [MAKE, "-C", str(CELLTOOLS), "replay-gate",
         f"CELLDIAG_DIR={stub_tree}", f"GATE_DIR={gate}", f"PYTHON={python}", *extra],
        capture_output=True, text=True, timeout=120,
        env=env if env is not None else _env_without_corpus(),
    )


def _suite_env(out: Path) -> dict:
    env = {}
    for line in (out / "env").read_text().splitlines():
        k, _, v = line.partition("=")
        env[k] = v
    return env


# ── no corpus: refuse, and refuse before building ─────────────────────────────
def test_the_gate_fails_when_cell_captures_is_unset(stub_tree, tmp_path, stub_python):
    py, out = stub_python
    gate = tmp_path / "gate"
    proc = _gate(stub_tree, gate, py)

    assert proc.returncode != 0, f"gate passed with no corpus:\n{proc.stdout}"
    assert "CELL_CAPTURES" in proc.stderr
    assert not (out / "args").exists(), "the suite ran although the gate cannot measure"


@pytest.mark.parametrize("jobs", [(), ("-j4",)], ids=["serial", "j4"])
def test_the_refusal_comes_before_either_build(stub_tree, tmp_path, stub_python, jobs):
    """Two relinks are not the price of learning the gate could never have run.

    Under -j, a prerequisite list alone would start the build BESIDE the check;
    .NOTPARALLEL is what keeps the refusal first, so it is measured both ways."""
    py, _ = stub_python
    gate = tmp_path / "gate"
    proc = _gate(stub_tree, gate, py, *jobs)
    assert proc.returncode != 0

    assert not (gate / "kismet_cap_cell_diag.native").exists()
    assert not (gate / "kismet_cap_cell_diag.baseline").exists()
    assert not (stub_tree / ".native_mode").exists()  # the stub was never built


def test_a_cell_captures_that_is_not_a_directory_fails(stub_tree, tmp_path, stub_python):
    py, out = stub_python
    proc = _gate(stub_tree, tmp_path / "gate", py,
                 f"CELL_CAPTURES={tmp_path / 'no-such-corpus'}")

    assert proc.returncode != 0
    assert "no-such-corpus" in proc.stderr
    assert not (out / "args").exists()


# ── corpus present: run the suite, strictly ──────────────────────────────────
def test_the_suite_runs_strict_so_a_missing_fixture_fails(stub_tree, tmp_path, stub_python):
    py, out = stub_python
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    env = _env_without_corpus()
    env["CELL_CAPTURES"] = str(corpus)
    proc = _gate(stub_tree, tmp_path / "gate", py, env=env)

    assert proc.returncode == 0, proc.stderr
    saw = _suite_env(out)
    assert saw.get("CELL_CAPTURES_REQUIRED") == "1"
    assert saw.get("CELL_CAPTURES") == str(corpus)
    assert "test_celldiag_replay_ab.py" in (out / "args").read_text()


def test_cell_captures_as_a_make_variable_reaches_the_suite(stub_tree, tmp_path, stub_python):
    py, out = stub_python
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    proc = _gate(stub_tree, tmp_path / "gate", py, f"CELL_CAPTURES={corpus}")

    assert proc.returncode == 0, proc.stderr
    saw = _suite_env(out)
    assert saw.get("CELL_CAPTURES") == str(corpus)
    assert saw.get("CELL_CAPTURES_REQUIRED") == "1"
    # the gate still hands the suite both builds, not one build twice
    assert saw["CELLDIAG_BASELINE_BIN"].endswith("kismet_cap_cell_diag.baseline")
    assert saw["CELLDIAG_NATIVE_BIN"].endswith("kismet_cap_cell_diag.native")


# ── the shadow tests must use the gate's NATIVE build, not the tree's ─────────
# The nativedecode=shadow tests must not find the in-tree binary via discovery:
# the gate restores the tree to its entry mode by design, so from a baseline tree
# they would always skip, although the gate has just built a NATIVE=1 copy and
# exported it as CELLDIAG_NATIVE_BIN. Their skip reason names no CELL_CAPTURES,
# so strict mode cannot see it. Both _baseline_binary_or_skip and
# _native_binary_or_skip prefer their gate variable.
import test_celldiag_replay_ab as ab  # noqa: E402  (module object: no test re-collection)


class _OracleOk:
    ok = True


@pytest.fixture()
def gate_helpers(monkeypatch):
    """Neutralise the checks that need a real binary; keep the selection logic."""
    monkeypatch.setattr(ab, "ORACLE", _OracleOk())
    monkeypatch.setattr(ab, "_fail_if_binary_is_stale", lambda p: None)

    def _no_discovery():
        raise AssertionError("discovered the in-tree binary although the gate named one")
    monkeypatch.setattr(ab, "_binary_or_skip", _no_discovery)
    return monkeypatch


def test_shadow_tests_take_the_gate_native_build(gate_helpers, tmp_path):
    native = tmp_path / "kismet_cap_cell_diag.native"
    native.write_text("stub")
    gate_helpers.setenv("CELLDIAG_NATIVE_BIN", str(native))
    gate_helpers.setattr(ab, "_defines_native_legs", lambda b: b == str(native))

    assert ab._native_binary_or_skip() == str(native)


def test_a_gate_native_build_without_legs_fails_not_skips(gate_helpers, tmp_path):
    """Named as the native build by the gate and not one: that is a broken gate,
    and a skip here would be exactly the quiet green this module guards against."""
    wrong = tmp_path / "kismet_cap_cell_diag.native"
    wrong.write_text("stub")
    gate_helpers.setenv("CELLDIAG_NATIVE_BIN", str(wrong))
    gate_helpers.setattr(ab, "_defines_native_legs", lambda b: False)

    # Not pytest.raises(pytest.fail.Exception): a helper that SKIPS raises
    # Skipped, which escapes that context manager and turns this very test into
    # a quiet skip, letting a mutation survive.
    outcome = None
    try:
        ab._native_binary_or_skip()
    except BaseException as e:  # pytest's outcomes are BaseException subclasses
        outcome = e
    assert isinstance(outcome, pytest.fail.Exception), (
        f"expected a failure, got {type(outcome).__name__}: {outcome}")
    assert "CELLDIAG_NATIVE_BIN" in str(outcome)


def test_without_the_gate_the_tree_binary_is_still_used(monkeypatch, tmp_path):
    """Outside the gate nothing changes: an ordinary NATIVE=1 tree still runs them."""
    tree = tmp_path / "kismet_cap_cell_diag"
    monkeypatch.delenv("CELLDIAG_NATIVE_BIN", raising=False)
    monkeypatch.setattr(ab, "_binary_or_skip", lambda: str(tree))
    monkeypatch.setattr(ab, "_defines_native_legs", lambda b: True)

    assert ab._native_binary_or_skip() == str(tree)
