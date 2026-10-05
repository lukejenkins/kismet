"""The census-contract comparator, `fixtures/check_contract.py`.

These tests need no decoder and no network: they pin the comparator's pure
logic. The end-to-end claim (the bridge's `finish()` census through the pinned
diaggrok exits 0) is the `contract` makefile target's job, and it is
deliberately not duplicated here: a pytest that skipped or failed on the same
condition would turn a loud failure of that target into a silent yellow.

The load-bearing tests in this module are the negative controls
(`test_an_empty_stream_is_rejected` and
`test_a_census_line_missing_the_contract_field_is_rejected`). The comparator's
entire job is to reject a census that never ran or a census line that dropped
a contract field. A comparator that could only ever return 0 would satisfy
every positive test here while proving nothing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
FIXTURES = HERE / "fixtures"

sys.path.insert(0, str(FIXTURES))
import check_contract  # noqa: E402


def _write(tmp_path: Path, lines: list) -> str:
    """Write JSONL (each item dumped on its own line; str items written raw so a
    test can inject a deliberately non-JSON line) and return the path."""
    p = tmp_path / "observed.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(ln if isinstance(ln, str) else json.dumps(ln))
            fh.write("\n")
    return str(p)


def _census(**overrides) -> dict:
    """A well-formed census line carrying every contract field. Tests remove or
    corrupt fields from this baseline rather than hand-building each case, so a
    field ADDED to CONTRACT_FIELDS later cannot leave a test silently checking a
    census the comparator now considers incomplete."""
    line = {"type": "diag_inventory", "ts": 1.0}
    for f in check_contract.CONTRACT_FIELDS:
        line[f] = 0
    line.update(overrides)
    return line


def _incomplete() -> dict:
    """A census line missing `silent_actionable_total` -- factored out so the
    'one bad among good' test can inject it without an inline expression."""
    line = _census()
    del line["silent_actionable_total"]
    return line


# ── the comparator is not vacuous: a passing baseline exists ─────────────────

def test_the_baseline_census_carries_every_contract_field():
    """The positive control for the test helper itself. Without it, every
    negative test below could pass against a `_census()` that was already
    incomplete -- and against a comparator that returns 1 unconditionally."""
    line = _census()
    for f in check_contract.CONTRACT_FIELDS:
        assert f in line


def test_a_complete_census_passes(tmp_path):
    obs = _write(tmp_path, [{"type": "cell_observation", "pci": 1}, _census()])
    assert check_contract.main([obs]) == 0


def test_the_contract_fields_include_silent_actionable_total():
    """A regression guard on the contract list itself. `silent_actionable_total`
    is a field whose absence from the pinned diaggrok crashes the bridge at
    finish(); dropping it from CONTRACT_FIELDS would make the `contract` target
    green on the very break it exists to catch."""
    assert "silent_actionable_total" in check_contract.CONTRACT_FIELDS


# ── the load-bearing negative controls ───────────────────────────────────────

def test_an_empty_stream_is_rejected(tmp_path):
    """No census line at all -- the census path never ran (e.g. `--no-inventory`
    crept back into the target). The check would pass vacuously, so this must
    be red."""
    obs = _write(tmp_path, [])
    assert check_contract.main([obs]) == 1


def test_a_stream_with_observations_but_no_census_is_rejected(tmp_path):
    """The subtler empty case: the decode produced cell observations (looks
    healthy) but no diag_inventory line. Still vacuous for the contract check."""
    obs = _write(tmp_path, [{"type": "cell_observation", "pci": 1},
                            {"type": "cell_observation", "pci": 2}])
    assert check_contract.main([obs]) == 1


def test_a_census_line_missing_the_contract_field_is_rejected(tmp_path):
    """The pinned-decoder break as data: a census line that rendered but dropped
    `silent_actionable_total` -- e.g. a bridge patched to tolerate the missing
    field instead of re-pinning diaggrok. Must be red."""
    bad = _census()
    del bad["silent_actionable_total"]
    obs = _write(tmp_path, [bad])
    assert check_contract.main([obs]) == 1


def test_a_census_line_missing_any_sibling_count_is_rejected(tmp_path):
    """Every contract field is asserted, not just the newest one -- pinning
    only `silent_actionable_total` would let the next dropped sibling through.
    Parametrised across the whole set so a field added later is covered for free.
    """
    for field in check_contract.CONTRACT_FIELDS:
        bad = _census()
        del bad[field]
        obs = _write(tmp_path, [bad])
        assert check_contract.main([obs]) == 1, f"{field} dropped but accepted"


def test_one_bad_census_among_good_ones_is_rejected(tmp_path):
    """A periodic census that renders fine for most flushes but drops the field
    on one is still a contract violation. Every line is checked, not just the
    first or last."""
    obs = _write(tmp_path, [_census(), _incomplete(), _census()])
    assert check_contract.main([obs]) == 1


def test_a_traceback_on_stdout_is_not_read_as_an_empty_census(tmp_path):
    """A non-JSON line (a Python traceback leaking onto stdout) must be surfaced
    as a failure, not silently filtered into an "empty census" that then reads
    as a different, more confusing error two steps later."""
    obs = _write(tmp_path, ['Traceback (most recent call last):',
                            '  KeyError: ...'])
    assert check_contract.main([obs]) == 1


def test_wrong_argument_count_is_a_usage_error():
    """Usage (exit 2) is distinct from a contract failure (exit 1): a harness
    that mis-invokes the comparator must not read as a green or a red decode."""
    assert check_contract.main([]) == 2
    assert check_contract.main(["a", "b"]) == 2
