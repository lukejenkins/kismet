#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Generate the golden observation fixtures for the native decode path.

## What a golden is

``$CELL_CAPTURES/goldens/<fixture>.ndjson`` is what Kismet receives when
``kismet_cap_cell_diag`` replays one committed capture with the Python bridge
decoding (``nativedecode=off``): one canonical line per relayed
``cell_observation``, with the GPS sub-block it rode in. The canonical form and
the replay both come from ``capture_cell_diag/celldiag_replay_ndjson``, the C
driver. The no-Python check (``capture_cell_diag/check_replay_goldens.sh``) runs
that same driver over the native path (``nativedecode=on``) and diffs. So one
program defines "the same", and only the decoder differs between the two runs.

A second stream, ``gps_fix`` (``<fixture>.gps_fix.txt``), pins the
``celldiag GPS fix <lat>,<lon> alt=<m>m src=<code>`` MESSAGE lines: the
positions the binary accepted. The binary intercepts the bridge's ``gps_fix``
JSON rather than relaying it. That message line and the GPS sub-block it
stamps onto later observations are what Kismet receives, and the sub-block is
pinned per row in the ``cell_observation`` body. Both lines carry no wall clock.
A fixture with no accepted fix pins an empty ``gps_fix`` body, so a native path
that invents a fix fails as well.

A third stream, ``diag_inventory`` (``<fixture>.diag_inventory.txt``), pins the
end-of-stream per-(code,version) census: the last ``celldiag inventory:``
MESSAGE and the last ``diag_census`` table, the census Kismet stores for the
source.

## Where it lives, and what pins it

The golden BODY sits beside its capture, in ``$CELL_CAPTURES/goldens/``, and is
not committed. It has the same sensitivity as the capture: GPS sub-blocks
carry bench coordinates, and every row carries the (EARFCN, PCI) of cells at a
known site, and the published tree must carry neither.
What is committed is the PIN: ``capture_cell_diag/goldens/MANIFEST.tsv``, one
row per fixture, holding the SHA-256 of the
**decompressed** capture, the SHA-256 and row count of the golden, and the
diaggrok revision that produced it. A body that does not hash to its committed
pin fails the check, so the golden cannot move without a reviewed commit to this
tree. The revision is the last commit touching
the decoder tree the binary actually resolved (``.deps/diaggrok`` under its
decoder root). A dirty decoder tree has no revision, so it is refused.

## Why regeneration refuses

A golden exists to catch the native path drifting from diaggrok. If
regenerating could quietly rewrite one, the next regression would be absorbed
the moment someone re-ran the generator. So:

* **Content changed, diaggrok revision unchanged**: REFUSED, with no override.
  The oracle did not move, so something else changed what the bridge emits
  (the binary, the bridge script, the capture). That is a regression to
  investigate, not a new truth.
* **Content changed, revision moved**: requires ``--moved-by <sha>`` naming a
  commit that is in ``old..new`` and touches the decoder tree, plus
  ``--reason``. Both are appended to ``capture_cell_diag/goldens/HISTORY.tsv``.
* **Capture hash changed**: requires ``--new-capture`` plus ``--reason``. The
  fixture itself was replaced.
* **New fixture**: requires ``--add``.
* **Content identical**: the manifest is re-pinned to the current revision
  with no ceremony.

## Guards

The bridge must decode with THIS tree's diaggrok. The binary is refused if
``celldiag_parity.decoder_root_from`` finds no tree decoder above it, if it is
stale against its sources (``celldiag_parity.staleness``), or if its own
"decoder root:" report names the installed data dir.

Dev-only: this generator needs python3 and a decoder. The check it feeds
needs neither.

Usage:
    python3 celltools/celldiag_goldens.py status
    python3 celltools/celldiag_goldens.py generate --add            # first time
    python3 celltools/celldiag_goldens.py generate --fixture NAME \\
        --moved-by <sha> --reason "0xB193 v18 rsrp now decoded (issue NNNN)"
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import celldiag_parity  # noqa: E402

TREE = HERE.parent
CELLDIAG_DIR = TREE / "capture_cell_diag"
#: The committed pins. The bodies live in ``body_dir(captures)``.
PIN_DIR = CELLDIAG_DIR / "goldens"
MANIFEST = PIN_DIR / "MANIFEST.tsv"
HISTORY = PIN_DIR / "HISTORY.tsv"


def body_dir(captures: Path) -> Path:
    return captures / "goldens"
DRIVER = CELLDIAG_DIR / "celldiag_replay_ndjson"

