# SPDX-License-Identifier: Apache-2.0
"""Discover, choose, Enable -- and see why when it fails.

The journey: Kismet is started without any cell modem options; the user opens
the Data Sources UI, sees the cell modems and capture types on offer, picks
one and hits Enable. What this pins:

* each row names the modem and capture type, not just
  ``Available Interface: cellat-351234567890123 (cellat)``;
* Enable carries options, so a profile, log or stream can be chosen at
  bring-up, not only a bare ``<iface>:type=<driver>``;
* a modem whose AT port a running source holds is still offered by its
  sibling driver, so enabling "AT commands" does not make "DIAG" disappear;
* a failed Enable answers with the reason, not a 500 with an empty body, and
  a celldiag bring-up failure shows the helper's reason ("Pass diagport=...
  explicitly"), not ``IPC connection closed``.

Three layers, each where it can be measured:

* **structural** -- the Enable form's profile list against the binary's table,
  the shared re-open helper's call order;
* **server** -- the reason a failed open now carries, and the celldiag
  bring-up/switch fields;
* **browser** -- what an operator SEES and clicks.

Browser tests skip without Playwright or a Chromium (see the sibling
``test_cell_web_ui_browser``); the replay ones without a decode helper and
``CELL_CAPTURES``.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from fake_at_modem import QENG_SERVING, FakeAtModem
from test_cellat_server_fields import F, _await, _fresh_server_or_skip
from test_celldiag_server_routes import KismetServer

TREE = Path(__file__).resolve().parents[2]
PANEL_JS = TREE / "http_data" / "js" / "kismet.ui.datasources.js"
OPTIONS_C = TREE / "capture_cell_at" / "cellat_options.c"

IMEI = "351234567890123"
CGMM = {"AT+CGMM": "RM520N-GL"}
D = "kismet.datasource.celldiag."


# ── structural ──────────────────────────────────────────────────────────────
def test_the_enable_profiles_mirror_the_binary():
    """Two lists that must agree: the form's, and STRATEGIES[]."""
    c = OPTIONS_C.read_text()
    table = c.split("STRATEGIES[] = {", 1)[1].split("};", 1)[0]
    binary = re.findall(r'\{\s*"([a-z_]+)",\s*"([^"]+)"', table)
    js = PANEL_JS.read_text()
    block = js.split("export const CELLAT_ENABLE_PROFILES = [", 1)[1].split("];", 1)[0]
    form = re.findall(r"\['([a-z_]+)', '([^']+)'\]", block)
    assert binary, "STRATEGIES[] not found -- the parse moved"
    assert form == binary, f"form offers {form}, the binary accepts {binary}"


def test_the_reopen_helper_closes_first_and_always_reopens():
    """The sequence every restart-to-apply control inherits: close first, and
    reopen even when the update fails."""
    js = PANEL_JS.read_text()
    body = js.split("const cell_reopen_with_options = ", 1)[1].split("\n}\n", 1)[0]
    assert body.index("close_source.cmd") < body.index("update_definition.cmd"), body
    assert re.search(r"\.always\(\(\) => \$\.get\(base \+ 'open_source\.cmd'\)\)", body), body
    for ctl in ("celldiag_rawpackets_control", "celldiag_qsh_control",
                "celldiag_anchor_control"):
        ctl_body = js.split(f"const {ctl} = ", 1)[1].split("\n}\n", 1)[0]
        assert "cell_reopen_with_options(" in ctl_body, ctl
        assert "update_definition.cmd" not in ctl_body, (
            f"{ctl} posts its own update; use the shared helper")


# ── server ──────────────────────────────────────────────────────────────────
def _post_raw(ks: KismetServer, path: str, payload: dict) -> "tuple[int, str]":
    body = ("json=" + urllib.parse.quote(json.dumps(payload))).encode()
    try:
        with urllib.request.urlopen(ks._request(path, body), timeout=60) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_a_failed_enable_says_why(tmp_path, monkeypatch):
    """A refused ``add_source`` answers 500 WITH the reason, not an empty map."""
    _fresh_server_or_skip()
    with FakeAtModem(imei=IMEI, extra=CGMM) as modem:
        monkeypatch.setenv("CELLAT_SCAN_PORTS", modem.port)
        with KismetServer(tmp_path, None) as ks:
            code, body = _post_raw(ks, "datasource/add_source.cmd",
                                   {"definition": f"cellat-{IMEI}:type=cellat,"
                                                  "strategy=bogus,retry=false"})
    assert code == 500, (code, body)
    assert "Unknown strategy 'bogus'" in body, body
    assert f"cellat-{IMEI}" in body, body


