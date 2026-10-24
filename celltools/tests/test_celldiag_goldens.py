# SPDX-License-Identifier: Apache-2.0
"""The regeneration rules of celltools/celldiag_goldens.py.

A golden is only an oracle while it cannot quietly absorb a regression, so the
rules that decide when one may change are tested branch by branch here. They
need no binary and no capture: ``decide`` is pure, and the commit check runs
against a throwaway git repository.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import celldiag_goldens as cg  # noqa: E402

OLD_REV = "a" * 40
NEW_REV = "b" * 40


def _entry(golden="g1", capture="c1", rev=OLD_REV, rows=3):
    return cg.Entry("fx.hdlc", cg.STREAM, capture, golden, rows, rev)


def _never_called(*_a):
    raise AssertionError("the commit check must not run on this branch")


def _ok(*_a):
    return None


def test_a_new_fixture_needs_add():
    with pytest.raises(cg.Refused, match="--add"):
        cg.decide(None, _entry(), cg.Request(), _never_called)
    assert cg.decide(None, _entry(), cg.Request(add=True), _never_called) == "added"


def test_identical_content_is_a_silent_repin():
    assert cg.decide(_entry(), _entry(rev=NEW_REV), cg.Request(), _never_called) == ""


def test_changed_content_with_an_unmoved_decoder_is_refused_outright():
    """THE rule: the oracle did not move, so nothing may move the golden.
    Every flag is set, and it is still refused."""
    req = cg.Request(add=True, new_capture=True, moved_by="abc", reason="because")
    with pytest.raises(cg.Refused, match="regression"):
        cg.decide(_entry(), _entry(golden="g2"), req, _never_called)


def test_a_moved_decoder_needs_the_commit_and_a_reason():
    with pytest.raises(cg.Refused, match="--moved-by"):
        cg.decide(_entry(), _entry(golden="g2", rev=NEW_REV), cg.Request(), _never_called)
    with pytest.raises(cg.Refused, match="--moved-by"):
        cg.decide(_entry(), _entry(golden="g2", rev=NEW_REV),
                  cg.Request(moved_by="abc"), _never_called)
    assert cg.decide(_entry(), _entry(golden="g2", rev=NEW_REV),
                     cg.Request(moved_by="abc", reason="0xB193 v18"), _ok) == "moved"


def test_the_named_commit_must_actually_move_the_decoder():
    def not_in_range(sha, old, new):
        return "is not after the golden's revision"
    with pytest.raises(cg.Refused, match="not after"):
        cg.decide(_entry(), _entry(golden="g2", rev=NEW_REV),
                  cg.Request(moved_by="abc", reason="r"), not_in_range)


def test_a_replaced_capture_needs_its_own_flag_and_reason():
    with pytest.raises(cg.Refused, match="capture itself changed"):
        cg.decide(_entry(), _entry(capture="c2"), cg.Request(reason="r"), _never_called)
    with pytest.raises(cg.Refused, match="capture itself changed"):
        cg.decide(_entry(), _entry(capture="c2"), cg.Request(new_capture=True), _never_called)
    assert cg.decide(_entry(), _entry(capture="c2", golden="g2"),
                     cg.Request(new_capture=True, reason="re-cut"), _never_called) == "new capture"


def test_manifest_round_trips(tmp_path):
    m = tmp_path / "MANIFEST.tsv"
    e = {("fx.hdlc", cg.STREAM): _entry()}
    cg.write_manifest(e, m)
    assert cg.read_manifest(m) == e
    assert m.read_text().startswith("#")


# ── the commit check, against a real (throwaway) repository ────────────────────
def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def decoder_repo(tmp_path):
    """A repo whose ``dec/`` is the decoder tree, with one unrelated commit."""
    repo = tmp_path / "repo"
    (repo / "dec").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")

    def commit(path: str, text: str, msg: str) -> str:
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
        _git(repo, "add", path)
        _git(repo, "commit", "-qm", msg)
        return _git(repo, "rev-parse", "HEAD")

    revs = {
        "old": commit("dec/p.py", "v1\n", "decoder v1"),
        "unrelated": commit("other.txt", "x\n", "not the decoder"),
        "fix": commit("dec/p.py", "v2\n", "decoder v2"),
    }
    return repo, revs


def test_decoder_rev_is_the_last_commit_touching_the_decoder(decoder_repo):
    repo, revs = decoder_repo
    _git(repo, "commit", "--allow-empty", "-qm", "later, unrelated")
    assert cg.decoder_rev(repo / "dec") == revs["fix"]


def test_a_dirty_decoder_has_no_revision(decoder_repo):
    repo, _ = decoder_repo
    (repo / "dec" / "p.py").write_text("uncommitted\n")
    with pytest.raises(cg.Refused, match="uncommitted"):
        cg.decoder_rev(repo / "dec")


def test_commit_check_accepts_only_a_decoder_commit_in_range(decoder_repo):
    repo, revs = decoder_repo
    check = cg.commit_moves_decoder_in(repo / "dec")
    assert check(revs["fix"], revs["old"], revs["fix"]) is None
    assert "does not touch" in check(revs["unrelated"], revs["old"], revs["fix"])
    assert "not after" in check(revs["old"], revs["old"], revs["fix"])
    assert "not a commit" in check("deadbeef" * 5, revs["old"], revs["fix"])


# ── the no-Python check's skip/fail contract ───────────────────────────────────
CHECK = cg.CELLDIAG_DIR / "check_replay_goldens.sh"


@pytest.mark.skipif(not cg.MANIFEST.exists() or not cg.DRIVER.exists(),
                    reason="needs committed goldens and a built celldiag_replay_ndjson")
def test_the_check_skips_loudly_without_captures_and_fails_when_required(tmp_path):
    env = {"PATH": "/usr/bin:/bin"}
    skip = subprocess.run(["sh", str(CHECK)], env=env, capture_output=True, text=True)
    assert skip.returncode == 0
    assert "NOT CHECKED" in skip.stdout
    req = subprocess.run(["sh", str(CHECK)], env={**env, "CELL_CAPTURES_REQUIRED": "1"},
                         capture_output=True, text=True)
    assert req.returncode == 1


# ── the gps_fix stream and body naming ─────────────────────────────────────────
def test_body_names_match_the_shell_rule():
    """check_replay_goldens.sh's body_of() spells the same names."""
    assert cg.body_name("tmp_probe.dlf", cg.STREAM) == "tmp_probe.dlf.ndjson"
    assert cg.body_name("tmp_probe.dlf", cg.GPS_STREAM) == "tmp_probe.dlf.gps_fix.txt"
    assert cg.body_name("a/b/c.bin", cg.STREAM) == "a__b__c.bin.ndjson"
    text = CHECK.read_text()
    assert 'gps_fix) echo "$gold/$b.gps_fix.txt"' in text
    assert 'cell_observation) echo "$gold/$b.ndjson"' in text
    assert "sed 's|/|__|g'" in text


