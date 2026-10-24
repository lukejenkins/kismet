# SPDX-License-Identifier: Apache-2.0
"""Locating the reference decoder, with the silence named.

Some harnesses here define this tree's C decode path as correct **by agreement
with a reference decoder**: diaggrok, driven by the ``kismet_diag_decode.py``
bridge that ``capture_cell_diag`` spawns at runtime. The reference is not
vendored and will not be: two copies of a decode truth diverge, and the
divergence is invisible precisely because both sides look self-consistent.

## One rule, and it is the binary's

The reference is located exactly the way the **shipped binary** locates its
decoder (``capture_cell_diag.c``: ``self_tree_decoder_root`` /
``resolve_python`` / ``resolve_helper_script``), with nothing to configure:

* the **library** is this tree's ``capture_cell_diag/.deps/diaggrok/src`` --
  what ``make -f standalone.mk deps`` fetches at the pinned ref;
* the **bridge** is ``capture_cell_diag/kismet_diag_decode.py`` beside it;
* the **interpreter** is ``python3`` on ``$PATH`` (then ``/usr/bin/python3``),
  with the library prepended to the child's ``PYTHONPATH``.

A developer who wants the A/B measured against a different diaggrok makes
``.deps/diaggrok`` BE that diaggrok (a symlink to a working tree with the same
``src/diaggrok`` layout), and puts an interpreter that can run it first on
``$PATH``. Both legs then see the same decoder, because both look in the same
place. A leg that resolved its decoder differently from the other would be
measuring a different program.

## Why a skip, and why the reason is in the message

**A "skipped" with no reason is how a suite reports zero coverage as health.**
Every harness in this directory guards against that, so the statuses below are distinct strings and every skip carries one:

==================  ==========================================================
``tree``            this tree carries a usable reference decoder
``no-decoder``      ``.deps/diaggrok/src`` is absent: ``deps`` never ran
``no-bridge``       the decoder is there but the bridge script is not
``no-interpreter``  no ``python3`` on ``$PATH`` or at ``/usr/bin/python3``
==================  ==========================================================
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

#: This tree's own root -- ``celltools/`` sits directly under it.
_TREE_ROOT = Path(__file__).resolve().parent.parent

#: Where the bridge sits under a tree root. This is the second entry of
#: ``capture_cell_diag.c``'s ``helper_script_rels`` (the first, a bare basename,
#: is the binary's view from inside ``capture_cell_diag/`` and the installed
#: layout); keep the two in step.
HELPER_SCRIPT_RELS = ("capture_cell_diag/kismet_diag_decode.py",)

#: The fetched decoder's importable root under a tree root: the binary's
#: ``HELPER_DEPS_SRC_REL`` (``.deps/diaggrok/src``) as seen from
#: ``capture_cell_diag/``, where the binary is built and finds it at level 0.
DECODER_SRC_REL = "capture_cell_diag/.deps/diaggrok/src"

#: The one command that fixes ``no-decoder``.
_FETCH_HINT = "run `make -f standalone.mk deps` in capture_cell_diag/"


@dataclass(frozen=True)
class Oracle:
    status: str
    root: Path | None
    python: Path | None
    script: Path | None
    detail: str
    #: The diaggrok source root the child must get FIRST on ``PYTHONPATH``.
    pythonpath: Path | None = None

    @property
    def ok(self) -> bool:
        return self.script is not None

    def skip_reason(self) -> str:
        """A skip message that names WHICH silence this is."""
        return f"[{self.status}] {self.detail}"

    def cmd(self, *args: str) -> list[str]:
        """The command line that runs the reference decoder.

        Run it with :meth:`env` as its environment -- otherwise the bridge
        imports whatever diaggrok the interpreter happens to have, or dies at
        ``import diaggrok`` and relays zero observations, which reads as a
        contentful decode that simply found nothing.
        """
        if not self.ok:
            raise RuntimeError(f"no decode oracle: {self.detail}")
        assert self.python is not None and self.script is not None
        return [str(self.python), str(self.script), *args]

    def env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """The child environment for :meth:`cmd`: :attr:`pythonpath` PREPENDED.

        First, so the tree's decoder is authoritative over any looser ambient
        ``PYTHONPATH`` and over the interpreter's own site-packages -- the exact
        wiring ``capture_cell_diag.c``'s ``child_prepend_decoder_pythonpath``
        does.
        """
        env = dict(os.environ if base is None else base)
        if self.pythonpath is not None:
            prev = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (f"{self.pythonpath}{os.pathsep}{prev}"
                                 if prev else str(self.pythonpath))
        return env


def helper_script_in(root: Path) -> Path | None:
    """The bridge script inside ``root``, or ``None``."""
    return next((root / rel for rel in HELPER_SCRIPT_RELS
                 if (root / rel).is_file()), None)


def decoder_src_in(root: Path) -> Path | None:
    """The fetched ``diaggrok`` source root inside ``root``, or ``None``."""
    src = root / DECODER_SRC_REL
    return src if (src / "diaggrok").is_dir() else None


def find_python3() -> Path | None:
    """``python3`` on ``$PATH``, then ``/usr/bin/python3`` -- the binary's
    ``resolve_python``. ABSOLUTE, never a bare name: the bridge runs after a
    chdir into the decoder root, where a relative name would resolve against the
    wrong cwd."""
    which = shutil.which("python3")
    if which:
        return Path(which)
    hard = Path("/usr/bin/python3")
    return hard if os.access(hard, os.X_OK) else None


def discover(tree: Path | None = None) -> Oracle:
    """Resolve the reference decoder in ``tree`` (default: this tree)."""
    root = _TREE_ROOT if tree is None else Path(tree)

    src = decoder_src_in(root)
    if src is None:
        return Oracle(
            "no-decoder", root, None, None,
            f"this tree has no fetched decoder at {root / DECODER_SRC_REL}, so "
            "the agreement between its C decode path and the reference decoder "
            f"cannot be checked here -- this is a skip, NOT a pass ({_FETCH_HINT})")

    script = helper_script_in(root)
    if script is None:
        return Oracle(
            "no-bridge", root, None, None,
            f"{root} has a fetched decoder but no bridge at "
            f"{', '.join(HELPER_SCRIPT_RELS)}")

    python = find_python3()
    if python is None:
        return Oracle(
            "no-interpreter", root, None, None,
            "no python3 on $PATH and none at /usr/bin/python3 -- the reference "
            "decoder needs an interpreter to decode anything at all")

    return Oracle("tree", root, python, script,
                  f"reference decoder at {script} (via {python}, diaggrok "
                  f"from {src})", src)


def import_diaggrok(name: str = "diaggrok.hdlc", tree: Path | None = None):
    """Import a module from the reference decoder, or return None.

    The TREE's decoder first, then an already-importable one. The tree's
    ``.deps/diaggrok`` is the reference the binary decodes with, so a harness
    that imported a different diaggrok in-process would be comparing the binary
    against a second program. Only when the tree has none does an ambient
    install stand in (a harness that needs framing only, e.g. HDLC).

    Returns ``None`` rather than raising, so the caller can skip with
    :meth:`Oracle.skip_reason` and the reason stays a single vocabulary.
    """
    from importlib import import_module

    src = decoder_src_in(_TREE_ROOT if tree is None else Path(tree))
    if src is not None and str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        return import_module(name)
    except ImportError:
        return None
