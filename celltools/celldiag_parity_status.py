#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""celldiag_parity_status — the ran-vs-skipped gauge for the parity harnesses.

## Why a gauge and not a gate

The correctness of this tree's C decode path is *defined* by agreement with
diaggrok, an external reference decoder. Two harnesses pin it, and both skip
when their driver -- or that reference -- is absent, which is the right
behaviour on a host that has neither, and exactly why a gauge is needed:
**a normal `pytest celltools/tests` run is green whether or not the contract
holds.**

Making them fail would be wrong (a fresh checkout has no built driver, by
design). What is needed is a *positive* signal saying "these ran, on this
host, at this commit". This prints it: non-fatal, but whoever is driving the
tree **sees** it.

## Exit codes

``0`` -- both harnesses can run here (or ran).
``0`` -- with ``--report``: always 0. The gauge never blocks a land.
``1`` -- with ``--check``: at least one harness would skip for a reason other
         than "this host cannot check it at all". That distinction is the whole
         point: an unconfigurable host is a fact about the machine; an unbuilt
         driver in a tree that IS here is a fact about nobody having run it.
``1`` -- with ``--run``: pytest exited 0 but **nothing actually executed**.
         An all-skipped pytest run also exits 0, so the exit
         code alone cannot tell those apart.

## Verdict vocabulary for ``--run``

``PASS``     both harnesses ran, every test executed, and all passed.
``PARTIAL``  one harness ran; the other's half of the contract is unverified.
``NO-OP``    nothing ran. **Not a pass** -- the contract is unchecked here.
``FAIL``     a harness that ran reported a **real** failure -- investigate.
``STALE-DRIVER``
             every failure is a helper binary older than its sources, so the
             staleness guard REFUSED to measure it. Nothing is
             broken **and nothing was verified** -- still exit 1, like
             ``NO-OP``, for the same reason. The remedy is one ``make`` line
             that the failure text already prints.

``STALE-DRIVER`` is its own verdict because the condition passes *discovery*
(``OK ... found built at``) and fails at *run* time. Reported as ``FAIL``, it
reads as a real breakage: dozens of tracebacks with one cause and a
one-command fix.

**A single unclassified failure keeps the verdict ``FAIL``.** An operator who
meets a red with a one-command remedy twice learns the shortcut, and the third
time it is a real break. A mixed run therefore stays
``FAIL`` *and* names which failures are the real ones.

## The verdict comes from what pytest RAN, not from what could have run

Discovery answers "is there a driver?". That is necessary and **not sufficient**:
both harnesses also skip when the *replay capture* is absent, and captures live
outside git, so fixture-absence is the more likely miss on a
fresh host than an unbuilt driver.

Deriving the verdict from discovery state alone is wrong: a host with both
drivers built and no replay capture would print *"the decode contract IS
checkable here"* directly above ``PASS`` while every test skipped. pytest can
tell the difference -- ``--junitxml`` reports passed and skipped counts per
test.

So ``PASS`` requires ``passed > 0 and skipped == 0`` per harness, and a skip
is reported with the reason pytest recorded.

This gauge does not check ``diagspec/`` against the generator that produces
it. That generator is not in this tree, and a gauge that shells out to a tool
it cannot see reports "clean" on every checkout that does not happen to sit
next to one.

Usage:
  python3 celltools/celldiag_parity_status.py            # report (default)
  python3 celltools/celldiag_parity_status.py --build    # build then report
  python3 celltools/celldiag_parity_status.py --check    # non-zero if avoidably skipping
  python3 celltools/celldiag_parity_status.py --run      # build, then RUN both harnesses
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent))

import celldiag_parity as parity  # noqa: E402

#: This directory. The harness paths below are relative to it, and ``--run``
#: invokes pytest from here, so the gauge works the same whatever directory the
#: operator happened to be standing in.
CELLTOOLS = Path(__file__).resolve().parent

HARNESS = {
    "logstream": "tests/test_celldiag_logstream_parity.py",
    "celldiag": "tests/test_celldiag_replay_ab.py",
    # Not a diaggrok-parity harness -- there is no oracle for an AT
    # dialogue -- but it is the same class of thing this gauge exists to keep
    # honest: a harness that silently does not run because nobody
    # built the binary. `report()` indexes HARNESS by role, so a role added to
    # celldiag_parity.ROLES without an entry here is a KeyError, which is the
    # correct coupling: the gauge should not be able to forget a role.
    "cellat": "tests/test_cellat_ipc.py",
}

