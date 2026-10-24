# SPDX-License-Identifier: Apache-2.0
"""``make install`` ships the configured decode mode, or refuses, and under the
native mode ships no Python bridge.

Without a guard, what an install ships is a consequence, not a statement:
the top-level install builds ``capture_cell_diag`` first, with the install's
OWN ``NATIVE=`` (a tree left native is relinked to the default before the
copy), so it ships whatever that invocation's ``NATIVE`` says: the default,
unless ``NATIVE`` is on the command line or exported. The default
is ``NATIVE=1`` and the install mode is ``native``, so a plain ``make install``
links and ships the native build, and an install refuses a baseline link.

The contract has four parts, and each has tests here:

* ``celldiag_binary_mode.sh`` reads the mode from the BINARY's symbol table
  (``kaitai::kstream`` is linked iff the native legs are), not from
  ``.native_mode``;
* ``CELLDIAG_INSTALL_MODE`` is defined once, in ``Makefile.in`` beside
  ``NATIVE``'s default, and ``standalone.mk`` reads it from there. Only a
  command-line value overrides it, so an exported variable cannot change what an
  install ships;
* ``install-decoder``, the hook the top-level install runs, runs
  ``install-mode-guard`` first, which refuses a binary in the other mode, names
  the fix, and installs nothing;
* under the ``native`` install mode ``install-decoder`` is a no-op for the
  bridge: it ships neither ``kismet_diag_decode.py`` nor ``.deps/diaggrok``
  (the bridge is the dev/test oracle), and it names, without deleting,
  a bridge an earlier install left behind. An explicit
  ``CELLDIAG_INSTALL_MODE=baseline`` keeps the old decoder install.

The guard is driven with small FAKE binaries of a known mode
(``CELLDIAG_INSTALL_BIN=``), because the tree's own binary is in whichever mode
the last build on this shared clone left it. The detector is also held to the
tree's real binaries wherever their mode is known from their names.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

TREE = Path(__file__).resolve().parents[2]
CELLDIAG = TREE / "capture_cell_diag"
DETECTOR = CELLDIAG / "celldiag_binary_mode.sh"
MAKEFILE_IN = CELLDIAG / "Makefile.in"
STANDALONE = CELLDIAG / "standalone.mk"

pytestmark = pytest.mark.skipif(
    shutil.which("make") is None or shutil.which("cc") is None
    or shutil.which("c++") is None or shutil.which("nm") is None,
    reason="needs make, cc, c++ and nm")

_BASELINE_SRC = """
int diag_native_decode(void) { return 0; }
int main(void) { return diag_native_decode(); }
"""
# The two things the detector reads: the extern "C" surface both modes define,
# and a DEFINED kaitai::kstream member, which only a native link has.
_NATIVE_SRC = """
namespace kaitai {
class kstream { public: explicit kstream(int v); int v_; };
kstream::kstream(int v) : v_(v) {}
}
extern "C" int diag_native_decode(void) { kaitai::kstream k(0); return k.v_; }
int main() { return diag_native_decode(); }
"""
_OTHER_SRC = "int main(void) { return 0; }\n"


@pytest.fixture(scope="module")
def fakes(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("fake_celldiag")
    out: dict[str, Path] = {}
    for name, src, cc in (("baseline", _BASELINE_SRC, "cc"),
                          ("native", _NATIVE_SRC, "c++"),
                          ("other", _OTHER_SRC, "cc")):
        ext = ".cpp" if cc == "c++" else ".c"
        (d / f"{name}{ext}").write_text(src)
        r = subprocess.run([cc, "-O0", "-o", str(d / name), str(d / f"{name}{ext}")],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        out[name] = d / name
    stripped = d / "stripped"
    shutil.copy2(out["baseline"], stripped)
    if shutil.which("strip") is not None:
        subprocess.run(["strip", str(stripped)], check=True)
        out["stripped"] = stripped
    return out


def _detect(binary: Path | str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(DETECTOR), str(binary), *extra],
                          capture_output=True, text=True, timeout=60)


def _clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("MAKEFLAGS", "MAKELEVEL", "MFLAGS", "CELLDIAG_INSTALL_MODE")}
    env.update(extra)
    return env


def _make(target: str, *, cwd: Path = CELLDIAG, bin: Path | None = None,
          mode: str | None = None, share: Path | None = None,
          decoder: tuple[Path, str] | None = None,
          env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    args = ["make", "--no-print-directory", "-C", str(cwd), "-f", "standalone.mk", target]
    if bin is not None:
        args.append(f"CELLDIAG_INSTALL_BIN={bin}")
    if mode is not None:
        args.append(f"CELLDIAG_INSTALL_MODE={mode}")
    if share is not None:
        args.append(f"CELL_SHARE={share}")
        if decoder is None:
            # No decoder: the payload then installs the bridge alone and says
            # why, which keeps these tests off the network and off .deps/.
            args.append(f"DIAGGROK_DIR={share.parent / 'no-decoder'}")
        else:
            # A fetched, PINNED decoder: the directory and the SHA it is at.
            args += [f"DIAGGROK_DIR={decoder[0]}", f"DIAGGROK_SHA={decoder[1]}"]
    return subprocess.run(args, capture_output=True, text=True, timeout=120,
                          env=env if env is not None else _clean_env())


@pytest.fixture
def pinned_decoder(tmp_path) -> tuple[Path, str]:
    """A local stand-in for a `deps` fetch: a git checkout with a src/diaggrok
    package, and its HEAD as the pin. With it, a baseline install has a decoder
    to ship, so a native install shipping NOTHING is a decision, not an
    absence."""
    if shutil.which("git") is None:
        pytest.skip("needs git")
    d = tmp_path / "fetched-diaggrok"
    (d / "src" / "diaggrok").mkdir(parents=True)
    (d / "src" / "diaggrok" / "__init__.py").write_text("# stand-in decoder\n")
    git = ["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t",
           "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
    for argv in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "stand-in"]):
        r = subprocess.run(git + argv, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    sha = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True,
                         text=True, check=True).stdout.strip()
    return d, sha


def _bridge_files(share: Path) -> list[str]:
    """What an install put under the share dir that belongs to the bridge."""
    if not share.exists():
        return []
    return sorted(str(p.relative_to(share)) for p in share.rglob("*")
                  if p.name == "kismet_diag_decode.py" or ".deps" in p.parts)


# ── the detector ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["baseline", "native"])
def test_the_detector_reads_each_mode(fakes, name):
    r = _detect(fakes[name])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == name


@pytest.mark.parametrize("name,why", [
    ("other", "not a kismet_cap_cell_diag"),
    ("stripped", "no symbol table"),
])
def test_the_detector_says_unknown_rather_than_guessing(fakes, name, why):
    """A binary that is not a celldiag helper, or a stripped copy, is UNKNOWN.
    Guessing `baseline` for "no C++ seen" would wave through exactly the copy
    whose mode nobody can vouch for."""
    if name not in fakes:
        pytest.skip("no strip(1) on this host")
    r = _detect(fakes[name])
    assert r.returncode == 1
    assert r.stdout == ""
    assert r.stderr.startswith("unknown:") and why in r.stderr, r.stderr


def test_the_detector_says_unknown_for_a_missing_file(tmp_path):
    r = _detect(tmp_path / "absent")
    assert r.returncode == 1 and "no file" in r.stderr


def test_the_detector_rejects_bad_usage(fakes):
    assert subprocess.run(["sh", str(DETECTOR)], capture_output=True).returncode == 64
    assert _detect(fakes["baseline"], "extra").returncode == 64


def _known_real_binaries() -> list[tuple[Path, str]]:
    """The tree's real binaries whose mode is known from where they live."""
    cands = [
        (CELLDIAG / "build-replay-gate" / "kismet_cap_cell_diag.baseline", "baseline"),
        (CELLDIAG / "build-replay-gate" / "kismet_cap_cell_diag.native", "native"),
        (CELLDIAG / "build-baseline" / "kismet_cap_cell_diag", "baseline"),
        (CELLDIAG / "build-native" / "kismet_cap_cell_diag", "native"),
    ]
    return [(p, m) for p, m in cands if p.is_file()]


