# SPDX-License-Identifier: Apache-2.0
"""``make -C celltools replay-gate`` must not mutate the tree it measures.

The gate builds BOTH helper configurations, and the two cannot coexist: ``NATIVE=``
is a make *variable*, so switching it relinks the same output path. The target
therefore has to build twice, and whichever build it does last is the one the
operator is left holding.

Always building the baseline second does not avoid the surprise; it only fixes
its direction. A tree built ``NATIVE=1`` and gated would come back **baseline**,
and every later test keyed on the native build (the staleness guard,
``_native_binary_or_skip``) would fail with nothing wrong in the code under test.
A measurement target that mutates its measurement subject reports on a tree that
no longer exists, so the gate restores the mode the tree entered in.

What these tests pin, and why they are synthetic:

* the entry mode is **restored**, for all three starting states -- ``native``,
  ``baseline``, and ``unbuilt``;
* both gate copies are still produced, and still come from the two different
  builds (a restore that ran too early would leave both copies identical);
* an ``unbuilt`` tree comes back with **no binary**, rather than with a baseline
  one. This is the sharp case: "leave it in the default configuration" hands a
  build to guards that were correctly *skipping* for want of one, which turns a
  skip into a pass silently.

``CELLDIAG_DIR`` is pointed at a **stub** tree whose Makefile reproduces exactly
the two behaviours the gate depends on -- ``.native_mode`` records the mode, and
the binary is rewritten on every build -- and nothing else. So this runs on a
host with no compiler and no configured tree, following
``test_binary_staleness.py``'s precedent. A stub is also what makes ``unbuilt`` a
state a test may legally *create*, which it is not on the real tree.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

CELLTOOLS = Path(__file__).resolve().parent.parent
MAKE = shutil.which("make")

pytestmark = pytest.mark.skipif(MAKE is None, reason="no make(1) on this host")

#: A stand-in for capture_cell_diag/Makefile carrying only what the gate touches:
#: the .native_mode stamp and a binary whose CONTENT differs per mode, so a copy
#: taken from the wrong build is detectable rather than merely suspected. Its
#: NATIVE reading is Makefile.in's: native
#: by default, and NATIVE=0 (or an empty NATIVE=) for the baseline.
STUB_MAKEFILE = """\
NATIVE ?= 1
ifeq ($(filter-out 0,$(strip $(NATIVE))),)
NATIVE_MODE = baseline
else
NATIVE_MODE = native
endif