# Statuses that mean "this host genuinely cannot check the contract" -- not a
# finding, just a property of the machine.
#
# `not-configurable` joins `no-tree` here. The distinction that matters is
# AVOIDABLE (an operator omission, with a remedy that works) vs UNAVOIDABLE (a
# fact about the machine).
# An unconfigured tree is avoidable; a tree that CANNOT be configured because
# the host lacks a configure-time system dep is not -- and printing the
# avoidable remedy for it is a silence whose stated cause is wrong, the one
# thing this gauge exists to stop.
_BENIGN = {"no-tree", "not-configurable"}
_OK = {"env", "discovered", "built"}


def report(allow_build: bool) -> list[parity.Discovery]:
    found = parity.discover_all(allow_build=allow_build or None)
    clone = parity.tree_root()
    print(f"# celldiag parity harnesses (tree: {clone or 'NOT A SOURCE TREE'})")
    for d in found:
        mark = "OK  " if d.ok else ("skip" if d.status in _BENIGN else "WARN")
        print(f"#   {mark} {d.role:<10} [{d.status}] {d.detail}")
        print(f"#        harness: {HARNESS[d.role]}")
    avoidable = [d for d in found if not d.ok and d.status not in _BENIGN]
    if avoidable:
        print("# ⚠️  at least one harness would skip for an AVOIDABLE reason.")
        print("#    A skipped parity harness is a green suite with the thing")
        print("#    under test never executed -- zero coverage")
        print("#    reported as health.")
        # The remedy has to match the reason. Telling an operator who just ran
        # --build to run --build is no help: --build cannot help an
        # unconfigured clone.
        if any(d.status == "not-configured" for d in avoidable):
            print(f"#    The tree was never ./configure'd -- --build CANNOT fix")
            print(f"#    that. Run:  cd {clone} && ./configure")
        else:
            print("#    Build with:")
            print("#      python3 celltools/celldiag_parity_status.py --build")
    elif all(d.ok for d in found):
        print(f"# all {len(found)} harnesses have a driver -- the decode "
              "contract IS checkable here.")
    # A benign skip is still worth ONE line of why, or the next operator
    # re-derives it. `not-configurable` in particular names a real, actionable
    # host fact -- it is just not an omission the operator made.
    stuck = [d for d in found if d.status == "not-configurable"]
    if stuck:
        print("# ℹ️  this host cannot ./configure this tree -- a host fact, not")
        print("#    an omission. It does NOT leave the contract unverified:")
        print("#    every role has a no-configure build, and")
        print("#    `--build` runs it. By hand, per role:")
        for d in stuck:
            esc = parity._STANDALONE_ESCAPE.get(d.role)
            if esc is not None:
                print(f"#      make -C {esc[0]} -f standalone.mk {esc[1]}"
                      f"   # {d.role} -> {esc[0]}/{esc[2]}")
        print("#    ...and the framework-free selftests:")
        # Derived from the SAME table the per-role skip reason reads, so the
        # summary always names the stuck role's directory: one source of truth
        # rather than two sentences that happen to agree today.
        for sub in sorted({parity._subdir_for(parity.ROLES[d.role][1])
                           for d in stuck}):
            print(f"#      make -C {sub} -f standalone.mk check")
    return found


@dataclass(frozen=True)
class ExpectedSkip:
    """A skip that is a property of the CHOSEN BUILD CONFIGURATION.

    Not "a skip we tolerate" -- a skip that *must* happen given how the tree
    was built, and whose remedy is a different build rather than a fix.
    """
    match: str      # substring of pytest's message; must be greppable in the harness
    why: str        # why the skip is correct, in one line
    remedy: str     # the command that makes it run, for an operator who wants it