#: The replay captures the goldens cover, listed beside the pins in
#: ``goldens/FIXTURES.tsv`` as ``label <TAB> path <TAB> what it covers``. Each
#: path is named as it sits in $CELL_CAPTURES (and may be stored ``.zst``).
#: The list lives with the ledger because those paths name directories of a
#: corpus the tree does not ship. With no ledger there are no fixtures, and
#: every check that would replay one skips.
FIXTURE_LIST = PIN_DIR / "FIXTURES.tsv"


def read_fixtures(path: Path = FIXTURE_LIST) -> dict[str, str]:
    """``label -> capture path``, in file order; empty when there is no list."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for ln in path.read_text().splitlines():
        if not ln.strip() or ln.startswith("#"):
            continue
        label, capture = ln.split("\t")[:2]
        out[label] = capture
    return out


FIXTURE_BY_LABEL = read_fixtures()
FIXTURES = tuple(FIXTURE_BY_LABEL.values())

#: The synthetic IMEI every replay is stamped with (the A/B harness's IMEI).
IMEI = "000000000000000"

STREAM = "cell_observation"
GPS_STREAM = "gps_fix"
INVENTORY_STREAM = "diag_inventory"
STREAMS = (STREAM, GPS_STREAM, INVENTORY_STREAM)
_INVENTORY_MESSAGE = "message: celldiag inventory: "
_CENSUS = "census: "
_GPS_MESSAGE = "message: celldiag GPS fix "


def body_name(fixture: str, stream: str) -> str:
    """The body's file name in ``body_dir``. check_replay_goldens.sh's body_of()
    spells the same rule."""
    base = fixture.replace("/", "__")
    return f"{base}.ndjson" if stream == STREAM else f"{base}.{stream}.txt"


def inventory_lines(driver_stderr: str) -> str:
    """The END-of-stream inventory: the last ``celldiag inventory:`` MESSAGE and
    the last ``diag_census`` table. Only the last of each: the census also goes
    out periodically on a wall-clock interval, so how many interim copies a
    replay produces depends on how fast the host ran it. The final one counts
    the whole capture."""
    msg = cen = None
    for ln in driver_stderr.splitlines():
        if ln.startswith(_INVENTORY_MESSAGE):
            msg = ln[len("message: "):]
        elif ln.startswith(_CENSUS):
            cen = ln
    return "".join(x + "\n" for x in (msg, cen) if x is not None)


def gps_fix_lines(driver_stderr: str) -> str:
    return "".join(ln[len("message: "):] + "\n" for ln in driver_stderr.splitlines()
                   if ln.startswith(_GPS_MESSAGE))

MANIFEST_HEADER = ("# fixture\tstream\tcapture_sha256\tgolden_sha256\trows\tdiaggrok_rev"
                   "\tkp_rev\n")
HISTORY_HEADER = ("# utc\tfixture\tstream\told_golden_sha256\tnew_golden_sha256\t"
                  "old_diaggrok_rev\tnew_diaggrok_rev\tmoved_by\treason\n")

_DECODER_ROOT = "message: decoder root: "
_TREE_ORIGIN = "(this binary's own tree)"


class Refused(Exception):
    """A regeneration the rules above do not allow. The message says why."""


@dataclass(frozen=True)
class Entry:
    fixture: str
    stream: str
    capture_sha256: str
    golden_sha256: str
    rows: int
    diaggrok_rev: str
    #: The commit of this tree the bridge binary was built from. Provenance only: it does
    #: not take part in :func:`decide`, which keys on the decoder revision.
    #: "-" for pins written before it was recorded.
    kp_rev: str = "-"

    def tsv(self) -> str:
        return "\t".join((self.fixture, self.stream, self.capture_sha256,
                          self.golden_sha256, str(self.rows), self.diaggrok_rev,
                          self.kp_rev)) + "\n"


def read_manifest(path: Path = MANIFEST) -> dict[tuple[str, str], Entry]:
    out: dict[tuple[str, str], Entry] = {}
    if not path.exists():
        return out
    for ln in path.read_text().splitlines():
        if not ln.strip() or ln.startswith("#"):
            continue
        cols = ln.split("\t")
        f, s, cap, gold, rows, rev = cols[:6]
        out[(f, s)] = Entry(f, s, cap, gold, int(rows), rev,
                            cols[6] if len(cols) > 6 else "-")
    return out


def write_manifest(entries: dict[tuple[str, str], Entry], path: Path = MANIFEST) -> None:
    body = "".join(entries[k].tsv() for k in sorted(entries))
    path.write_text(MANIFEST_HEADER + body)


def append_history(row: list[str], path: Path = HISTORY) -> None:
    if not path.exists():
        path.write_text(HISTORY_HEADER)
    with path.open("a") as fp:
        fp.write("\t".join(c.replace("\t", " ").replace("\n", " ") for c in row) + "\n")


# ── the decision, kept pure so its every branch is unit-testable ───────────────
@dataclass(frozen=True)
class Request:
    add: bool = False
    new_capture: bool = False
    moved_by: str | None = None
    reason: str | None = None


def decide(old: Entry | None, new: Entry, req: Request,
           commit_moves_decoder) -> str:
    """Whether ``new`` may replace ``old``. Returns the history note to record
    ("" when nothing needs recording), or raises :class:`Refused`.

    ``commit_moves_decoder(sha, old_rev, new_rev) -> str | None`` answers
    whether ``sha`` is a decoder commit in ``old_rev..new_rev``. It returns None
    if so, else the reason it is not. It is injected so the rules can be tested
    without a repository.
    """
    if old is None:
        if not req.add:
            raise Refused(f"{new.fixture}: no golden yet -- pass --add to create one")
        return "added"
    if new.capture_sha256 != old.capture_sha256:
        if not (req.new_capture and req.reason):
            raise Refused(
                f"{new.fixture}: the capture itself changed "
                f"({old.capture_sha256[:12]} -> {new.capture_sha256[:12]}). "
                f"If the fixture was deliberately replaced, pass --new-capture "
                f"--reason '...'")
        return "new capture"
    if new.golden_sha256 == old.golden_sha256:
        return ""                                   # re-pin only
    if new.diaggrok_rev == old.diaggrok_rev:
        raise Refused(
            f"{new.fixture}: the golden would change ({old.rows} -> {new.rows} "
            f"rows, {old.golden_sha256[:12]} -> {new.golden_sha256[:12]}) but "
            f"the diaggrok revision did not ({new.diaggrok_rev[:12]}). The oracle "
            f"did not move, so something else changed what the bridge emits. "
            f"That is a regression to find, and a golden may not absorb it. "
            f"No flag overrides this.")
    if not (req.moved_by and req.reason):
        raise Refused(
            f"{new.fixture}: diaggrok moved {old.diaggrok_rev[:12]} -> "
            f"{new.diaggrok_rev[:12]} and the golden changes with it. Say which "
            f"commit moved it: --moved-by <sha> --reason '...'")
    why_not = commit_moves_decoder(req.moved_by, old.diaggrok_rev, new.diaggrok_rev)
    if why_not is not None:
        raise Refused(f"{new.fixture}: --moved-by {req.moved_by}: {why_not}")
    return "moved"


# ── git facts about the decoder tree ───────────────────────────────────────────
def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def decoder_rev(diaggrok: Path) -> str:
    """The last commit touching the decoder tree. Refuses a dirty tree."""
    dirty = _git(diaggrok, "status", "--porcelain", "--", ".")
    if dirty.returncode != 0:
        raise Refused(f"{diaggrok} is not in a git work tree, so it has no revision")
    if dirty.stdout.strip():
        raise Refused(f"{diaggrok} has uncommitted changes, so no revision "
                      f"describes the decoder that would produce the golden")
    r = _git(diaggrok, "log", "-1", "--format=%H", "--", ".")
    if r.returncode != 0 or not r.stdout.strip():
        raise Refused(f"no commit touches {diaggrok}")
    return r.stdout.strip()


#: The tracked paths a bridge binary is built from. Untracked files are ignored:
#: build products and another session's new, unwired files are not inputs.
#: ``capture_cell_diag`` covers its Makefile.in and standalone.mk;
#: ``celltools/Makefile`` is listed because its replay-gate-build makes the pair.
_BUILD_INPUTS = ("capture_cell_diag", "diagspec", "Makefile.in", "configure",
                 "celltools/Makefile")


def build_tree_rev(tree: Path = TREE) -> str:
    """The commit of this tree the binary's sources are at. Refuses uncommitted edits to
    them, since a golden made from a binary with someone's WIP baked in pins
    behaviour no commit describes. The goldens' own pins are excluded."""
    r = _git(tree, "status", "--porcelain", "--untracked-files=no", "--", *_BUILD_INPUTS)
    if r.returncode != 0:
        raise Refused(f"{tree} is not a git work tree, so the binary has no source revision")
    dirty = [ln[3:] for ln in r.stdout.splitlines()
             if ln[3:] and not ln[3:].startswith("capture_cell_diag/goldens/")]
    if dirty:
        raise Refused(f"the binary's sources have uncommitted edits ({', '.join(dirty[:5])}"
                      f"{' ...' if len(dirty) > 5 else ''}); commit or wait for them first")
    return _git(tree, "rev-parse", "HEAD").stdout.strip()


