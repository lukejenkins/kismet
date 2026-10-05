#!/usr/bin/env python3
"""Compare the `functional` replay's observations against the expected set.

    python3 fixtures/check_functional.py <observed.jsonl> <expected.json>

Exit status
-----------
    0  the observed set is NON-EMPTY and matches the expected set
    1  a real mismatch (wrong values, wrong count, or -- the important one --
       an EMPTY observation set)
    2  usage

Why a separate comparator
-------------------------
The `functional` target's claim is *"this tree, on a fresh machine, decodes
its own canned capture into cell observations"*. Two things have to be asserted
to make that claim, and only asserting the second is the classic trap:

  1. **The set is non-empty.** A decoder that silently produces nothing exits 0
     and prints nothing, which `diff`-against-expected would also report as a
     pass if the expected file were ever empty. Non-emptiness is checked FIRST
     and on its own, so it can never be satisfied vacuously.
  2. **The values are right.** Non-empty alone would accept a decoder emitting
     four observations of garbage.

`prov.captured_at` is excluded from the comparison, and it is the only field
that is. It is host wall-clock stamped at replay time, so it differs on every
run and on every machine; pinning it would make the target fail for a reason
that has nothing to do with decode. `prov.log_tick` is not excluded: it comes from the fixture's own
synthetic DIAG ticks and is therefore deterministic, so it stays pinned and
keeps the record ORDER honest.
"""
from __future__ import annotations

import json
import sys

#: The one non-deterministic field. See the module docstring.
_VOLATILE = ("prov", "captured_at")


def _strip_volatile(obs: dict) -> dict:
    out = json.loads(json.dumps(obs))          # cheap deep copy
    parent, key = _VOLATILE
    if isinstance(out.get(parent), dict):
        out[parent].pop(key, None)
    return out


def _load_observed(path: str) -> list[dict]:
    """Read the bridge's JSONL stdout, keeping only cell observations.

    The bridge can also emit `gps_fix` and `diag_inventory` lines. The
    `functional` target passes --no-inventory and the fixture carries no GNSS
    record, so neither should appear -- but filtering rather than asserting
    their absence keeps this comparator honest if the fixture later grows a
    GNSS leg, instead of failing for an unrelated reason.
    """
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Not JSON at all -- a traceback or a warning on stdout. Do not
                # swallow it: it is exactly the kind of thing that turns into a
                # mystery empty set two steps later.
                print(f"check_functional: non-JSON line on stdout: {line[:200]}",
                      file=sys.stderr)
                return []
            if obj.get("type") in ("gps_fix", "diag_inventory", "diag_stats",
                                   "diag_census"):
                continue
            out.append(obj)
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    observed_path, expected_path = argv

    observed = _load_observed(observed_path)
    with open(expected_path, encoding="utf-8") as fh:
        expected = json.load(fh)

    # ── assertion 1: NON-EMPTY, checked alone and first ──────────────────────
    if not observed:
        print("FAIL: the replay produced ZERO cell observations.", file=sys.stderr)
        print("", file=sys.stderr)
        print("  This is the outcome the target exists to detect. The usual", file=sys.stderr)
        print("  cause is a decoder that does not carry the parsers the bridge", file=sys.stderr)
        print("  imports -- see `make -f standalone.mk deps` and DIAGGROK_REF.", file=sys.stderr)
        print("  It is NOT a fixture problem unless the round-trip test in", file=sys.stderr)
        print("  tests/test_functional_fixture.py is also failing.", file=sys.stderr)
        return 1

    # A guard on the expected file itself. An empty expectation would make
    # assertion 2 vacuous, and assertion 1 would already have returned above --
    # so this can only fire on a corrupted/truncated expected file.
    if not expected:
        print("FAIL: the expected file is empty; it cannot assert anything.",
              file=sys.stderr)
        return 1

    # ── assertion 2: the values are right ────────────────────────────────────
    obs_cmp = [_strip_volatile(o) for o in observed]
    exp_cmp = [_strip_volatile(o) for o in expected]

    if obs_cmp == exp_cmp:
        print(f"PASS: {len(obs_cmp)} cell observations, matching the expected set")
        return 0

    print(f"FAIL: observed {len(obs_cmp)} observations, expected {len(exp_cmp)}",
          file=sys.stderr)
    for i in range(max(len(obs_cmp), len(exp_cmp))):
        o = obs_cmp[i] if i < len(obs_cmp) else None
        e = exp_cmp[i] if i < len(exp_cmp) else None
        if o != e:
            print(f"  [{i}] expected: {json.dumps(e, sort_keys=True)}", file=sys.stderr)
            print(f"  [{i}] observed: {json.dumps(o, sort_keys=True)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