def test_the_detector_agrees_with_the_trees_real_binaries():
    """The fakes prove the rule; this proves the rule matches what the real
    links contain. build-baseline/ is only ever the baseline target's output
    and build-native/ only check-native's, so their names are their modes."""
    known = _known_real_binaries()
    if not known:
        pytest.skip("no built celldiag binary of a known mode in this tree")
    for path, mode in known:
        r = _detect(path)
        assert r.returncode == 0 and r.stdout.strip() == mode, (path, r.stdout, r.stderr)


# ── the configured mode: one definition, read, not copied ────────────────────

def _assignments(text: str, var: str) -> list[str]:
    return re.findall(rf"^{var}[ \t]*[:?]?=[ \t]*(.*?)[ \t]*$", text, re.M)


def _builds(native_value: str) -> str:
    """What a plain `make` builds for a given NATIVE default: empty or 0 is
    the dev-only baseline, anything else the native build (Makefile.in's
    NATIVE_BUILD rule)."""
    return "baseline" if native_value.strip() in ("", "0") else "native"


def test_the_install_mode_is_defined_once_and_tracks_natives_default():
    """A change of default moves NATIVE's default AND the install mode.
    Flipping one without the other ships a build the default refuses, which is
    the very mismatch the guard exists for, so the two are held to each other
    here."""
    text = MAKEFILE_IN.read_text()
    modes = _assignments(text, "CELLDIAG_INSTALL_MODE")
    assert len(modes) == 1, f"CELLDIAG_INSTALL_MODE must be defined exactly once: {modes}"
    natives = _assignments(text, "NATIVE")
    assert len(natives) == 1, natives
    default = _builds(natives[0])
    assert modes[0] == default, (
        f"Makefile.in installs {modes[0]!r} but a plain `make` builds {default!r}")