all:
\techo $(NATIVE_MODE) > .native_mode
\tprintf 'stub-%s-build\\n' '$(NATIVE_MODE)' > kismet_cap_cell_diag
\tchmod +x kismet_cap_cell_diag
.PHONY: all
"""


@pytest.fixture()
def stub_tree(tmp_path: Path) -> Path:
    """An unbuilt stub CELLDIAG_DIR: a Makefile, no binary, no .native_mode."""
    d = tmp_path / "capture_cell_diag"
    d.mkdir()
    (d / "Makefile").write_text(STUB_MAKEFILE)
    return d


def _run_gate(stub_tree: Path, gate_dir: Path) -> subprocess.CompletedProcess:
    """Drive the build half of the gate. Not `replay-gate`: the A/B suite needs a
    real binary and a real capture, and neither is what this test is about."""
    proc = subprocess.run(
        [MAKE, "-C", str(CELLTOOLS), "replay-gate-build",
         f"CELLDIAG_DIR={stub_tree}", f"GATE_DIR={gate_dir}"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"replay-gate-build failed:\n{proc.stdout}\n{proc.stderr}"
    return proc


def _build(stub_tree: Path, native: bool) -> None:
    args = [MAKE, "-C", str(stub_tree), "NATIVE=1" if native else "NATIVE=0"]
    subprocess.run(args, capture_output=True, text=True, check=True, timeout=60)


def test_the_pair_builds_native_with_the_default_and_baseline_with_NATIVE_0(
        stub_tree: Path, tmp_path: Path):
    """The pair's native copy is the DEFAULT build -- no NATIVE= named -- so
    what the harness measures as native is what a plain `make` ships; the
    baseline is the explicit NATIVE=0. A stub whose default is the baseline
    therefore yields two baseline copies, which the replay harness's polarity
    check refuses (test_baseline_and_native_builds_decode_a_capture_identically)."""
    flipped_back = stub_tree.parent / "flipped_back"
    flipped_back.mkdir()
    (flipped_back / "Makefile").write_text(
        STUB_MAKEFILE.replace("NATIVE ?= 1", "NATIVE ?= 0"))
    for tree, want_native in ((stub_tree, "stub-native-build"),
                              (flipped_back, "stub-baseline-build")):
        gate = tmp_path / f"gate-{tree.name}"
        _run_gate(tree, gate)
        assert (gate / "kismet_cap_cell_diag.native").read_text().strip() == want_native
        assert (gate / "kismet_cap_cell_diag.baseline").read_text().strip() == \
            "stub-baseline-build"


def _mode(stub_tree: Path) -> str | None:
    f = stub_tree / ".native_mode"
    return f.read_text().strip() if f.exists() else None


# ── the restore, for each starting state ──────────────────────────────────────
@pytest.mark.parametrize("native, expected", [(True, "native"), (False, "baseline")])
def test_gate_restores_the_mode_the_tree_entered_in(
    stub_tree: Path, tmp_path: Path, native: bool, expected: str
):
    _build(stub_tree, native=native)
    assert _mode(stub_tree) == expected  # the fixture is what the test says it is

    _run_gate(stub_tree, tmp_path / "gate")

    assert _mode(stub_tree) == expected
    # The stamp is a claim about the binary; check the binary agrees. A restore
    # that rewrote .native_mode without relinking would pass on the stamp alone.
    assert (stub_tree / "kismet_cap_cell_diag").read_text().strip() == f"stub-{expected}-build"


def test_an_unbuilt_tree_comes_back_unbuilt(stub_tree: Path, tmp_path: Path):
    """The unbuilt starting state.

    "Left in its DEFAULT configuration" is a mutation when the default is *no
    build at all*. `_native_binary_or_skip` and the staleness guard both key on
    the binary's existence, so a gate run would convert their skips into runs
    against an artifact the operator never asked for.
    """
    assert _mode(stub_tree) is None
    assert not (stub_tree / "kismet_cap_cell_diag").exists()

    _run_gate(stub_tree, tmp_path / "gate")

    assert _mode(stub_tree) is None
    assert not (stub_tree / "kismet_cap_cell_diag").exists()


# ── the restore must not cost the gate its outputs ────────────────────────────
def test_both_gate_copies_survive_the_restore_and_differ(stub_tree: Path, tmp_path: Path):
    """`nm`-differ, at stub scale: the two copies must come from the two builds.

    Restoring *before* the copies are taken -- or taking both copies after the
    restore -- yields two byte-identical files and an A/B that compares one build
    to itself, which is the exact failure the GATE_DIR copy-out exists to prevent.
    """
    gate = tmp_path / "gate"
    _build(stub_tree, native=True)
    _run_gate(stub_tree, gate)

    native = gate / "kismet_cap_cell_diag.native"
    baseline = gate / "kismet_cap_cell_diag.baseline"
    assert native.read_text().strip() == "stub-native-build"
    assert baseline.read_text().strip() == "stub-baseline-build"


def test_the_entry_mode_is_recorded_where_a_later_restore_can_find_it(
    stub_tree: Path, tmp_path: Path
):
    """An interrupted gate must be recoverable by hand.

    Each recipe line is its own shell, so the entry mode cannot live in a shell
    variable; it is a file under GATE_DIR. That makes `make replay-gate-restore`
    a usable recovery command after a Ctrl-C, which is worth pinning because the
    file looks like an implementation detail and is not.
    """
    gate = tmp_path / "gate"
    _build(stub_tree, native=True)
    _run_gate(stub_tree, gate)
    assert (gate / ".entry_mode").read_text().strip() == "native"

    # Simulate the post-interrupt state: tree in the wrong mode, entry file intact.
    _build(stub_tree, native=False)
    assert _mode(stub_tree) == "baseline"

    subprocess.run(
        [MAKE, "-C", str(CELLTOOLS), "replay-gate-restore",
         f"CELLDIAG_DIR={stub_tree}", f"GATE_DIR={gate}"],
        capture_output=True, text=True, check=True, timeout=60,
    )
    assert _mode(stub_tree) == "native"


def test_an_unrecognised_entry_mode_fails_rather_than_guessing(
    stub_tree: Path, tmp_path: Path
):
    """A corrupt entry file must not silently resolve to a preferred default --
    the same mutation this module guards against, one layer down."""
    gate = tmp_path / "gate"
    gate.mkdir()
    (gate / ".entry_mode").write_text("banana\n")
    _build(stub_tree, native=True)

    proc = subprocess.run(
        [MAKE, "-C", str(CELLTOOLS), "replay-gate-restore",
         f"CELLDIAG_DIR={stub_tree}", f"GATE_DIR={gate}"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0
    assert "banana" in proc.stderr
    assert _mode(stub_tree) == "native"  # untouched, not reset to a default


# ── failure paths: a broken pair build, an interrupted gate, a busy copy ─────
#: The stub, plus a switch: with ``fail-native`` present, a NATIVE=1 build
#: stamps the tree native and THEN fails, the half-switched state a broken
#: compile leaves behind.
FAILING_STUB_MAKEFILE = STUB_MAKEFILE.replace(
    "\tchmod +x kismet_cap_cell_diag\n",
    "\tchmod +x kismet_cap_cell_diag\n"
    "\t@if [ '$(NATIVE_MODE)' = native ] && [ -f fail-native ]; then "
    "echo 'stub: NATIVE=1 build failed' >&2; exit 1; fi\n",
)


def _gate(stub_tree: Path, gate_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [MAKE, "-C", str(CELLTOOLS), "replay-gate-build",
         f"CELLDIAG_DIR={stub_tree}", f"GATE_DIR={gate_dir}"],
        capture_output=True, text=True, timeout=120,
    )


def test_a_failed_pair_build_still_restores_the_entry_mode(stub_tree, tmp_path):
    """A broken NATIVE=1 build must not leave the tree in native mode."""
    (stub_tree / "Makefile").write_text(FAILING_STUB_MAKEFILE)
    _build(stub_tree, native=False)
    (stub_tree / "fail-native").touch()
    gate = tmp_path / "gate"

    proc = _gate(stub_tree, gate)

    assert proc.returncode != 0, "a failed pair build must fail the target"
    assert _mode(stub_tree) == "baseline", proc.stdout + proc.stderr
    assert (stub_tree / "kismet_cap_cell_diag").read_text().strip() == \
        "stub-baseline-build"
    assert not (gate / ".restore_pending").exists(), (
        "the restore succeeded, so nothing is pending")


def test_a_rerun_after_an_interrupted_gate_keeps_the_TRUE_entry_mode(
        stub_tree, tmp_path):
    """The rerun must not record the mode the interrupted gate left behind.

    Interrupted between its NATIVE=1 build and the restore, a gate leaves a
    baseline tree in native mode. A rerun that recorded "native" as the entry
    mode would then restore the tree to native, permanently.
    """
    _build(stub_tree, native=False)
    gate = tmp_path / "gate"
    gate.mkdir()
    # Exactly what the interrupted run left: its record, its marker, and the
    # tree in the mode it had switched to.
    (gate / ".entry_mode").write_text("baseline\n")
    (gate / ".restore_pending").touch()
    _build(stub_tree, native=True)

    proc = _gate(stub_tree, gate)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (gate / ".entry_mode").read_text().strip() == "baseline"
    assert _mode(stub_tree) == "baseline"
    assert not (gate / ".restore_pending").exists()


def test_a_failed_restore_keeps_the_marker_for_the_next_gate(stub_tree, tmp_path):
    """Only a SUCCESSFUL restore may clear the marker."""
    _build(stub_tree, native=False)
    gate = tmp_path / "gate"
    gate.mkdir()
    (gate / ".entry_mode").write_text("garbage\n")
    (gate / ".restore_pending").touch()

    proc = subprocess.run(
        [MAKE, "-C", str(CELLTOOLS), "replay-gate-restore",
         f"CELLDIAG_DIR={stub_tree}", f"GATE_DIR={gate}"],
        capture_output=True, text=True, timeout=60)

    assert proc.returncode != 0
    assert (gate / ".restore_pending").exists()


#: The stub, building a real EXECUTABLE (a copy of sleep(1)), so a gate copy
#: can be run while the next gate replaces it. "Text file busy" is about an
#: executing image: a shell-script stub would not reproduce it.
_SLEEP = shutil.which("sleep")
EXEC_STUB_MAKEFILE = STUB_MAKEFILE.replace(
    "\tprintf 'stub-%s-build\\n' '$(NATIVE_MODE)' > kismet_cap_cell_diag\n",
    f"\tcp {_SLEEP} kismet_cap_cell_diag\n",
)


@pytest.mark.skipif(_SLEEP is None, reason="no sleep(1) on this host")
def test_the_gate_replaces_a_copy_that_is_EXECUTING(stub_tree, tmp_path):
    """A replay running the previous gate copy must not abort the next gate.

    An in-place copy fails with ETXTBSY on Linux while the old image is
    running. The gate renames a fresh copy over it instead.
    """
    (stub_tree / "Makefile").write_text(EXEC_STUB_MAKEFILE)
    _build(stub_tree, native=False)
    gate = tmp_path / "gate"
    first = _gate(stub_tree, gate)
    assert first.returncode == 0, first.stdout + first.stderr

    running = subprocess.Popen([str(gate / "kismet_cap_cell_diag.native"), "30"])
    try:
        again = _gate(stub_tree, gate)
    finally:
        running.kill()
        running.wait()

    assert again.returncode == 0, again.stdout + again.stderr
    assert _mode(stub_tree) == "baseline"
    assert not list(gate.glob(".*.tmp")), "a temporary copy was left behind"
