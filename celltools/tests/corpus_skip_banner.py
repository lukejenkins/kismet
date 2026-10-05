# SPDX-License-Identifier: Apache-2.0
"""A replay corpus that is absent must not read as a green run.

Every replay / native-vs-bridge A/B test in this directory skips when
``CELL_CAPTURES`` is unset -- correctly, since the corpus is host state and no
default path may be guessed. But a skip is quiet, and ``-q`` prints it as one
``s`` among hundreds of dots, so a broken native-vs-bridge test can stay red
indefinitely while every run without the variable reports green.

Two things, both driven from ``conftest.py``:

* a **banner** in the terminal summary whenever any test skipped for want of
  the corpus, with the count -- so a run that measured none of the A/B says so
  in the last lines anyone reads;
* ``CELL_CAPTURES_REQUIRED=1`` turns those skips into a **failed session**, for
  a host that has the corpus and wants the gate to be impossible to miss.

A skip counts as a corpus skip when its reason names ``CELL_CAPTURES`` -- the
wording every fixture-backed skip in this directory already uses.
"""
from __future__ import annotations

import os

CORPUS_ENV = "CELL_CAPTURES"
REQUIRED_ENV = "CELL_CAPTURES_REQUIRED"


def _reason(report) -> str:
    lr = getattr(report, "longrepr", None)
    if isinstance(lr, tuple) and len(lr) == 3:
        return str(lr[2])
    return str(lr or "")


def corpus_skips(stats) -> list:
    return [r for r in stats.get("skipped", []) if CORPUS_ENV in _reason(r)]


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get(REQUIRED_ENV) != "1":
        return
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    if tr is not None and corpus_skips(tr.stats) and exitstatus == 0:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    n = len(corpus_skips(terminalreporter.stats))
    if not n:
        return
    strict = os.environ.get(REQUIRED_ENV) == "1"
    terminalreporter.write_sep(
        "!", f"{n} test(s) SKIPPED: {CORPUS_ENV} is unset -- the replay A/B did not run",
        red=True, bold=True)
    terminalreporter.write_line(
        f"A pass above says nothing about native-vs-bridge parity. Set {CORPUS_ENV}=<dir> "
        f"to run them; {REQUIRED_ENV}=1 makes this a failure"
        + (" (it is set: this session FAILED)." if strict else "."))
