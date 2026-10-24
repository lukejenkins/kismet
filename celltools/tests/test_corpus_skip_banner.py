# SPDX-License-Identifier: Apache-2.0
"""A run that skipped the replay A/B for want of the corpus says so.

Runs a throwaway pytest session in a subprocess, wired to the real plugin, so
the banner and the exit status are measured on an actual terminal summary.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

CONFTEST = f"""
import sys
sys.path.insert(0, {str(HERE)!r})
from corpus_skip_banner import pytest_sessionfinish, pytest_terminal_summary  # noqa: F401
"""

TESTS = '''
import pytest
def test_ok():
    pass
def test_needs_corpus():
    pytest.skip("no capture directory: set CELL_CAPTURES=<dir> (fixtures live outside the tree)")
def test_other_skip():
    pytest.skip("binary built without make NATIVE=1")
'''


def _run(tmp_path: Path, tests: str, **env) -> subprocess.CompletedProcess:
    (tmp_path / "conftest.py").write_text(CONFTEST)
    (tmp_path / "test_x.py").write_text(tests)
    e = {k: v for k, v in os.environ.items()
         if k not in ("CELL_CAPTURES", "CELL_CAPTURES_REQUIRED")}
    e.update(env)
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           str(tmp_path)], cwd=tmp_path, env=e,
                          capture_output=True, text=True, timeout=120)


def test_a_corpus_skip_prints_the_banner_with_its_count(tmp_path):
    r = _run(tmp_path, TESTS)
    assert r.returncode == 0, r.stdout
    assert "1 test(s) SKIPPED: CELL_CAPTURES is unset" in r.stdout, r.stdout


def test_an_unrelated_skip_prints_no_banner(tmp_path):
    r = _run(tmp_path, TESTS.replace("CELL_CAPTURES=<dir>", "SOMETHING_ELSE=<dir>"))
    assert r.returncode == 0, r.stdout
    assert "SKIPPED: CELL_CAPTURES" not in r.stdout, r.stdout


def test_required_turns_a_corpus_skip_into_a_failed_session(tmp_path):
    r = _run(tmp_path, TESTS, CELL_CAPTURES_REQUIRED="1")
    assert r.returncode == 1, (r.returncode, r.stdout)
    assert "this session FAILED" in r.stdout, r.stdout


def test_required_is_silent_when_nothing_skipped_for_the_corpus(tmp_path):
    r = _run(tmp_path, TESTS.replace("CELL_CAPTURES=<dir>", "X=<dir>"),
             CELL_CAPTURES_REQUIRED="1")
    assert r.returncode == 0, (r.returncode, r.stdout)