def commit_moves_decoder_in(diaggrok: Path):
    def check(sha: str, old_rev: str, new_rev: str) -> str | None:
        full = _git(diaggrok, "rev-parse", "--verify", f"{sha}^{{commit}}")
        if full.returncode != 0:
            return "is not a commit in the decoder's repository"
        full_sha = full.stdout.strip()
        if _git(diaggrok, "merge-base", "--is-ancestor", old_rev, full_sha).returncode != 0 \
                or full_sha == old_rev:
            return f"is not after the golden's revision {old_rev[:12]}"
        if _git(diaggrok, "merge-base", "--is-ancestor", full_sha, new_rev).returncode != 0:
            return f"is not part of the current revision {new_rev[:12]}"
        touched = _git(diaggrok, "diff-tree", "--no-commit-id", "--name-only", "-r",
                       full_sha, "--", ".")
        if not touched.stdout.strip():
            return "does not touch the decoder tree"
        return None
    return check


# ── producing a candidate golden ───────────────────────────────────────────────
def capture_path(captures: Path, fixture: str) -> Path:
    for cand in (captures / fixture, captures / f"{fixture}.zst"):
        if cand.exists():
            return cand
    raise Refused(f"{fixture}: not found in {captures} (plain or .zst)")


def materialize(src: Path, dst: Path) -> None:
    """Decompress with the zstd CLI. The no-Python check uses it too, so the
    two decompress identically, and this module stays stdlib-only."""
    if src.suffix == ".zst":
        with dst.open("wb") as out:
            # On stdin: zstd skips a symlinked input named on its command line.
            with src.open("rb") as inp:
                subprocess.run(["zstd", "-dqc"], stdin=inp, stdout=out, check=True)
    else:
        dst.write_bytes(src.read_bytes())


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_bridge_binary(binary: Path) -> Path:
    """The decoder root ``binary`` will use. Refuses a binary that would decode with the wrong diaggrok."""
    root = celldiag_parity.decoder_root_from(binary.resolve().parent)
    if root is None:
        raise Refused(
            f"{binary}: no .deps/diaggrok above it, so its bridge would decode "
            f"with the INSTALLED diaggrok, of unknown age. Use a binary "
            f"under capture_cell_diag/.")
    st = celldiag_parity.staleness("celldiag", binary)
    if st.stale:
        raise Refused(f"{binary} is older than its sources ({st.summary()}): "
                      f"{st.rebuild_cmd()}")
    return root