#: The expected-skip table. Keep it SHORT and keep every entry
#: greppable in a harness source -- ``test_every_expected_skip_pattern_still_
#: matches_a_LIVE_harness_skip`` fails if a pattern goes dead, because a table
#: of dead patterns classifies nothing while looking authoritative.
#:
#: A REWORDED message degrades safely: it stops matching and reports as
#: UNEXPECTED, which is the alarm we want. Adding an entry is a decision --
#: the test pins the length so the table cannot shrink or grow silently.
EXPECTED_SKIPS = (
    ExpectedSkip(
        match="nativedecode=shadow is refused by design",
        why="this binary is the Python-decode BASELINE build, and the shadow "
            "leg is an optional-mode test, never a release gate",
        remedy="make NATIVE=1 -C capture_cell_diag",
    ),
    ExpectedSkip(
        match="unset — build both configurations with",
        why="the two-build replay-equality gate needs TWO binaries in "
            "different configurations; building them inside pytest would "
            "relink twice and mutate the tree under measurement. The make "
            "target itself is non-mutating — it restores the mode the tree "
            "entered in, so running it costs you nothing",
        remedy="make -C celltools replay-gate",
    ),
    ExpectedSkip(
        # Trimmed to survive the dead-pattern guard: the harness splits this
        # sentence across two f-string pieces, so anything spanning the join is
        # dead on arrival.
        match="is a NATIVE=1 build",
        why="the exact MIRROR of the first entry above: `nativedecode=on` must "
            "be REFUSED by a build with no legs (over the stub it would "
            "suppress the only renderer present and emit nothing in its "
            "place), and asserting that needs a baseline binary. A configured "
            "tree holds exactly ONE configuration, so on a NATIVE=1 tree this "
            "skip is the tree being in the other valid state -- not a "
            "regression",
        remedy="make -C celltools replay-gate  (or a plain "
               "`make -C capture_cell_diag`)",
    ),
    ExpectedSkip(
        # The discovery-side `not-configurable` status, surfacing in the RUN
        # phase. Discovery already lists it in _BENIGN ("a host fact, not an
        # omission"), so the run phase must not class the resulting skips as
        # UNEXPECTED, or every run on such a host (e.g. macOS) raises dozens of
        # alarms at a host fact. The text is composed in celldiag_parity.discover(), not in a harness, so
        # the dead-pattern guard reads that module too.
        match="cannot be ./configure'd on this host",
        why="a configure-time system dependency is absent on this host -- the "
            "same `not-configurable` status discovery already classes as "
            "benign. Every role has a no-configure build, so this "
            "skip now means `--build` was not asked for, not that the leg is "
            "unreachable",
        remedy="python3 celltools/celldiag_parity_status.py --run  (it builds "
               "each role via standalone.mk when the tree is unconfigured)",
    ),
    ExpectedSkip(
        # The pattern stops at "set " on purpose. The harness builds this
        # message as f"...set {_CAPTURES_ENV}=<dir>...", so the variable's NAME
        # never appears literally in the source and a pattern containing
        # `CELL_CAPTURES` is dead on arrival.
        match="no capture directory: set ",
        why="the replay fixtures are large binary recordings kept OUTSIDE any "
            "source tree, so whether they are reachable is a fact about the "
            "HOST, not an operator omission. Classing it UNEXPECTED would raise "
            "an alarm on every host without the capture directory, and a gauge "
            "that cries regression at a host fact is one an operator stops "
            "reading",
        remedy="export CELL_CAPTURES=<dir> (e.g. ~/captures)",
    ),
)


@dataclass(frozen=True)
class StaleDriver:
    """A FAILURE that means "the binary predates its sources", not "parity broke".

    The staleness guard is doing its job when this fires: it refuses to
    measure a driver that does not correspond to its sources, because a stale
    one silently tests code nobody shipped. But the verdict called that
    ``FAIL``, which this gauge documents as *"a real breakage, investigate"* --
    misleading in the expensive direction for a cause whose remedy is one
    command the failure text already prints.
    """
    match: str      # substring of pytest's message; must be greppable in the harness
    why: str        # why this red is not a parity break, in one line
    remedy: str     # what makes it measurable again


#: The driver-staleness table, a deliberate mirror of
#: ``EXPECTED_SKIPS`` above -- same shape, same dead-pattern guard, same
#: degrade-safely property: a reworded message stops matching and the verdict
#: falls back to ``FAIL``, which is the conservative direction.
#:
#: **There are TWO spellings.** The cellat and logstream harnesses say
#: ``KP_CELLAT_BIN is STALE``-style text; the celldiag harness leads with
#: ``REBUILD NEEDED:`` instead, deliberately -- pytest's short summary shows
#: only the first line, so the fix goes first. A one-pattern classifier would
#: leave the verdict at FAIL for most runs while looking correct.
#:
#: **Contiguous in SOURCE is not the same as contiguous at runtime.** All
#: three emitters print "is older than its sources", which looks like the
#: obvious single pattern -- but the logstream harness splits that phrase
#: across an f-string join, so as a pattern it is dead on arrival. That is the
#: same trap ``EXPECTED_SKIPS`` documents.
STALE_DRIVER_PATTERNS = (
    StaleDriver(
        match="is STALE: ",
        why="the cellat and logstream harnesses refuse to measure a driver "
            "older than its sources -- nothing is broken, nothing was measured",
        remedy="the failure text prints the exact `make` line; run it and "
               "re-run this gauge",
    ),
    StaleDriver(
        match="REBUILD NEEDED: ",
        why="the celldiag replay harness's spelling of the same refusal; it "
            "leads with the rebuild command because pytest's short summary "
            "shows only the first line",
        remedy="the failure text prints the exact `make` line; run it and "
               "re-run this gauge",
    ),
)