def test_a_failed_celldiag_bringup_is_the_sources_error(tmp_path, monkeypatch):
    """The deferred bring-up's reason, as fields AND as the error.

    An AT-only fake has no DIAG port, so the bring-up fails at port detection
    -- after the open was already answered, which is the case that would
    otherwise read "IPC connection closed"."""
    _fresh_server_or_skip()
    with FakeAtModem(imei=IMEI, extra=CGMM) as modem:
        monkeypatch.setenv("CELLDIAG_SCAN_PORTS", modem.port)
        monkeypatch.setenv("CELLAT_SCAN_PORTS", modem.port)
        with KismetServer(tmp_path, f"celldiag-{IMEI}:retry=false") as ks:
            row = _await(ks, lambda r: r.get(D + "bringup_error")
                         and not r["kismet.datasource.running"], timeout=40)
    why = row[D + "bringup_error"]
    assert "Could not locate DIAG interface" in why, why
    assert row["kismet.datasource.error_reason"] == "bring-up failed: " + why, (
        row["kismet.datasource.error_reason"])
    assert row[D + "bringup_phases"].startswith("at_scan="), row[D + "bringup_phases"]
    assert row[D + "mask_preset"] == "failed", row[D + "mask_preset"]
    assert row[D + "helper_alive"] == 0


def _replay_srcdef(tmp_path: Path, extra: str = "") -> str:
    import decode_oracle
    from test_celldiag_server_routes import _replay_or_skip
    oracle = decode_oracle.discover()
    if not oracle.ok:
        pytest.skip(oracle.skip_reason())
    replay = _replay_or_skip(tmp_path)
    return (f"celldiag-000000000000000:replay={replay},f3=off,name=switchtest,"
            f"retry=false{extra}")


def test_the_celldiag_switches_read_back_and_rawpackets_toggles(tmp_path):
    """On a replay source: the celldiag switches are REPORTED, and the
    re-open the panel's button performs changes the reported value."""
    _fresh_server_or_skip()
    with KismetServer(tmp_path, _replay_srcdef(tmp_path)) as ks:
        row = _await(ks, lambda r: r.get(D + "switches_reported") == 1
                     and r.get(D + "rawpackets_slices", 0) > 0, timeout=60)
        assert row[D + "rawpackets_on"] == 1
        assert row[D + "bringup_phases"] == "", "a replay runs no modem bring-up"
        base = f"datasource/by-uuid/{row['kismet.datasource.uuid']}/"
        ks.get(base + "close_source.cmd")
        _await(ks, lambda r: not r["kismet.datasource.running"])
        status, _ = ks.post(base + "update_definition.cmd",
                            {"options": {"rawpackets": "off"}})
        assert status == 200
        ks.get(base + "open_source.cmd")
        row = _await(ks, lambda r: r.get(D + "rawpackets_on") == 0, timeout=60)
        assert "rawpackets=off" in row["kismet.datasource.definition"]


# ── browser ─────────────────────────────────────────────────────────────────
def _browser():
    pytest.importorskip("playwright.sync_api")
    from test_cell_web_ui_browser import _data_sources
    return _data_sources


def _await_text(loc, needle: str, timeout: float = 30.0) -> str:
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = loc.inner_text() if loc.count() else ""
        if needle in text:
            return text
        time.sleep(0.5)
    raise AssertionError(f"never showed {needle!r}; last: {text!r}")


def test_the_list_names_each_modem_and_capture_and_enables_with_options(tmp_path, monkeypatch):
    """Readable rows, then Enable with a profile and an AT log."""
    data_sources = _browser()
    atlog = tmp_path / "enabled-with.jsonl"
    with FakeAtModem(imei=IMEI, extra={**QENG_SERVING, **CGMM}) as modem:
        monkeypatch.setenv("CELLAT_SCAN_PORTS", modem.port)
        monkeypatch.setenv("CELLDIAG_SCAN_PORTS", modem.port)
        with data_sources(tmp_path, None) as (ks, page):
            titles = page.locator(".k-ds-cell-title")
            at = page.locator(".interface", has=page.locator(
                ".k-ds-cell-title", has_text="Quectel RM520N-GL — AT commands"))
            diag = page.locator(".interface", has=page.locator(
                ".k-ds-cell-title", has_text="Quectel RM520N-GL — DIAG"))
            at.first.wait_for(timeout=30000)
            diag.first.wait_for(timeout=30000)
            sub = at.first.locator(".k-ds-modem").inner_text()
            assert f"IMEI {IMEI}" in sub and "available" in sub, sub
            assert "RM520NGLAAR03A03M4G" in sub, sub
            # One modem's rows sort together, AT first.
            order = [x for x in titles.all_inner_texts() if "RM520N-GL" in x]
            assert order[:2] == ["Quectel RM520N-GL — AT commands",
                                 "Quectel RM520N-GL — DIAG"], order

            at.first.locator("h3").click()
            at.first.locator("select.cell-enable-profile").select_option("walking")
            at.first.locator("input.cell-enable-atlog").fill(str(atlog))
            time.sleep(3.5)   # a list refresh: the choice must survive it
            assert at.first.locator("select.cell-enable-profile").input_value() == "walking"
            at.first.locator("button.cell-enable-button").click()

            row = _await(ks, lambda r: r.get(F + "strategy") == "walking"
                         and r.get(F + "atlog_active"), timeout=40)
            definition = row["kismet.datasource.definition"]
            assert "strategy=walking" in definition, definition
            assert f"atlog={atlog}" in definition, definition
            # The running source's header names the modem and the capture.
            hdr = page.locator(".source .k-ds-modem").first
            _await_text(hdr, "AT commands")


