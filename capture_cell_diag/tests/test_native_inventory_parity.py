"""Differential test: the native census (diag_inventory.c) against the bridge's.

The ``diag_inventory`` line has two producers now -- the bridge's
``DiagInventory`` (``kismet_diag_decode.py`` over ``diaggrok.inventory``) and
the C census the capture source runs without the bridge. Both are fed the
SAME record stream here and every wire line must be byte-identical: key
order, the ``ts`` float, the ``status`` one-liner, the flagged-first
``table``, the bounded arrays and their ``*_total`` counts.

The C side runs as ``test_diag_inventory --trace``, built by ``make check`` (or
``make test_diag_inventory``) in this directory. The streams are synthetic:
codes from the emit contract, the never-emits code, the identity and config
codes, and enough random codes and versions to reach both tiers of the bound.
Real captures are compared in ``celltools/tests/test_celldiag_native_census.py``.

Run with the decoder library fetched and on the path::

    make -f standalone.mk deps
    make test_diag_inventory
    PYTHONPATH=.deps/diaggrok/src python3 -m pytest tests/test_native_inventory_parity.py
"""
from __future__ import annotations

import importlib.util
import json
import random
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
TRACE_BIN = HERE / "test_diag_inventory"


def _load_bridge():
    path = HERE / "kismet_diag_decode.py"
    spec = importlib.util.spec_from_file_location("kismet_diag_decode", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kdd = _load_bridge()

#: The codes a census actually meets on the wardrive mask, plus two that sit
#: either side of the contract (0xB821 decodes and never self-emits; 0xB197
#: is config).
_REAL_CODES = (0xB192, 0xB193, 0xB195, 0xB0C0, 0xB97F, 0x1476, 0x14D8,
               0xB821, 0xB197, 0xB17F)


def _trace_or_skip() -> Path:
    if not TRACE_BIN.exists():
        pytest.skip(f"{TRACE_BIN.name} is not built: run `make "
                    f"{TRACE_BIN.name}` (or `make check`) in {HERE}")
    return TRACE_BIN


def _native(commands: list[str]) -> list[str]:
    out = subprocess.run([str(_trace_or_skip()), "--trace"],
                         input="\n".join(commands) + "\n", text=True,
                         capture_output=True, check=True)
    return out.stdout.splitlines()


def _run_both(stream, max_codes=512, max_versions=16):
    """Feed one stream of ("R", code, ver, decoded, emitted, enrich) and
    ("W", now) items to both censuses; return (python_lines, native_lines)."""
    inv = kdd.DiagInventory(max_codes=max_codes,
                            max_versions_per_code=max_versions)
    want, cmds = [], [f"C {max_codes} {max_versions}"]
    for item in stream:
        if item[0] == "R":
            _, code, ver, dec, em, enr = item
            inv.record(code, ver, dec, 0.0, emitted=em, enrich_failed=enr)
            cmds.append(f"R {code} {ver} {int(dec)} {em} {int(enr)}")
        else:
            now = item[1]
            want.append(json.dumps(inv.summary(now, wire=True),
                                   separators=(",", ":")))
            cmds.append(f"W {now!r}")
    return want, _native(cmds)


def _random_stream(rng: random.Random, n: int, code_pool: int):
    codes = list(_REAL_CODES) + [rng.randrange(0x1000, 0x2000)
                                 for _ in range(code_pool)]
    now = 1_792_532_000.0 + rng.random() * 1000
    for _ in range(n):
        code = rng.choice(codes)
        # Byte-0 as a real version (few values) on the real codes, as DATA
        # (any byte) on the random ones -- the case the per-code cap guards.
        ver = (rng.choice((-1, 1, 2, 27)) if code in _REAL_CODES
               else rng.randrange(-1, 256))
        dec = rng.random() > 0.15
        em = rng.choice((0, 0, 1, 2, 7)) if dec else 0
        enr = dec and rng.random() < 0.03
        yield ("R", code, ver, dec, em, enr)
        if rng.random() < 0.02:
            now += rng.random() * 37
            yield ("W", now)
    yield ("W", now + rng.random())


@pytest.mark.parametrize("seed", range(12))
def test_random_streams_render_byte_identically(seed):
    rng = random.Random(seed)
    # Alternate a small pool (no cap reached) with a large one (both tiers).
    pool = 3 if seed % 2 == 0 else 700
    want, got = _run_both(_random_stream(rng, 6000, pool))
    assert len(got) == len(want)
    for i, (w, g) in enumerate(zip(want, got)):
        assert g == w, f"census line {i} diverged\n py: {w}\n  c: {g}"


def test_tight_caps_render_byte_identically():
    """Both tiers at small caps, where every record past them is overflow."""
    rng = random.Random(99)
    want, got = _run_both(_random_stream(rng, 3000, 40),
                          max_codes=9, max_versions=3)
    assert got == want
    assert '"overflow":0,' not in want[-1], "the stream never reached a cap"


def test_every_flag_list_is_cut_and_still_counted():
    """More than WIRE_LIST_MAX entries in every list at once, so the cut, the
    "+N more" and the totals are all on one line."""
    stream = []
    for v in range(40):                       # unrecognized: decoded == 0
        stream.append(("R", 0x1100 + v, 0, False, 0, False))
    for v in range(40):                       # enrich_failed
        stream.append(("R", 0x1200 + v, 1, True, 1, True))
    for code in (0xB192, 0xB193, 0xB195, 0xB0C0, 0xB97F, 0x1476, 0x14D8):
        for v in range(16):                   # silent: 7 codes x 16 versions
            stream.append(("R", code, v, True, 0, False))
    stream.append(("W", 1_792_532_140.7484))
    want, got = _run_both(stream)
    assert got == want
    line = json.loads(want[0])
    assert line["silent_total"] == 112 and len(line["silent"]) == 32
    assert line["silent_actionable_total"] == 96


def test_the_contract_sets_match_diaggrok():
    """The C copies of EMIT_CONTRACT_CODES / NEVER_EMITS_CODES, member for
    member: a code that joins one set in diaggrok must join it here, or the
    two censuses disagree about `silent` on exactly that code."""
    from diaggrok.inventory import EMIT_CONTRACT_CODES, NEVER_EMITS_CODES
    src = (HERE / "diag_inventory.c").read_text()

    def c_set(name):
        body = src.split(f"{name}[] = {{", 1)[1].split("}", 1)[0]
        return {int(tok, 16) for tok in body.replace(",", " ").split()}

    assert c_set("DIAG_INVENTORY_EMIT_CONTRACT_CODES") == set(EMIT_CONTRACT_CODES)
    assert c_set("DIAG_INVENTORY_NEVER_EMITS_CODES") == set(NEVER_EMITS_CODES)


# ── the native-only undecoded_native state ─────────────────────────────────────
def test_armed_native_coverage_is_the_python_line_plus_its_own_fields():
    """With undecoded_native armed and nothing undecoded, the line is the
    Python line with exactly two additions -- the zero total and the empty
    array. Everything else the relay reads is untouched by arming it."""
    rng = random.Random(7)
    stream = list(_random_stream(rng, 4000, 700))
    want, _ = _run_both(stream)
    cmds = ["N"]
    for item in stream:
        if item[0] == "R":
            _, code, ver, dec, em, enr = item
            cmds.append(f"R {code} {ver} {int(dec)} {em} {int(enr)}")
        else:
            cmds.append(f"W {item[1]!r}")
    got = _native(cmds)
    assert len(got) == len(want)
    for w, g in zip(want, got):
        assert '"undecoded_native_total":0,' in g and g.endswith(',"undecoded_native":[]}')
        stripped = (g.replace('"undecoded_native_total":0,', "", 1)
                     .replace(',"undecoded_native":[]}', "}", 1))
        # The total sits after enrich_failed_total; without it the key order
        # is Python's again.
        assert stripped == w


def test_the_diaggrok_code_table_is_current():
    """diag_inventory_codes.h is generated from diaggrok.registered_codes()
    and committed, so the shipping build needs no Python. This is the check
    that it has not drifted from the decoder in .deps: a parser added to
    diaggrok must move its code out of `unrecognized` and into
    `undecoded_native` in the native census too."""
    path = HERE / "fixtures" / "make_diaggrok_codes.py"
    spec = importlib.util.spec_from_file_location("make_diaggrok_codes", path)
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    committed = (HERE / "diag_inventory_codes.h").read_text()
    assert committed == gen.generate(), (
        "diag_inventory_codes.h has drifted from diaggrok. Regenerate it: "
        "PYTHONPATH=.deps/diaggrok/src python3 fixtures/make_diaggrok_codes.py")
