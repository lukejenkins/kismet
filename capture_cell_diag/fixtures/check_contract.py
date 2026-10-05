#!/usr/bin/env python3
"""Assert the bridge's CENSUS output satisfies the bridge/diaggrok runtime contract.

    python3 fixtures/check_contract.py <observed.jsonl>

This is the comparator for the `contract` make target. Where
`check_functional.py` proves the *cell_observation* stream is right, this
proves the *diag_inventory* census line the bridge renders at `finish()` is
complete. That census line is the one place the bridge reads fields straight
out of diaggrok's `DiagInventory.summary()`, so it is where a bridge that has
outrun its pinned decoder crashes.

Exit status
-----------
    0  a census line is present AND carries every contract field
    1  a real contract violation (no census line at all, or a census line
       missing one of the contract fields)
    2  usage

Why this check exists, and why `functional` cannot see it
---------------------------------------------------------
If the bridge reads a census field (e.g. ``silent_actionable_total``) that the
pinned diaggrok release does not yet produce, the bridge decodes every record
and then crashes at `finish()` with exit 70. The SHA guard in `deps` catches a
tag that moved; it cannot catch a bridge that outran a frozen library, and the
build and compile checks never run the decoder.

`functional` does run the pinned decoder end to end, but it passes
``--no-inventory`` so its stdout is pure ``cell_observation`` lines for a
byte-diff, and that sets ``inventory=None``, which skips exactly the census
``finish()`` path where the contract fields are read. The `contract` target
runs the bridge with the census on so `finish()` fires.

The load-bearing assertions are the negative controls. This comparator must
fail on (a) an empty stream (the census never ran, e.g. `--no-inventory` crept
back in) and (b) a census line that rendered but dropped
``silent_actionable_total`` (e.g. a bridge patched to tolerate the missing
field with ``.get(..., None)`` instead of re-pinning the decoder). A
comparator that could only ever return 0 would prove nothing. Both cases are
exercised in tests/test_contract_fixture.py.
"""
from __future__ import annotations

import json
import sys

#: The fields the bridge's census wire record (`kismet_diag_decode.py`, the
#: ``diag_inventory`` line) reads out of ``diaggrok.inventory.DiagInventory``'s
#: summary. Each is a `snap[...]` subscript in the bridge, so a diaggrok that
#: does not produce it makes the bridge raise ``KeyError`` -> exit 70. This list
#: is the runtime contract. ``silent_actionable_total`` is the newest, but
#: pinning only it would let the next dropped sibling through, so all six are
#: asserted.
CONTRACT_FIELDS = (
    "distinct",
    "overflow",
    "unrecognized_total",
    "silent_total",
    "silent_actionable_total",
    "enrich_failed_total",
)

#: The census line's own discriminator (see the bridge's `_DiagInventoryWire`).
CENSUS_TYPE = "diag_inventory"


def _load_census_lines(path: str) -> list[dict] | None:
    """Return the ``diag_inventory`` census objects on the bridge's stdout.

    Returns ``None`` (distinct from ``[]``) if a non-JSON line is seen -- a
    traceback or warning on stdout is precisely the mystery this gate must not
    swallow into an "empty census" two steps later.
    """
    census: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                print(f"check_contract: non-JSON line on stdout: {line[:200]}",
                      file=sys.stderr)
                return None
            if isinstance(obj, dict) and obj.get("type") == CENSUS_TYPE:
                census.append(obj)
    return census


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    (observed_path,) = argv

    census = _load_census_lines(observed_path)
    if census is None:
        print("FAIL: non-JSON on the bridge's stdout; refusing to read it as a "
              "census. See the line dumped above.", file=sys.stderr)
        return 1

    # ── assertion 1: the census ran, checked alone and first ─────────────────
    # A crashing bridge exits 70 and the make recipe fails before this runs;
    # what reaches here with an empty census is a bridge that decoded fine but
    # never emitted the census -- `--no-inventory` crept back into the target,
    # or `finish()` stopped firing the final snapshot. Either makes this check
    # vacuous, so it is rejected on its own, ahead of any field check.
    if not census:
        print("FAIL: the replay produced NO diag_inventory census line.",
              file=sys.stderr)
        print("", file=sys.stderr)
        print("  This check measures the census `finish()` path -- the one",
              file=sys.stderr)
        print("  place the bridge reads fields straight out of the pinned",
              file=sys.stderr)
        print("  diaggrok. No census line means that path never ran, so this",
              file=sys.stderr)
        print("  check would pass vacuously. The usual cause is `--no-inventory`",
              file=sys.stderr)
        print("  on the bridge invocation (the `functional` target passes it;",
              file=sys.stderr)
        print("  `contract` must not). This is not a contract violation by",
              file=sys.stderr)
        print("  itself -- it is the check being unable to ask its question.",
              file=sys.stderr)
        return 1

    # ── assertion 2: every census line carries every contract field ──────────
    ok = True
    for i, line in enumerate(census):
        missing = [f for f in CONTRACT_FIELDS if f not in line]
        if missing:
            ok = False
            print(f"FAIL: diag_inventory line [{i}] is missing contract "
                  f"field(s): {', '.join(missing)}", file=sys.stderr)
            print("", file=sys.stderr)
            print("  The bridge reads these straight from diaggrok's summary.",
                  file=sys.stderr)
            print("  A live crash on a missing field is exit 70 (caught before",
                  file=sys.stderr)
            print("  this comparator); a rendered census line missing the field",
                  file=sys.stderr)
            print("  means the bridge was patched to tolerate the gap instead",
                  file=sys.stderr)
            print("  of re-pinning diaggrok -- which silently drops the count",
                  file=sys.stderr)
            print("  the C relay escalates on. Re-pin, don't paper over it.",
                  file=sys.stderr)
    if not ok:
        return 1

    print(f"PASS: {len(census)} diag_inventory census line(s), each carrying "
          f"all {len(CONTRACT_FIELDS)} contract fields "
          f"(incl. silent_actionable_total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