def classify_failures(messages: dict[str, int]
                      ) -> tuple[list[tuple[str, int, StaleDriver]],
                                 dict[str, int]]:
    """Split a failure census into driver-staleness and everything else.

    **The caller must require ``other`` to be EMPTY before relabelling.**
    An operator who meets a red with a one-command remedy
    twice learns the shortcut, and the third time it is real. If one genuine
    parity break could hide among 74 staleness failures and get relabelled,
    this classifier would be strictly worse than the bug it fixes.
    """
    stale: list[tuple[str, int, StaleDriver]] = []
    other: dict[str, int] = {}
    for message, n in messages.items():
        for s in STALE_DRIVER_PATTERNS:
            if s.match in message:
                stale.append((message, n, s))
                break
        else:
            other[message] = n
    return stale, other


def classify_skips(reasons: dict[str, int]
                   ) -> tuple[list[tuple[str, int, ExpectedSkip]],
                              dict[str, int]]:
    """Split a skip census into expected-by-configuration and unexpected.

    **A COUNT cannot answer "is this a regression?"** A documented number of
    residual skips goes stale the moment a legitimate new skip is added, and a
    reader comparing it against a live run cannot tell an expected new skip
    from a regression. The reasons are stable, self-describing and each names its own
    remedy -- so classify them and let the number fall out.
    """
    expected: list[tuple[str, int, ExpectedSkip]] = []
    unexpected: dict[str, int] = {}
    for reason, n in reasons.items():
        for e in EXPECTED_SKIPS:
            if e.match in reason:
                expected.append((reason, n, e))
                break
        else:
            unexpected[reason] = n
    return expected, unexpected


@dataclass
class Counts:
    """What pytest actually did with one harness's tests."""
    passed: int = 0
    skipped: int = 0
    failed: int = 0
    reason: str = ""       # first skip reason pytest recorded, verbatim
    #: EVERY distinct skip reason -> how many tests skipped for it.
    #: ``reason`` alone under-reports the moment a harness skips for two
    #: causes: the minority cause appears nowhere in the gauge's output.
    reasons: dict[str, int] = field(default_factory=dict)
    #: EVERY distinct FAILURE message -> how many tests failed with it.
    #: Without it, by the time ``verdict`` sees a red, *why* it was red has
    #: already been discarded one layer down, and the verdict can only say
    #: "FAIL (pytest rc=1)".
    failures: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Keep the two views consistent by CONSTRUCTION rather than by every
        # caller remembering. Two fields describing the same thing that can
        # disagree is how an under-report survives unnoticed.
        if not self.reason and self.reasons:
            self.reason = next(iter(self.reasons))

    @property
    def executed(self) -> bool:
        """The bar for PASS: something ran and nothing was passed over."""
        return self.passed > 0 and self.skipped == 0 and self.failed == 0

    def census(self) -> dict[str, int]:
        """The skip census, falling back to the legacy single ``reason``.

        The fallback matters: a ``Counts`` built by an older caller must not
        classify as *zero skips of any kind*, which would read as "nothing to
        see" -- the failure direction this whole gauge exists to prevent.
        """
        if self.reasons:
            return dict(self.reasons)
        if self.skipped and self.reason:
            return {self.reason: self.skipped}
        return {}


def _role_of(classname: str) -> str | None:
    """Map a junit ``classname`` (``tests.test_celldiag_replay_ab``) to a role."""
    for role, path in HARNESS.items():
        if Path(path).stem in classname:
            return role
    return None