def test_the_default_build_and_install_are_native():
    """The default itself: a plain `make` builds NATIVE=1 and a
    plain `make install` ships it. The test above holds the two lines to each
    other; this one holds them to the decision."""
    text = MAKEFILE_IN.read_text()
    assert _assignments(text, "NATIVE") == ["1"], _assignments(text, "NATIVE")
    assert _assignments(text, "CELLDIAG_INSTALL_MODE") == ["native"]


def test_standalone_mk_reads_the_mode_rather_than_defining_it():
    """A literal here would be the second definition the flip forgets."""
    for value in _assignments(STANDALONE.read_text(), "CELLDIAG_INSTALL_MODE"):
        assert "Makefile.in" in value, f"standalone.mk sets the install mode itself: {value!r}"


@pytest.fixture
def fake_tree(tmp_path):
    """A capture_cell_diag with this tree's standalone.mk and detector, and a
    Makefile.in that configures the BASELINE as the install mode -- the other
    side of the default, which the shared tree is not in."""
    d = tmp_path / "capture_cell_diag"
    d.mkdir()
    for f in ("standalone.mk", "baseline_platform.mk", "celldiag_binary_mode.sh",
              "kismet_diag_decode.py"):
        shutil.copy2(CELLDIAG / f, d / f)
    (d / "Makefile.in").write_text("NATIVE ?= 0\nCELLDIAG_INSTALL_MODE = baseline\n")
    return d


def test_the_guard_takes_the_mode_from_makefile_in(fake_tree, fakes):
    """Change Makefile.in alone, and the guard follows it with no edit to
    standalone.mk: here a baseline link passes and a native link is refused."""
    ok = _make("install-mode-guard", cwd=fake_tree, bin=fakes["baseline"])
    assert ok.returncode == 0, ok.stderr
    assert "install mode baseline" in ok.stdout
    bad = _make("install-mode-guard", cwd=fake_tree, bin=fakes["native"])
    assert bad.returncode != 0
    assert "ships the baseline build" in bad.stderr, bad.stderr
    assert _fix_lines(bad.stderr)[0] == "make install NATIVE=0", bad.stderr


def test_an_unreadable_makefile_in_is_refused(fake_tree, fakes):
    """No configured mode is not a mode: refusing beats shipping by default."""
    (fake_tree / "Makefile.in").unlink()
    r = _make("install-mode-guard", cwd=fake_tree, bin=fakes["baseline"])
    assert r.returncode != 0
    assert "neither baseline nor native" in r.stderr, r.stderr


# ── the guard ────────────────────────────────────────────────────────────────

def _fix_lines(err: str) -> list[str]:
    """The indented command lines of a refusal."""
    return [ln.strip("* ").strip() for ln in err.splitlines() if ln.startswith("***       ")]