def replay(binary: Path, capture: Path, mode: str, timeout: float = 600) -> tuple[str, str]:
    """(canonical ndjson, driver stderr) for one replay through the C driver."""
    if not DRIVER.exists():
        raise Refused(f"{DRIVER} is not built: make -C {CELLDIAG_DIR} replay-ndjson")
    definition = f"celldiag-{IMEI}:replay={capture},nativedecode={mode}"
    p = subprocess.run([str(DRIVER), "--timeout", str(timeout), str(binary), definition],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise Refused(f"replay of {capture.name} failed (exit {p.returncode}):\n{p.stderr[-2000:]}")
    roots = [ln for ln in p.stderr.splitlines() if ln.startswith(_DECODER_ROOT)]
    if not roots:
        raise Refused(f"{binary} never reported its decoder root, so which diaggrok "
                      f"decoded {capture.name} is unknown. Rebuild it.")
    if not all(_TREE_ORIGIN in ln for ln in roots):
        raise Refused(f"{binary} decoded with a decoder outside its tree: {roots[0]!r}")
    return p.stdout, p.stderr


def generate(fixtures: list[str], captures: Path, binary: Path, req: Request,
             dry_run: bool = False, streams: list[str] | None = None) -> int:
    root = check_bridge_binary(binary)
    diaggrok = (root / ".deps" / "diaggrok").resolve()
    rev = decoder_rev(diaggrok)
    kp_rev = build_tree_rev()
    entries = read_manifest()
    moves = commit_moves_decoder_in(diaggrok)
    bodies = body_dir(captures)
    if not dry_run:
        bodies.mkdir(exist_ok=True)
        PIN_DIR.mkdir(exist_ok=True)
    refused = 0
    with tempfile.TemporaryDirectory(prefix="celldiag_goldens_") as td:
        for fx in fixtures:
            raw = Path(td) / "replay.raw"
            materialize(capture_path(captures, fx), raw)
            ndjson, err = replay(binary, raw, "off")
            if ndjson.count("\n") == 0:
                print(f"REFUSED {fx}: the bridge relayed 0 observations -- a bad "
                      f"fixture or a silent bridge, never a golden")
                refused += 1
                continue
            cap_sha = sha256_file(raw)
            for stream, body in ((STREAM, ndjson), (GPS_STREAM, gps_fix_lines(err)),
                                 (INVENTORY_STREAM, inventory_lines(err))):
                if streams is not None and stream not in streams:
                    continue
                rows = body.count("\n")
                new = Entry(fx, stream, cap_sha,
                            hashlib.sha256(body.encode()).hexdigest(), rows, rev, kp_rev)
                old = entries.get((fx, stream))
                try:
                    note = decide(old, new, req, moves)
                except Refused as e:
                    print(f"REFUSED [{stream}] {e}")
                    refused += 1
                    continue
                verb = {"": "re-pinned" if old and old.diaggrok_rev != rev else "unchanged",
                        "added": "added", "new capture": "replaced (new capture)",
                        "moved": "moved"}[note]
                print(f"{verb:24s} {fx} [{stream}]: {rows} rows, golden "
                      f"{new.golden_sha256[:12]}, diaggrok {rev[:12]}")
                if dry_run:
                    continue
                (bodies / body_name(fx, stream)).write_text(body)
                entries[(fx, stream)] = new
                if note:
                    append_history([
                        _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        fx, stream,
                        old.golden_sha256 if old else "-", new.golden_sha256,
                        old.diaggrok_rev if old else "-", rev,
                        req.moved_by or "-", f"{req.reason or note} [tree {kp_rev[:12]}]"])
    if not dry_run:
        write_manifest(entries)
    return 1 if refused else 0


def status(captures: Path | None) -> int:
    entries = read_manifest()
    if not entries:
        print(f"no goldens yet ({MANIFEST} absent)")
        return 0
    for (fx, stream), e in sorted(entries.items()):
        if captures is None:
            mark = "?  "                 # the body is not reachable without the corpus
        else:
            g = body_dir(captures) / body_name(fx, stream)
            ok = g.exists() and hashlib.sha256(g.read_bytes()).hexdigest() == e.golden_sha256
            mark = "ok " if ok else "BAD"
        print(f"{mark} {fx} [{stream}] {e.rows} rows, "
              f"capture {e.capture_sha256[:12]}, diaggrok {e.diaggrok_rev[:12]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status", help="list the pins and whether each body matches")
    st.add_argument("--captures", type=Path, default=os.environ.get("CELL_CAPTURES"))
    g = sub.add_parser("generate", help="(re)generate goldens from the bridge")
    g.add_argument("--fixture", action="append", choices=FIXTURES,
                   help="limit to this fixture (repeatable); default: all")
    g.add_argument("--captures", type=Path, default=os.environ.get("CELL_CAPTURES"),
                   help="capture directory (default: $CELL_CAPTURES)")
    g.add_argument("--binary", type=Path, default=CELLDIAG_DIR / "kismet_cap_cell_diag",
                   help="the kismet_cap_cell_diag to replay (either build mode)")
    g.add_argument("--add", action="store_true", help="allow creating a missing golden")
    g.add_argument("--new-capture", action="store_true",
                   help="allow a fixture whose capture hash changed (needs --reason)")
    g.add_argument("--moved-by", help="the decoder commit that changed the golden")
    g.add_argument("--reason", help="why, recorded in capture_cell_diag/goldens/HISTORY.tsv")
    g.add_argument("--stream", action="append", choices=STREAMS,
                   help="limit to this stream (repeatable); default: all")
    g.add_argument("--dry-run", action="store_true", help="decide and report; write nothing")
    args = ap.parse_args(argv)
    if args.cmd == "status":
        return status(Path(args.captures) if args.captures else None)
    if args.captures is None:
        print("no capture directory: pass --captures or set CELL_CAPTURES", file=sys.stderr)
        return 2
    try:
        return generate(list(args.fixture or FIXTURES), Path(args.captures), args.binary,
                        Request(args.add, args.new_capture, args.moved_by, args.reason),
                        dry_run=args.dry_run, streams=args.stream)
    except Refused as e:
        print(f"REFUSED {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