def parse_junit(xml_path: Path) -> dict[str, Counts]:
    """Per-harness pass/skip/fail counts from a pytest ``--junitxml`` report.

    pytest CAN tell us what ran: an all-skipped run and an all-passed run both exit 0, but they are plainly
    different in the report.
    """
    counts: dict[str, Counts] = {role: Counts() for role in HARNESS}
    if not xml_path.is_file():
        return counts
    root = ElementTree.parse(xml_path).getroot()
    for case in root.iter("testcase"):
        role = _role_of(case.get("classname", ""))
        if role is None:
            continue
        c = counts[role]
        skip = case.find("skipped")
        if skip is not None:
            c.skipped += 1
            msg = (skip.get("message") or "").strip()
            if not c.reason:
                c.reason = msg
            c.reasons[msg] = c.reasons.get(msg, 0) + 1
        elif (bad := (case.find("failure")
                      if case.find("failure") is not None
                      else case.find("error"))) is not None:
            c.failed += 1
            # Record WHY, not just that. junit puts the assertion text
            # in @message; fall back to the element body, which is where a
            # pytest.fail() with a long message can end up.
            msg = ((bad.get("message") or "") or (bad.text or "")).strip()
            if msg:
                c.failures[msg] = c.failures.get(msg, 0) + 1
        else:
            c.passed += 1
    return counts


def _stall_detail(role: str, c: Counts) -> str:
    """One harness's shortfall, classified.

    Not ``f"{role}: {c.skipped} skipped ({c.reason})"``: a single reason
    standing in for a heterogeneous set prints ``7 skipped (<the 6-test
    reason>)`` and the 7th skip's cause appears nowhere at all.
    """
    census = c.census()
    if not census:
        return f"{role}: {c.skipped} skipped"
    expected, unexpected = classify_skips(census)
    n_exp = sum(n for _, n, _ in expected)
    n_unexp = sum(unexpected.values())
    out = (f"{role}: {c.skipped} skipped "
           f"({n_exp} expected-by-configuration, {n_unexp} unexpected)")
    if unexpected:
        # Name them. A bare count of unexpected skips is a number the operator
        # has to go and decode elsewhere.
        out += " — unexpected: " + "; ".join(
            f"{r} [×{n}]" for r, n in sorted(unexpected.items()))
    return out


def _first_line(message: str) -> str:
    """The head of a failure message, for naming it inside a one-line verdict."""
    head = message.strip().splitlines()[0] if message.strip() else message
    return head if len(head) <= 160 else head[:157] + "..."


def _red_verdict(rc: int, counts: dict[str, Counts],
                 found: list[parity.Discovery] | None = None) -> tuple[str, int]:
    """Classify a non-zero pytest run: is this a breakage or a stale binary?

    **`STALE-DRIVER` still exits 1.** Renaming the verdict must not turn it
    into a pass -- nothing was measured, because the guard REFUSED to run
    against a binary that does not correspond to its sources. That is the same
    "nothing ran, so nothing complained" shape as ``NO-OP``, and it returns 1
    for the same reason. What changes is only the WORD the operator reads: a
    plain ``FAIL`` would send them looking for a parity break that does not
    exist.

    Three separate refusals to be clever here, each one a way this could
    end up worse than the bug it fixes:

    * **Any unclassified failure keeps the verdict FAIL.** One real break
      hiding among 74 staleness failures must not be relabelled.
    * **An EMPTY census keeps the verdict FAIL.** A missing or truncated junit
      report, or a collection error, gives rc!=0 with nothing recorded --
      putting the friendliest label on the least understood run.
    * **A mixed run NAMES the real failures.** Staying FAIL is necessary but
      not sufficient; the operator still has to find the one real failure
      among 75, so the verdict says which is which.
    """
    census: dict[str, int] = {}
    for c in counts.values():
        for message, n in c.failures.items():
            census[message] = census.get(message, 0) + n

    stale, other = classify_failures(census)
    n_stale = sum(n for _, n, _ in stale)
    n_other = sum(other.values())

    if stale and not other:
        remedies = sorted({s.remedy for _, _, s in stale})
        # The drivers share libkismetdatasource.a — rebuilding one restamps
        # the archive and makes a sibling stale, so a per-driver remedy sends
        # the operator through a SECOND STALE-DRIVER run for a different
        # driver. When we discovered ≥2 built drivers, name the ONE command that
        # rebuilds them all and converges in a single pass.
        convergent = parity.convergent_rebuild_for(found) if found else None
        convergent_note = (
            f" Rebuild ALL drivers in one pass (they share "
            f"libkismetdatasource.a — rebuilding one alone restamps the archive "
            f"and re-stales the others): {convergent}."
            if convergent else "")
        return (f"STALE-DRIVER -- all {n_stale} failures are driver staleness, "
                f"NOT a parity break: a helper binary is "
                f"older than its sources, so the harness refused to measure "
                f"it. Nothing is broken and nothing was verified. "
                f"{'; '.join(remedies)}.{convergent_note} Refused to run against: "
                + "; ".join(sorted({_first_line(m) for m, _, _ in stale}))), 1

    if stale:
        # The mixed case -- the one where the operator most needs help.
        return (f"FAIL (pytest rc={rc}) -- {n_other} real failure(s), plus "
                f"{n_stale} driver-staleness failure(s) that are noise "
                f"(rebuild). The real ones: "
                + "; ".join(f"{_first_line(m)} [×{n}]"
                            for m, n in sorted(other.items()))), 1

    return f"FAIL (pytest rc={rc})", 1