def test_a_baseline_link_is_refused_by_the_default_install(fakes, tmp_path):
    """The native default's guard: a plain `make install` is for native,
    and it found a baseline link (`make install NATIVE=0`, or an exported
    NATIVE=0). The install relinks with its own NATIVE=, so the fix is the
    install's NATIVE=, not a relink. The mode came from Makefile.in, so the fix
    does not restate it."""
    share = tmp_path / "share"
    r = _make("install-decoder", bin=fakes["baseline"], share=share)
    assert r.returncode != 0
    err = r.stderr
    assert "celldiag install REFUSED" in err
    assert "ships the native build" in err and "is the baseline link" in err, err
    assert "ITS OWN NATIVE=" in err and "exported NATIVE" in err, err
    assert _fix_lines(err) == ["make install NATIVE=1",
                               "make install NATIVE=0 CELLDIAG_INSTALL_MODE=baseline"], err
    assert "BEFORE this check" in err, "the refusal hides that the binary is already copied"


def test_a_native_link_is_refused_by_an_explicit_baseline_install(fakes, tmp_path):
    """The dev-only install, asked for on the command line, found a native
    link. A mode given on the command line is repeated in the fix, because a
    plain `make install` would not repeat it."""
    r = _make("install-decoder", bin=fakes["native"], mode="baseline",
              share=tmp_path / "share")
    assert r.returncode != 0
    assert "ships the baseline build" in r.stderr and "is the native link" in r.stderr
    assert _fix_lines(r.stderr) == [
        "make install NATIVE=0 CELLDIAG_INSTALL_MODE=baseline",
        "make install NATIVE=1 CELLDIAG_INSTALL_MODE=native"], r.stderr


def test_a_baseline_link_is_refused_by_an_explicit_native_install(fakes, tmp_path):
    r = _make("install-decoder", bin=fakes["baseline"], mode="native",
              share=tmp_path / "share")
    assert r.returncode != 0
    assert "ships the native build" in r.stderr and "is the baseline link" in r.stderr
    assert _fix_lines(r.stderr) == [
        "make install NATIVE=1 CELLDIAG_INSTALL_MODE=native",
        "make install NATIVE=0 CELLDIAG_INSTALL_MODE=baseline"], r.stderr


# ── what each install mode ships ─────────────────────────────────────────────

@pytest.mark.parametrize("mode", [None, "native"])
def test_a_native_install_ships_no_bridge_and_no_decoder(fakes, tmp_path, pinned_decoder, mode):
    """The default install: the guard runs, and passes on the native link, and
    then NOTHING of the bridge is installed -- not kismet_diag_decode.py, not
    .deps/diaggrok -- even though a pinned decoder IS fetched and a baseline
    install (below) would ship it. The bridge is the dev/test oracle."""
    share = tmp_path / "share"
    r = _make("install-decoder", bin=fakes["native"], mode=mode, share=share,
              decoder=pinned_decoder)
    assert r.returncode == 0, r.stderr
    assert "install mode native" in r.stdout, r.stdout
    assert "no Python bridge" in r.stdout, r.stdout
    assert _bridge_files(share) == [], (
        f"a native install shipped the bridge: {_bridge_files(share)}")


def test_an_explicit_baseline_install_still_ships_the_pinned_decoder(
        fakes, tmp_path, pinned_decoder):
    """CELLDIAG_INSTALL_MODE=baseline keeps today's decoder install: the bridge,
    and the fetched decoder at its pin."""
    share = tmp_path / "share"
    r = _make("install-decoder", bin=fakes["baseline"], mode="baseline", share=share,
              decoder=pinned_decoder)
    assert r.returncode == 0, r.stderr
    assert "install mode baseline" in r.stdout
    assert (share / "kismet_diag_decode.py").is_file()
    assert (share / ".deps" / "diaggrok" / "src" / "diaggrok" / "__init__.py").is_file()
    assert (share / ".deps" / "diaggrok" / "PINNED_SHA").read_text().strip() == pinned_decoder[1]