def test_gps_fix_lines_keep_only_the_accepted_fix_messages():
    err = ("message: decoder root: /x (this binary's own tree)\n"
           "message: celldiag GPS fix 1.000000,2.000000 alt=3m src=0x1476\n"
           "open: code=1\n"
           "message: celldiag GPS fix 1.000001,2.000000 alt=3m src=0x1476\n"
           "rows: 2\n")
    assert cg.gps_fix_lines(err) == (
        "celldiag GPS fix 1.000000,2.000000 alt=3m src=0x1476\n"
        "celldiag GPS fix 1.000001,2.000000 alt=3m src=0x1476\n")
    assert cg.gps_fix_lines("message: decoder root: /x\n") == ""


def test_inventory_lines_keep_only_the_end_of_stream_census():
    err = ("message: celldiag inventory: 3 (code,ver) key(s), 0 unrecognized\n"
           'census: {"table":"0xB193/v18,1,1,0,1,0,-","type":"diag_census"}\n'
           "message: celldiag GPS fix 1.0,2.0 alt=3m src=0x1476\n"
           "message: celldiag inventory: 5 (code,ver) key(s), 0 unrecognized\n"
           'census: {"table":"0xB193/v18,2,2,0,2,0,-","type":"diag_census"}\n')
    assert cg.inventory_lines(err) == (
        "celldiag inventory: 5 (code,ver) key(s), 0 unrecognized\n"
        'census: {"table":"0xB193/v18,2,2,0,2,0,-","type":"diag_census"}\n')
    assert cg.inventory_lines("rows: 0\n") == ""
    assert 'diag_inventory) echo "$gold/$b.diag_inventory.txt"' in CHECK.read_text()


def test_old_six_column_pins_still_read(tmp_path):
    m = tmp_path / "MANIFEST.tsv"
    m.write_text("# header\nfx.hdlc\tcell_observation\tc1\tg1\t3\t" + OLD_REV + "\n")
    assert cg.read_manifest(m)[("fx.hdlc", cg.STREAM)].kp_rev == "-"


def test_kp_rev_does_not_change_the_decision():
    old = _entry()
    new = cg.Entry(old.fixture, old.stream, old.capture_sha256, old.golden_sha256,
                   old.rows, old.diaggrok_rev, "f" * 40)
    assert cg.decide(old, new, cg.Request(), _never_called) == ""


def test_build_tree_rev_refuses_tracked_edits_but_not_untracked_files(decoder_repo):
    repo, revs = decoder_repo
    (repo / "capture_cell_diag").mkdir()
    (repo / "capture_cell_diag" / "x.c").write_text("int x;\n")
    _git(repo, "add", "capture_cell_diag/x.c")
    _git(repo, "commit", "-qm", "source")
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "capture_cell_diag" / "new_wip.c").write_text("untracked\n")
    assert cg.build_tree_rev(repo) == head
    (repo / "capture_cell_diag" / "x.c").write_text("int y;\n")
    with pytest.raises(cg.Refused, match="uncommitted"):
        cg.build_tree_rev(repo)


@pytest.mark.parametrize("path", ["capture_cell_diag/standalone.mk",
                                  "capture_cell_diag/Makefile.in",
                                  "celltools/Makefile"])
def test_every_build_recipe_is_a_guarded_input(decoder_repo, path):
    """The makefiles that produce the bridge binary are inputs too: an
    uncommitted recipe edit changes the binary as surely as a source edit."""
    repo, _ = decoder_repo
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text("all:\n")
    _git(repo, "add", path)
    _git(repo, "commit", "-qm", "recipe")
    assert cg.build_tree_rev(repo)
    (repo / path).write_text("all: edited\n")
    with pytest.raises(cg.Refused, match="uncommitted"):
        cg.build_tree_rev(repo)
