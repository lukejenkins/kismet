"""Through the real server: a decoder that fell behind is on the source,
warned, and recorded in the kismetdb.

``test_celldiag_bridge_queue.py`` drives the helper over the IPC and proves
the read loop never waits on the decoder. This file proves the drop lands where
an operator looks: the server keeps the queue as
``kismet.datasource.celldiag.bridge_*`` fields, raises the source's warning at
the first drop, and logs the helper's ``DiagBridgeDrop`` row to the kismetdb
``data`` table under the source.

A live source is needed: a replay never drops (its input waits for the queue).
The slow decoder comes in through the decoder interpreter, which is ``python3``
off ``PATH``, as a shim that answers the ``--help`` probe and then
reads stdin at the pace the test sets, whatever bridge it is handed.
The fast-decoder run is the control: the fields are reported (``bridge_reported``
= 1) and read 0, with no warning and no row. Without it, a server that never set
the warning would pass the "no warning" half.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fake_at_modem import FakeAtModem  # noqa: E402
from celldiag_stub_tree import python3_shim  # noqa: E402
from fake_diag_modem import FakeDiagModem  # noqa: E402
from test_celldiag_bridge_queue import IMEI, QUEUE_ENV, STUB  # noqa: E402
from test_celldiag_raw_abort_server import _await  # noqa: E402
from test_celldiag_server_routes import KP_ROOT, SERVER  # noqa: E402
from test_graceful_close_server import LoggingKismetServer  # noqa: E402

F = "kismet.datasource.celldiag."
_DEPS = {
    SERVER: ("datasource_cell_diag.cc", "datasource_cell_diag.h"),
    KP_ROOT / "capture_cell_diag" / "kismet_cap_cell_diag": (
        "capture_cell_diag/capture_cell_diag.c", "capture_cell_diag/diag_bridgeq.c",
        "capture_cell_diag/diag_stats.c", "capture_cell_diag/diag_stats.h"),
}


def _built_or_skip() -> None:
    for binary, deps in _DEPS.items():
        if not binary.is_file():
            pytest.skip(f"no {binary.name} at {binary} -- build it in {KP_ROOT}")
        stale = [d for d in deps if (KP_ROOT / d).stat().st_mtime > binary.stat().st_mtime]
        if stale:
            pytest.skip(f"{binary.name} is older than {', '.join(stale)} -- rebuild it")


def _drive(tmp_path: Path, monkeypatch, *, delay: str, expect_drop: bool):
    shim = python3_shim(tmp_path, f"#!{sys.executable}\n" + STUB)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ.get('PATH', '/usr/bin:/bin')}")
    monkeypatch.setenv("STUB_COUNT", str(tmp_path / "consumed"))
    monkeypatch.setenv("STUB_DELAY", delay)
    monkeypatch.setenv("STUB_CHUNK", "4096")
    monkeypatch.setenv(QUEUE_ENV, str(256 * 1024))

    with FakeAtModem(imei=IMEI) as at, FakeDiagModem(
            filler_hz=400.0, filler_pad=4000, filler_max=2000) as dg:
        srcdef = (f"celldiag-{IMEI}:atport={at.port},diagport={dg.port},nomask=true,"
                  f"defer=false,retry=false,stats_interval=1,clock_anchor_sec=0")
        with LoggingKismetServer(tmp_path, srcdef) as srv:
            _await(lambda: (srv.source() or {}).get(F + "bridge_reported") == 1,
                   30, "the bridge counters on the source", srv)
            if expect_drop:
                _await(lambda: (srv.source() or {}).get(F + "bridge_dropped_chunks", 0) > 0,
                       30, "a drop on the source", srv)
            else:
                _await(lambda: dg.filler_sent >= 2000, 30, "the whole stream", srv)
                _await(lambda: (srv.source() or {}).get(F + "bridge_queued", 1) == 0
                       and (srv.source() or {}).get(F + "bridge_peak", -1) >= 0,
                       15, "an empty queue after the stream", srv)
            src = srv.source()
    db = srv.kismetdb()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT datasource, json FROM data WHERE type = 'DiagBridgeDrop'"
                           ).fetchall()
    finally:
        con.close()
    rows = [(u, json.loads(bytes(j).decode() if isinstance(j, (bytes, memoryview)) else j))
            for u, j in rows]
    return src, rows


def test_a_decoder_that_fell_behind_is_on_the_source_warned_and_in_the_kismetdb(
        tmp_path, monkeypatch):
    _built_or_skip()
    src, rows = _drive(tmp_path, monkeypatch, delay="0.2", expect_drop=True)

    assert src[F + "bridge_reported"] == 1
    assert src[F + "bridge_dropped_chunks"] > 0 and src[F + "bridge_dropped_bytes"] > 0, {
        k: v for k, v in src.items() if "bridge" in k}
    assert src[F + "bridge_peak"] > 0
    warning = src.get("kismet.datasource.warning", "")
    assert "decoder fell behind" in warning, warning

    assert len(rows) == 1, rows
    uuid, row = rows[0]
    assert uuid == src["kismet.datasource.uuid"], (uuid, src["kismet.datasource.uuid"])
    assert row["schema"] == "diag-bridge-drop/1", row
    assert row["queue_bytes"] == 256 * 1024 and row["dropped_chunks"] >= 1, row


def test_a_decoder_that_keeps_up_is_reported_and_not_warned(tmp_path, monkeypatch):
    _built_or_skip()
    src, rows = _drive(tmp_path, monkeypatch, delay="0", expect_drop=False)

    assert src[F + "bridge_reported"] == 1
    assert (src[F + "bridge_dropped_chunks"], src[F + "bridge_dropped_bytes"]) == (0, 0)
    assert "decoder fell behind" not in src.get("kismet.datasource.warning", "")
    assert rows == [], rows