def test_a_native_install_names_a_bridge_left_by_an_earlier_install(fakes, tmp_path):
    """An earlier (baseline) install left the bridge in the share dir. A native
    install neither ships nor DELETES it -- removing files from a system dir is
    the operator's call -- but says it is there, and that only an explicit
    bridge mode would ever run it."""
    share = tmp_path / "share"
    (share / ".deps" / "diaggrok" / "src" / "diaggrok").mkdir(parents=True)
    (share / ".deps" / "diaggrok" / "PINNED_SHA").write_text("0" * 40 + "\n")
    (share / "kismet_diag_decode.py").write_text("# an old bridge\n")
    before = _bridge_files(share)
    r = _make("install-decoder", bin=fakes["native"], share=share)
    assert r.returncode == 0, r.stderr
    assert _bridge_files(share) == before, "the install changed the old bridge"
    out = r.stdout + r.stderr
    assert str(share / "kismet_diag_decode.py") in out, out
    assert str(share / ".deps" / "diaggrok") in out, out
    assert "nativedecode=off" in out and "not removed" in out, out


def test_a_refused_install_installs_nothing(fakes, tmp_path):
    """The guard is a prerequisite, so the payload never starts after a refusal."""
    share = tmp_path / "share"
    r = _make("install-decoder", bin=fakes["native"], mode="baseline", share=share)
    assert r.returncode != 0
    assert not share.exists(), sorted(p.name for p in share.rglob("*"))


def test_an_exported_mode_does_not_change_what_ships(fakes, tmp_path):
    """A stray `export CELLDIAG_INSTALL_MODE=baseline` must not turn a refusal
    into a pass: only the command line states an install's mode."""
    r = _make("install-decoder", bin=fakes["baseline"], share=tmp_path / "share",
              env=_clean_env(CELLDIAG_INSTALL_MODE="baseline"))
    assert r.returncode != 0
    assert "ships the native build" in r.stderr, r.stderr


def test_a_mode_given_to_the_parent_make_reaches_the_guard(fakes, tmp_path):
    """The top-level install reaches this hook through a recursive $(MAKE), so an
    override typed on ITS command line arrives here through MAKEFLAGS. A
    stand-in parent shows both directions."""
    share = tmp_path / "share"
    parent = tmp_path / "parent.mk"
    parent.write_text(
        "install:\n"
        f"\t$(MAKE) --no-print-directory -C {CELLDIAG} -f standalone.mk install-decoder "
        f"CELL_SHARE={share} DIAGGROK_DIR={tmp_path / 'no-decoder'} "
        f"CELLDIAG_INSTALL_BIN={fakes['baseline']}\n")
    run = ["make", "--no-print-directory", "-f", str(parent), "install"]
    refused = subprocess.run(run, capture_output=True, text=True, env=_clean_env())
    assert refused.returncode != 0 and "REFUSED" in refused.stderr
    passed = subprocess.run(run + ["CELLDIAG_INSTALL_MODE=baseline"],
                            capture_output=True, text=True, env=_clean_env())
    assert passed.returncode == 0, passed.stderr
    assert (share / "kismet_diag_decode.py").is_file()


@pytest.mark.parametrize("which", ["missing", "other", "stripped"])
def test_a_binary_whose_mode_cannot_be_read_is_refused(fakes, tmp_path, which):
    if which == "stripped" and "stripped" not in fakes:
        pytest.skip("no strip(1) on this host")
    binary = tmp_path / "absent" if which == "missing" else fakes[which]
    r = _make("install-decoder", bin=binary, share=tmp_path / "share")
    assert r.returncode != 0
    assert "cannot be read" in r.stderr and "unknown:" in r.stderr, r.stderr
    # No "ship it on purpose" advice: there is no known mode to name.
    assert _fix_lines(r.stderr) == ["make install NATIVE=1"], r.stderr


def test_a_mode_that_is_neither_is_refused(fakes, tmp_path):
    r = _make("install-decoder", bin=fakes["baseline"], mode="shipping",
              share=tmp_path / "share")
    assert r.returncode != 0
    assert "neither baseline nor native" in r.stderr, r.stderr


def test_the_payload_alone_is_unguarded(fakes, tmp_path):
    """install-decoder-files is what the installed-layout tests drive, so their
    verdict cannot depend on this shared tree's link mode. The install runs
    install-decoder, which is guarded (above) and, under the native mode, never
    reaches this payload."""
    share = tmp_path / "share"
    r = _make("install-decoder-files", bin=fakes["native"], share=share)
    assert r.returncode == 0, r.stderr
    assert (share / "kismet_diag_decode.py").is_file()
