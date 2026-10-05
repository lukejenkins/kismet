"""The bridge must import with ONLY the public decoder library on the path.

A module-level import of anything outside the decoder library makes the bridge
fail to import on a clean host, before any flag is read.

The interpreter under test is the system python3, named explicitly, not
``sys.executable``. That is the difference between a real check and a false
green:

  * ``sys.executable`` is whatever ran pytest, which on a developer workstation
    is a virtualenv that may have a decoder library installed as an EDITABLE
    package. An editable install arrives via a ``.pth`` file in
    ``site-packages``, which Python loads regardless of ``PYTHONPATH`` -- so
    scrubbing the environment cannot unhook it, and the import this test exists
    to forbid would succeed by the wrong route while the test reported PASS.
  * ``/usr/bin/python3`` is also the interpreter the C capture source execs, so
    pinning it means this test measures the deployment contract rather than a
    neighbouring one.

``test_the_probe_can_actually_fail`` is the positive control: a gauge that has
never read negative cannot be trusted when it reads positive.
"""
import os
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
DEPS = HERE / ".deps" / "diaggrok" / "src"

# The interpreter the C side execs. See the module docstring for why this is
# pinned rather than inherited from the test runner.
SYSTEM_PYTHON = "/usr/bin/python3"

_CLEAN_ENV = {"PATH": "/usr/bin:/bin"}


def _import_bridge(pythonpath: str) -> subprocess.CompletedProcess:
    """Import the bridge in a subprocess with a REPLACED environment.

    ``env=`` replaces rather than extends deliberately -- an inherited
    ``PYTHONPATH`` pointing at another decoder checkout is exactly the contamination this
    test exists to exclude.
    """
    return subprocess.run(
        [SYSTEM_PYTHON, "-c",
         "import kismet_diag_decode as m; print(m.__name__)"],
        cwd=HERE, env={**_CLEAN_ENV, "PYTHONPATH": pythonpath},
        capture_output=True, text=True)


# The library the bridge is allowed to import, and the only one whose presence
# in the SYSTEM interpreter can break the isolation asserted below.
DECODER_PKG = "diaggrok"


# Both collectors take the interpreter as an argument rather than closing over
# SYSTEM_PYTHON. That is not generality for its own sake: it is the only way
# the tests can reach the failure branches, which on a healthy host are
# unreachable.