def verdict(found: list[parity.Discovery], rc: int,
            counts: dict[str, Counts]) -> tuple[str, int]:
    """Turn (discovery, pytest rc, what actually ran) into a verdict + exit code.

    Split out from :func:`run_harnesses` so it is testable without a pytest
    subprocess -- the counts are the whole input, and they are the thing that
    was previously never consulted.
    """
    if rc != 0:
        return _red_verdict(rc, counts, found)

    executed = [d.role for d in found if counts[d.role].executed]
    if not executed:
        # Distinguish the two silences: no driver at all vs drivers present and
        # every test skipped anyway. The remedy differs completely.
        if not any(d.ok for d in found):
            why = "no harness had a driver"
        else:
            reasons = {counts[d.role].reason for d in found if counts[d.role].reason}
            why = ("a driver was found for every harness, but pytest SKIPPED "
                   "every test: " + ("; ".join(sorted(r for r in reasons if r))
                                     or "no reason recorded"))
        return (f"NO-OP -- pytest exited 0 but NOTHING RAN ({why}). This is NOT "
                f"a pass; the contract is unverified here"), 1

    stalled = [d.role for d in found if not counts[d.role].executed]
    if stalled:
        detail = "; ".join(_stall_detail(role, counts[role]) for role in stalled)
        # Report the TEST fraction beside the harness fraction. `PARTIAL`
        # alone hides magnitude: the same word covers "one small harness
        # stalled" and "two thirds of the contract never ran". A harness count
        # cannot show that; harnesses are very unequal in size (celldiag is
        # most of the tests).
        ran_tests = sum(counts[d.role].passed + counts[d.role].failed for d in found)
        all_tests = sum(counts[d.role].passed + counts[d.role].failed
                        + counts[d.role].skipped for d in found)
        return (f"PARTIAL -- {len(executed)}/{len(found)} harnesses, "
                f"{ran_tests}/{all_tests} tests ran; "
                f"{detail} (that half of the contract is unverified)"), 0

    total = sum(counts[d.role].passed for d in found)
    return f"PASS ({total} tests executed, 0 skipped)", 0


def run_harnesses(found: list[parity.Discovery]) -> int:
    """Actually run both, so the gauge can report RAN rather than RUNNABLE.

    **A run in which nothing executed must not print PASS.** Two easy ways to
    get that wrong: an unconfigured tree where every harness skips, and a tree
    with **both drivers built and no replay capture**, where discovery says
    "the decode contract IS checkable here" and every test still skips. The
    verdict therefore comes from the junit report: what pytest ran, not what
    it could have run.
    """
    with tempfile.TemporaryDirectory() as td:
        xml = Path(td) / "parity.xml"
        rc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q",
             f"--junitxml={xml}", *HARNESS.values()],
            cwd=CELLTOOLS, check=False,
        ).returncode
        counts = parse_junit(xml)

    for role in sorted(counts):
        c = counts[role]
        print(f"#   ran  {role:<10} passed={c.passed} skipped={c.skipped} "
              f"failed={c.failed}")
        # Every distinct reason, classified -- not just the first one.
        expected, unexpected = classify_skips(c.census())
        for reason, n, e in expected:
            print(f"#        skip ×{n} EXPECTED — {e.why}")
            print(f"#             run it with: {e.remedy}")
            print(f"#             pytest said: {reason}")
        for reason, n in sorted(unexpected.items()):
            print(f"#        skip ×{n} ⚠️ UNEXPECTED — {reason}")

    text, ret = verdict(found, rc, counts)
    print(f"# parity harness run: {text}")
    return ret


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--build", action="store_true",
                    help="build any missing driver in this tree first")
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero when a harness would skip avoidably")
    ap.add_argument("--run", action="store_true",
                    help="build, then RUN both harnesses (implies --build)")
    args = ap.parse_args()

    found = report(allow_build=args.build or args.run)
    if args.run:
        return run_harnesses(found)
    if args.check:
        avoidable = [d for d in found if not d.ok and d.status not in _BENIGN]
        return 1 if avoidable else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