def test_a_sibling_the_list_cannot_see_is_offered_with_the_reason(tmp_path, monkeypatch):
    """A sibling hidden by a held AT port, and its reason, in the browser.

    The DIAG lister is pointed at nothing, as it effectively is when the AT
    port it identifies modems by is held by the running cellat source. The row
    must still be offered, SAY why it is not in the list, and -- enabled -- the
    celldiag source must show the bring-up's reason, not "IPC connection
    closed"."""
    data_sources = _browser()
    with FakeAtModem(imei=IMEI, extra={**QENG_SERVING, **CGMM}) as modem:
        monkeypatch.setenv("CELLDIAG_SCAN_PORTS", str(tmp_path / "no-such-tty"))
        monkeypatch.setenv("CELLAT_SCAN_PORTS", modem.port)
        with data_sources(tmp_path, f"cellat-{IMEI}:atport={modem.port},"
                                    "debug=false") as (ks, page):
            _await(ks, lambda r: r.get(F + "obs_total", 0) > 0)
            diag = page.locator(".interface", has=page.locator(
                ".k-ds-cell-title", has_text="— DIAG"))
            diag.first.wait_for(timeout=30000)
            assert "not in the list right now" in diag.first.locator(".k-ds-modem").inner_text()
            diag.first.locator("h3").click()
            reason = _await_text(diag.first.locator("tr#cell_unlisted"),
                                 "holds the AT port")
            assert f"cellat-{IMEI}" in reason, reason

            diag.first.locator("button.cell-enable-button").click()
            src = page.locator(".k-ds-source", has_text=f"celldiag-{IMEI}").first
            src.wait_for(timeout=30000)
            src.click()
            bring = _await_text(page.locator("tr#celldiag_bringup"), "failed after",
                                timeout=40)
            # This scan sees no tty at all, so the bring-up fails finding the
            # modem: the helper's own words, not the transport's.
            assert "not found on any serial port" in bring, bring
            helper = page.locator("tr#celldiag_helper").inner_text()
            assert "DEAD" not in helper and "bring-up failed" in helper, helper
            uuid = next(s["kismet.datasource.uuid"]
                        for s in ks.get("datasource/all_sources.json")
                        if s["kismet.datasource.name"] == f"celldiag-{IMEI}")
            # Read after SEVERAL refreshes, not on first appearance: if the
            # header icon span and this row share `#error`, the icon update
            # blanks the row from the second refresh on, and a check in the
            # first second would not see it.
            time.sleep(3.5)
            err = _await_text(page.locator(f"div.source[id='{uuid}'] tr#error"),
                              "bring-up failed:")
            assert "IPC connection closed" not in err, err


def test_the_rawpackets_switch_is_flipped_from_the_browser(tmp_path):
    """The rawpackets switch and the celldiag replay controls, in the browser."""
    data_sources = _browser()
    with data_sources(tmp_path, _replay_srcdef(tmp_path)) as (ks, page):
        _await(ks, lambda r: r.get(D + "rawpackets_slices", 0) > 0, timeout=60)
        src = page.locator(".k-ds-source", has_text="switchtest").first
        src.wait_for(timeout=30000)
        src.click()
        text = _await_text(page.locator("tr#celldiag_rawpackets"), "slices into the kismetdb")
        # The COUNT, not only the words: a row stuck at "0 slices" beside a
        # field that says thousands read as healthy (a mutation that showed 0 survived).
        m = re.search(r"on \u2014 (\d+) slices", text)
        assert m and int(m.group(1)) > 0, text
        # QSH and clock anchors exist only on a live modem.
        assert page.locator("tr#celldiag_qsh").count() == 0
        assert page.locator("tr#celldiag_anchor").count() == 0
        page.locator("tr#celldiag_rawpackets button", has_text="Turn off").click()
        # Not gated on `running`: a replay drains in seconds and stops; the
        # field is the new session's own report either way.
        _await(ks, lambda r: r.get(D + "rawpackets_on") == 0, timeout=60)
        _await_text(page.locator("tr#celldiag_rawpackets"), "off — the raw DIAG stream")