def _decoder_origin(python: str = SYSTEM_PYTHON) -> str | None:
    """Where ``python`` finds the decoder with NOTHING on the path.

    ``None`` means it does not find one -- i.e. the isolation is intact and
    there is nothing to explain. Never raises: this runs inside an assertion
    message, where an exception would replace a legible failure with a
    traceback from the diagnostic itself.

    A non-zero exit reports ``None`` even if the probe wrote to stdout. A
    failed probe has no origin to report, and printing whatever it happened to
    emit would put a fabricated "resolved from:" line into the diagnosis --
    worse than saying nothing.
    """
    try:
        proc = subprocess.run(
            [python, "-c", f"import {DECODER_PKG} as d; print(d.__file__)"],
            cwd=HERE, env={**_CLEAN_ENV, "PYTHONPATH": "/nonexistent"},
            capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _stray_install_evidence(python: str = SYSTEM_PYTHON) -> list[str]:
    """Decoder ``.pth`` / dist-info entries in ``python``'s site directories.

    An editable install is exactly these two artefacts, and they are what an
    operator has to remove. Never raises, for the reason given above.
    """
    try:
        proc = subprocess.run(
            [python, "-c",
             "import site;d=list(site.getsitepackages());"
             "d.append(site.getusersitepackages());"
             "print('\\n'.join(d))"],
            cwd=HERE, env=dict(_CLEAN_ENV),
            capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            return []
        found: list[str] = []
        for line in proc.stdout.splitlines():
            site_dir = Path(line.strip())
            if not line.strip() or not site_dir.is_dir():
                continue
            for pattern in (f"*{DECODER_PKG}*", "__editable__*"):
                found += [str(p) for p in site_dir.glob(pattern)]
        return sorted(set(found))
    except Exception:
        return []


def explain_isolation_breach(origin: str | None, evidence: list[str]) -> str:
    """Explain a positive control that failed to fail, and say whose bug it is.

    The two branches differ on purpose. With evidence in hand this is a
    host condition with a one-command remedy. With none, it is *undiagnosed*,
    and must say so, because
    a diagnosis that always blamed host hygiene would send the next reader
    hunting a file that is not there while a real regression (a vendored
    decoder, a ``sitecustomize``, a decoder gone importable from the stdlib
    path) sat unexamined. Absence of evidence reads as "not located", never as
    "nothing to find".
    """
    lines = [
        "the bridge imported with NO decoder library on the path -- the "
        "isolation this suite asserts is not real"]
    if origin:
        lines.append(f"  {DECODER_PKG} resolved from: {origin}")
    if evidence:
        lines.append(
            f"  a stray {DECODER_PKG} install is present in the SYSTEM "
            f"interpreter's site-packages ({SYSTEM_PYTHON}):")
        lines += [f"    {p}" for p in evidence]
        lines.append(
            "  This is a HOST condition, not a defect in this tree: an "
            "editable install arrives via a .pth, which Python loads "
            "regardless of PYTHONPATH, so no environment scrubbing can "
            "unhook it. Remedy -- remove the paths listed above. "
            f"{DECODER_PKG} belongs on PYTHONPATH via the project venv, "
            "never installed into the base interpreter.")
    else:
        lines.append(
            "  The route could not be located: no decoder .pth or dist-info "
            f"was found in {SYSTEM_PYTHON}'s site directories. Do NOT assume "
            "host hygiene -- look for a vendored copy of the decoder, a "
            "sitecustomize, or a decoder that became importable from the "
            "stdlib path. Something in this tree may genuinely have "
            "regressed.")
    return "\n".join(lines)


@pytest.fixture(autouse=True)
def _require_system_python():
    if not os.access(SYSTEM_PYTHON, os.X_OK):
        pytest.fail(
            f"{SYSTEM_PYTHON} is missing. This is a FAILURE, not a skip: the C "
            "capture source execs exactly this path, so a host without it "
            "cannot run the bridge at all.")


# A red here means the deployed helper cannot import on a clean host: the
# pinned diaggrok is missing a module the bridge imports. Fix it by bumping the
# pin to a diaggrok release that has the module, not by marking this xfail.
def test_bridge_imports_against_public_decoder_only():
    assert DEPS.is_dir(), "run `make -f standalone.mk deps` first"
    proc = _import_bridge(str(DEPS))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "kismet_diag_decode"


def test_the_probe_can_actually_fail():
    """Positive control for the check above.

    Without the fetched decoder on the path the import MUST fail. If this
    passes, the preceding test is not measuring what it claims -- the decoder
    is reaching the interpreter by some route the path does not control.

    If this reads red, read the message: it names the route. The usual cause
    is a stray ``pip install -e`` of the decoder into the system interpreter,
    which is host hygiene and not a defect in this tree; see
    ``explain_isolation_breach``.
    """
    proc = _import_bridge("/nonexistent")
    assert proc.returncode != 0, explain_isolation_breach(
        _decoder_origin(), _stray_install_evidence())
    assert "diaggrok" in proc.stderr, proc.stderr


def test_no_private_only_imports_remain():
    src = (HERE / "kismet_diag_decode.py").read_text()
    for banned in ("f3correlate", "qsr4", "_capture_io"):
        assert banned not in src, f"{banned} still referenced"


# ── the diagnosis the positive control prints when it fails ──────────────────
# These test ``explain_isolation_breach`` directly rather than through a
# deliberately-broken host, because the condition it explains cannot be
# induced from inside the test run: a ``.pth`` in system site-packages is
# loaded at interpreter start-up, so there is no monkeypatch that creates one.

def test_the_diagnosis_names_every_stray_install_it_was_given():
    msg = explain_isolation_breach(
        "/repo/libs/diaggrok/src/diaggrok/__init__.py",
        ["/usr/local/lib/python3.13/dist-packages/__editable__.diaggrok-0.1.0.pth",
         "/usr/local/lib/python3.13/dist-packages/diaggrok-0.1.0.dist-info"])
    assert "__editable__.diaggrok-0.1.0.pth" in msg
    assert "diaggrok-0.1.0.dist-info" in msg


def test_the_diagnosis_reports_where_the_decoder_was_imported_from():
    msg = explain_isolation_breach(
        "/repo/libs/diaggrok/src/diaggrok/__init__.py", [])
    assert "/repo/libs/diaggrok/src/diaggrok/__init__.py" in msg


def test_the_diagnosis_says_a_located_cause_is_HOST_hygiene_not_a_code_defect():
    msg = explain_isolation_breach("/x/diaggrok/__init__.py", ["/sp/stray.pth"])
    assert "host" in msg.lower()
    assert "not a defect in this tree" in msg


def test_the_diagnosis_REFUSES_to_blame_the_host_when_it_found_no_stray_install():
    """The failure mode this guards is the mirror of the one it fixes.

    A diagnosis that always says "delete a .pth" would send the next reader
    hunting a file that is not there while a real regression -- a vendored
    copy of the decoder, a sitecustomize, a decoder that became importable
    from the stdlib path -- sits unexamined. Absence of evidence must read as
    'not located', never as 'nothing to find'.
    """
    msg = explain_isolation_breach("/x/diaggrok/__init__.py", [])
    assert "not a defect in this tree" not in msg
    assert "could not be located" in msg


def test_the_diagnosis_still_states_the_breach_when_nothing_at_all_is_known():
    msg = explain_isolation_breach(None, [])
    assert "isolation this suite asserts is not real" in msg


def test_the_remedy_is_only_offered_alongside_the_evidence_for_it():
    """The remedy sentence and the evidence list travel together or not at all."""
    with_evidence = explain_isolation_breach("/x/d/__init__.py", ["/sp/s.pth"])
    without = explain_isolation_breach("/x/d/__init__.py", [])
    assert "remove" in with_evidence.lower()
    assert "remove" not in without.lower()


def test_the_probes_answer_without_raising_on_this_host():
    """Smoke: the two collectors must never turn a red into an ERROR.

    They run *inside* an assertion message, so an exception in either one
    replaces a legible failure with a traceback from the diagnostic itself.

    This exercises only the path a healthy host can reach. The branches
    that matter are covered by the two tests below, which supply a broken
    interpreter; without them the ``except`` guards are asserted by nothing
    and a mutation removing them would survive.
    """
    origin = _decoder_origin()
    assert origin is None or isinstance(origin, str)
    assert isinstance(_stray_install_evidence(), list)


def test_a_probe_that_cannot_even_be_LAUNCHED_answers_rather_than_raising():
    """The un-runnable-interpreter branch -- unreachable on a healthy host."""
    missing = "/nonexistent/bin/python-that-is-not-there"
    assert _decoder_origin(missing) is None
    assert _stray_install_evidence(missing) == []


def test_a_FAILED_origin_probe_reports_no_origin_even_though_it_printed(tmp_path):
    """A non-zero probe must not have its stdout promoted to an origin.

    Otherwise the diagnosis gains a fabricated "resolved from: <garbage>"
    line, which sends the reader somewhere that was never on sys.path.
    """
    fake = tmp_path / "noisy-failing-python"
    fake.write_text("#!/bin/sh\necho /this/was/never/imported\nexit 1\n")
    fake.chmod(0o755)
    assert _decoder_origin(str(fake)) is None
    assert _stray_install_evidence(str(fake)) == []
