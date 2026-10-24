"""Drive the celldiag panel's HTTP routes against a REAL Kismet server.

The panel's mask and F3 selectors and its Restart button all sit on
``POST /datasource/by-uuid/:uuid/update_definition`` and the close/open
sequence around it. Tests that read source text cannot show that a running
server accepts that sequence, so this file drives it over HTTP.

``CaptureRun`` is not enough here: it speaks the capture-framework IPC
protocol straight to the helper binary, while these routes live in the server
(``datasourcetracker.cc``) and are only reachable over HTTP. This harness
starts an isolated ``kismet`` instead (private ``--homedir`` / ``--confdir``,
an ephemeral port, logging off) and drives the endpoints as the panel does.

The route refuses a running source, so the only working order is
``close_source -> update_definition -> open_source``. With
``update_definition`` first, the first call returns 500, ``.then()`` does not
chain on a rejection, and the close and open never happen. The panel installs
no ``.fail()`` handler, so that failure would be silent.

Only ``f3_preset`` can be checked end to end offline. A ``replay=``
source hard-codes ``mask_preset`` to the literal ``"replay"``
(``capture_cell_diag.c``), so the mask selector's round-trip still needs a
modem. ``f3_preset`` is resolved *before* the live/replay split and reports
honestly — and it exercises the identical route, so the shared mechanism is
covered even though one of its two labels is not.
"""

from __future__ import annotations

import base64
import json
import re
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decode_oracle  # noqa: E402
from test_celldiag_replay_ab import _helper_relays_f3  # noqa: E402


def _reported_f3(preset: str) -> str:
    """What a source armed with ``f3=<preset>`` reports as ``f3_preset``.

    A preset the modem arms while the bridge has no ``--f3`` reads
    ``<preset>+norelay``, which is the accurate readout rather than a failure. The
    expected value is derived from the bridge the server runs (the fixture's
    ``helper=`` is the same discovered oracle), the way the replay A/B derives it,
    so the round trip below is checked on both branches instead of assuming the
    relay. ``off`` arms nothing and is never decorated.
    """
    if preset == "off" or _helper_relays_f3():
        return preset
    return f"{preset}+norelay"

KP_ROOT = Path(__file__).resolve().parents[2]
SERVER = KP_ROOT / "kismet"
CONF = KP_ROOT / "conf"

#: 15-digit synthetic IMEI. `probe_callback` requires `celldiag-<15 digits>`;
#: a bare `celldiag:` interface is silently declined and the source never
#: registers at all, with no message.
IMEI = "000000000000000"

#: Sources whose staleness makes a run meaningless: these routes and the
#: sequence they enforce are compiled INTO the server binary.
_SERVER_DEPS = ("datasourcetracker.cc", "kis_datasource.cc", "kis_datasource.h")

_CAPTURES_ENV = "CELL_CAPTURES"

#: How many times to concatenate the replay fixture. See `_replay_or_skip`.
REPLAY_REPEATS = 6


def _server_or_skip() -> Path:
    """The built server, and it must be NEWER than the routes under test.

    A stale ``kismet`` would drive the old route, report green, and certify a
    sequence nobody compiled. Skips (naming ``make kismet``) rather than
    failing, because an unbuilt server is a host property: the build takes
    about ten minutes and not every checkout wants to pay it.
    """
    if not SERVER.is_file():
        pytest.skip(f"no server binary at {SERVER} -- run `make kismet` in {KP_ROOT}")
    mtime = SERVER.stat().st_mtime
    stale = [d for d in _SERVER_DEPS
             if (KP_ROOT / d).is_file() and (KP_ROOT / d).stat().st_mtime > mtime]
    if stale:
        pytest.skip(f"{SERVER.name} is older than {', '.join(stale)} -- "
                    f"run `make kismet` in {KP_ROOT}")
    return SERVER


def _replay_or_skip(tmp_path: Path) -> Path:
    """A raw-HDLC capture to feed ``replay=``.

    Same discovery contract as ``test_celldiag_replay_ab``: no default path,
    because captures are large binaries kept outside any source tree and
    guessing one yields a skip whose stated cause is wrong.
    """
    env = os.environ.get("CELLDIAG_REPLAY_FIXTURE")
    if env:
        src = Path(env)
        if not src.exists():
            pytest.skip(f"CELLDIAG_REPLAY_FIXTURE={env!r} not found")
    else:
        root = os.environ.get(_CAPTURES_ENV)
        if not root:
            pytest.skip(f"no capture directory: set {_CAPTURES_ENV}=<dir> "
                        "(replay fixtures live outside the source tree)")
        cand = [Path(root) / n for n in ("celldiag_mixed_lte_nr.hdlc",
                                         "celldiag_mixed_lte_nr.hdlc.zst")]
        src = next((c for c in cand if c.exists()), None)
        if src is None:
            pytest.skip(f"no replay fixture (${_CAPTURES_ENV}/"
                        "celldiag_mixed_lte_nr.hdlc[.zst])")

    dst = tmp_path / "replay.hdlc"
    if src.suffix == ".zst":
        try:
            import zstandard as zstd
        except ImportError:  # pragma: no cover
            pytest.skip("zstandard needed to decompress the fixture")
        with src.open("rb") as fp, dst.open("wb") as out:
            zstd.ZstdDecompressor().copy_stream(fp, out)
    else:
        shutil.copyfile(src, dst)

    # Concatenated to widen the window the source is up for. `replay=` has
    # no pacing and no loop: the capture thread reads the file as fast as it
    # can, and a 20 MB fixture drains in about four seconds, after which the
    # source spins down. Every assertion here needs a running source.
    # Concatenation is sound because the stream is a sequence of
    # self-delimiting HDLC frames, so the deframer sees a longer capture, not
    # a malformed one. It buys time, not coverage.
    with dst.open("ab") as out:
        chunk = dst.read_bytes()
        for _ in range(REPLAY_REPEATS - 1):
            out.write(chunk)
    return dst


#: capture dir -> the parity role whose DISCOVERED binary the server must launch.
_HELPER_ROLES = {"capture_cell_at": "cellat", "capture_cell_diag": "celldiag"}


def _helper_dir(capture_dir: str) -> Path:
    """Where the server should look for ``capture_dir``'s helper binary.

    The directory of the binary ``celldiag_parity.discover`` found (the same
    binary every staleness guard here inspects), not a fixed
    ``KP_ROOT/<capture_dir>``. On a configured tree the two agree. On a tree
    built only through ``standalone.mk baseline`` (e.g. macOS) the binary is in
    ``build-baseline/``, and the fixed path would make every real-server test
    fail "Unable to find driver". Roles without a parity entry keep the fixed
    path.
    """
    role = _HELPER_ROLES.get(capture_dir)
    if role is not None:
        import celldiag_parity
        found = celldiag_parity.discover(role)
        if found.ok:
            return Path(found.path).parent
    return KP_ROOT / capture_dir


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class KismetServer:
    """An isolated Kismet, with an HTTP client that speaks the panel's dialect.

    Isolated in three ways: a private ``--homedir`` (so the generated admin
    credentials and server UUID never touch the operator's ``~/.kismet``), a
    copied ``--confdir`` (so the port and logging overrides do not edit the
    repo's ``kismet_site.conf``), and an ephemeral port (so a developer's own
    running Kismet is neither disturbed nor accidentally driven by this test).
    """

    USER, PASSWD = "celldiagtest", "celldiagtest"

    def __init__(self, tmp_path: Path, srcdef: str, silent: bool = True) -> None:
        #: `--silent` suppresses every message after startup, including
        #: `Found type`, `Data source … launched successfully`, `Unable to find
        #: driver`, `Data source … failed to launch` and every `celldiag stats:`
        #: line. No test that runs with `silent=True` may assert on log
        #: content: an assertion that a message is absent passes whether or
        #: not it was emitted.
        #:
        #: Every test in THIS file asserts on HTTP/reported fields, so the
        #: default is safe and kept. Pass `silent=False` when the log IS the
        #: measurement (see `test_celldiag_live_native_ab.py`).
        self.silent = silent
        self.port = _free_port()
        self.home = tmp_path / "home"
        (self.home / ".kismet").mkdir(parents=True)
        # Pre-seeding the credentials skips Kismet's first-run setup prompt,
        # which otherwise leaves every LOGON_ROLE route unreachable.
        (self.home / ".kismet" / "kismet_httpd.conf").write_text(
            f"httpd_username={self.USER}\nhttpd_password={self.PASSWD}\n")
        self.conf = tmp_path / "conf"
        shutil.copytree(CONF, self.conf)
        # The site conf names its helpers by absolute path to one checkout
        # (`helper_binary_path=<that checkout>/capture_cell_at`).
        # helper_binary_path is a search list and the first hit wins, so from
        # any other tree (a worktree, a scratch copy) the server would launch
        # that checkout's helpers while every staleness guard here inspects
        # this tree's binaries. Point each capture dir at this tree's.
        site = self.conf / "kismet_site.conf"
        text = re.sub(
            r"(?m)^helper_binary_path=\S*/(capture_[A-Za-z0-9_]+)\s*$",
            lambda m: f"helper_binary_path={_helper_dir(m.group(1))}",
            site.read_text())
        # Drop the site conf's own bind line (``0.0.0.0``: the operator's
        # LAN-reachable server). The first value of a key wins, so with it in
        # place the loopback line appended below is dead text and the harness
        # server listens on every interface with this class's fixed
        # USER/PASSWD.
        site.write_text(re.sub(r"(?m)^[ \t]*httpd_bind_address[ \t]*=.*\n?", "", text))
        with (self.conf / "kismet_site.conf").open("a") as fp:
            # `remote_capture_enabled=false` is required. The repo's site
            # conf listens on the fixed port 3501, and a server that cannot
            # bind it dies with "KISMET HAS ENCOUNTERED A FATAL ERROR", so
            # back-to-back tests would race each other's lingering socket and
            # a developer's own running Kismet would fail the suite.
            fp.write(f"\nhttpd_port={self.port}\nhttpd_bind_address=127.0.0.1\n"
                     "enable_logging=false\n"
                     "remote_capture_enabled=false\n")
        self.srcdef = srcdef
        self.logpath = tmp_path / "kismet.log"
        self._log = self.logpath.open("wb")
        self.proc: subprocess.Popen | None = None

    # ── lifecycle ────────────────────────────────────────────────────────
    def __enter__(self) -> "KismetServer":
        argv = [str(SERVER), "--no-ncurses", "--no-line-wrap",
                "--homedir", str(self.home), "--confdir", str(self.conf),
                "--datadir", str(KP_ROOT)]
        # srcdef=None: a server started with no sources, where everything is
        # enabled later from the Data Sources panel.
        if self.srcdef:
            # A list starts several sources (one modem's cellat + celldiag).
            for d in ([self.srcdef] if isinstance(self.srcdef, str) else self.srcdef):
                argv += ["-c", d]
        if self.silent:
            argv.insert(2, "--silent")
        # KISMET_CONF, not just --confdir. --confdir sets only the directory
        # the kismet_*.conf overrides are read from; the base kismet.conf path
        # is `$KISMET_CONF/kismet.conf`, falling back to the compiled-in
        # SYSCONF_LOC. Without it, a build-only host exits at once ("Error
        # reading config file '/usr/local/etc/kismet.conf'"), and a host with
        # an installed Kismet silently reads the installed base config. The
        # private copy keeps the base config as isolated as the overrides.
        self.proc = subprocess.Popen(
            argv,
            stdout=self._log, stderr=subprocess.STDOUT, cwd=str(KP_ROOT),
            env={**os.environ, "HOME": str(self.home),
                 "KISMET_CONF": str(self.conf)})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(f"kismet exited early:\n{self.tail()}")
            try:
                self.get("system/status.json")
                return self
            except Exception:
                time.sleep(0.5)
        raise AssertionError(f"kismet never served HTTP:\n{self.tail()}")

    def __exit__(self, *exc) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(30)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self.proc.kill()
        self._log.close()

    def tail(self, n: int = 3000) -> str:
        return self.logpath.read_text(errors="replace")[-n:]

    # ── HTTP ─────────────────────────────────────────────────────────────
    def _request(self, path: str, data: bytes | None = None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/{path}", data=data)
        token = base64.b64encode(
            f"{self.USER}:{self.PASSWD}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        return req

    def get(self, path: str):
        with urllib.request.urlopen(self._request(path), timeout=30) as r:
            return json.loads(r.read().decode())

    def post(self, path: str, payload: dict):
        """POST the way the panel does -- a urlencoded ``json=`` field."""
        body = ("json=" + urllib.parse.quote(json.dumps(payload))).encode()
        with urllib.request.urlopen(self._request(path, body), timeout=30) as r:
            return r.status, json.loads(r.read().decode())

    # ── datasource helpers ───────────────────────────────────────────────
    def source(self) -> dict | None:
        rows = self.get("datasource/all_sources.json")
        return rows[0] if rows else None

    def await_running(self, want: bool = True, timeout: float = 90) -> dict:
        deadline = time.monotonic() + timeout
        row = None
        while time.monotonic() < deadline:
            row = self.source()
            if row is not None and bool(row["kismet.datasource.running"]) is want:
                return row
            time.sleep(0.5)
        raise AssertionError(
            f"source never reached running={want}; last row="
            f"{json.dumps(row, indent=1) if row else None}\n{self.tail()}")

    def ensure_running(self) -> dict:
        """Re-open the source if the replay has drained.

        A `replay=` source is finite. There is no loop and no pacing, so the
        capture thread reaches EOF and the source spins down, which is normal
        and not an error. With `retry=false` (see the source definition)
        nothing re-opens it behind the test's back, which is the property every
        attribution here depends on; the cost is that the test must open it
        itself when a previous step ran long.
        """
        row = self.source()
        assert row is not None, f"no datasource registered\n{self.tail()}"
        if row["kismet.datasource.running"]:
            return row
        self.get(f"datasource/by-uuid/{row['kismet.datasource.uuid']}/"
                 "open_source.cmd")
        return self.await_running(True)

    def await_field(self, name: str, want, timeout: float = 60) -> None:
        """Wait for a celldiag stats field to REPORT ``want``.

        The outcome of every one of these controls is a field the capture
        binary re-reports on its own stats interval -- never the HTTP status,
        which a refused celldiag setting returns as success anyway.
        """
        key = f"kismet.datasource.celldiag.{name}"
        deadline = time.monotonic() + timeout
        got = None
        while time.monotonic() < deadline:
            row = self.source()
            got = (row or {}).get(key)
            if got == want:
                return
            time.sleep(0.5)
        raise AssertionError(
            f"{name} never reported {want!r} (last {got!r})\n{self.tail()}")


@pytest.fixture()
def server(tmp_path: Path):
    _server_or_skip()
    oracle = decode_oracle.discover()
    if not oracle.ok:
        pytest.skip(oracle.skip_reason())
    replay = _replay_or_skip(tmp_path)
    # `retry=false` is load-bearing. A replay source is finite, so under
    # Kismet's default retry it flaps on roughly a ten-second period, with a
    # fresh helper pid each cycle. Every assertion about
    # "close then open brought the source back" or "the override was armed by
    # MY open" is then unattributable: the retry would have produced the same
    # observation with the test doing nothing. Turning retry off makes this
    # harness the only thing that opens the source.
    srcdef = (f"celldiag-{IMEI}:replay={replay},f3=off,name=routetest,"
              f"retry=false")
    with KismetServer(tmp_path, srcdef) as srv:
        srv.await_running(True)
        yield srv


def _base(row: dict) -> str:
    return f"datasource/by-uuid/{row['kismet.datasource.uuid']}/"


# ─────────────────────────────────────────────────────────────────────────
def test_the_harness_turns_logging_off_with_a_key_kismet_reads():
    """Every conf key this harness writes must be one the server reads.

    Kismet reads only ``enable_logging`` (default true), not
    ``logging_enabled``. A misspelled key is accepted without a word, logging
    stays on, and every run leaves a ``.kismet`` log in the tree root. Source
    text only, so it runs on a host with no server build.
    """
    block = Path(__file__).read_text().split("fp.write(", 1)[1].split(")", 1)[0]
    written = set(re.findall(r'(?:\\n|")([a-z_]+)=', block))
    assert "httpd_port" in written, written   # the parse itself found the block
    assert "enable_logging" in written, written
    assert "logging_enabled" not in written, written
    logtracker = (KP_ROOT / "logtracker.cc").read_text()
    assert 'fetch_opt_bool("enable_logging"' in logtracker


def _first_value(conf_text: str, key: str) -> "str | None":
    """Kismet's rule for a key set twice in one file: the FIRST value wins
    (``config_file::fetch_opt`` returns ``second[0]``, configfile.cc)."""
    for line in conf_text.splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip().lower() == key:
            return v.strip()
    return None


def test_the_harness_conf_binds_the_server_to_loopback(tmp_path):
    """The harness's REST server must listen on 127.0.0.1 only.

    The repo's ``kismet_site.conf`` sets ``httpd_bind_address=0.0.0.0``, and
    the harness appends ``httpd_bind_address=127.0.0.1`` to the same file.
    The first value of a key wins, so unless the original line is removed the
    append is dead text and the server listens on every interface with this
    file's fixed USER/PASSWD. Conf only, so it runs with no server build.
    """
    srv = KismetServer(tmp_path, None)
    srv._log.close()
    site = (srv.conf / "kismet_site.conf").read_text()
    assert _first_value(site, "httpd_port") == str(srv.port), "the parse found the block"
    assert _first_value(site, "httpd_bind_address") == "127.0.0.1", \
        [ln for ln in site.splitlines() if "httpd_bind_address" in ln]


def test_the_harness_server_listens_on_loopback_only(tmp_path):
    """Loopback-only binding, read off the running server's bound address.

    ``kis_net_beast_httpd.cc`` logs ``HTTP server listening on <addr>:<port>``
    from the bound endpoint, so this is the server's own report, not the
    conf's claim. ``silent=False``: the log IS the measurement here.
    """
    _server_or_skip()
    with KismetServer(tmp_path, None, silent=False) as srv:
        port = srv.port
    log = srv.logpath.read_text(errors="replace")
    m = re.search(r"HTTP server listening on (\S+):(\d+)", log)
    assert m, f"no listen line in the server log:\n{log[-1500:]}"
    assert (m.group(1), int(m.group(2))) == ("127.0.0.1", port), m.group(0)


def test_update_definition_refuses_a_running_source(server):
    """``update_definition`` on a running source returns 500.

    This is why the panel must close the source first. Asserted here rather
    than only in the C++ so the refusal is pinned as observed behaviour of a
    running server, which source-reading guards cannot establish.
    """
    row = server.ensure_running()
    with pytest.raises(urllib.error.HTTPError) as exc:
        server.post(_base(row) + "update_definition.cmd",
                    {"options": {"f3": "high"}})
    assert exc.value.code == 500
    assert "source is running" in exc.value.read().decode()


def test_close_update_open_actually_changes_the_armed_preset(server):
    """The full round trip, end to end, on a live server.

    ``f3=off`` -> close -> ``update_definition`` -> open -> the capture binary
    reports ``f3_preset='high'`` (``'high+norelay'`` when the bridge cannot relay
    F3). The assertion is on the reported preset, not
    on the 200: a refused celldiag setting comes back with success=1,
    so the status code cannot distinguish applied from declined.
    """
    row = server.ensure_running()
    base = _base(row)
    server.await_field("f3_preset", "off")

    server.get(base + "close_source.cmd")
    server.await_running(False)
    status, _ = server.post(base + "update_definition.cmd",
                            {"options": {"f3": "high"}})
    assert status == 200
    server.get(base + "open_source.cmd")
    server.await_running(True)
    server.await_field("f3_preset", _reported_f3("high"))


def test_a_rejected_update_leaves_the_source_down_until_it_is_reopened(server):
    """Why the panel's reopen is ``.always()`` and not ``.then()``.

    Closing first moves the risk: the source is down when the call that can
    fail is made. A rejected ``update_definition`` returns 500 with the source
    closed, and only an unconditional reopen brings it back. A reopen chained
    on success would leave the operator's capture down and, since the control
    reads nothing from the transport, silent.

    It also pins the sticky semantics under failure: the ``high`` applied
    before the rejected call survives the recovery reopen, so the failed
    second Apply does not quietly revert the successful first one.
    """
    row = server.ensure_running()
    base = _base(row)

    server.get(base + "close_source.cmd")
    server.await_running(False)
    server.post(base + "update_definition.cmd", {"options": {"f3": "high"}})
    server.get(base + "open_source.cmd")
    server.await_field("f3_preset", _reported_f3("high"))

    server.ensure_running()
    server.get(base + "close_source.cmd")
    server.await_running(False)
    with pytest.raises(urllib.error.HTTPError) as exc:
        server.post(base + "update_definition.cmd", {"options": {}})
    assert exc.value.code == 500
    assert not server.source()["kismet.datasource.running"], (
        "the source should still be closed -- this is the state the panel "
        "must recover from unconditionally")

    server.get(base + "open_source.cmd")
    server.await_running(True)
    server.await_field("f3_preset", _reported_f3("high"))


def test_restart_is_close_then_open_and_the_source_comes_back(server):
    """The panel's Restart button: the same sequence minus the update.

    The control's value is the ordering (the open must not start until the
    close has returned), so the check is that the pair, run in that order,
    leaves a source that is running again.

    The assertion is a changed ``ipc_pid``: a real Kismet server reaping and
    respawning the process. A "running again" check alone would not show
    that anything was reaped.

    This is only attributable because the source runs with ``retry=false``.
    With retry on, the source flaps on a ~10 s period with a fresh pid every
    cycle, so a pid check would pass with the test doing nothing at all.

    Not asserted on ``helper_alive``: that field reports whether the decode
    helper is up right now, and a drained replay legitimately has no helper,
    so on a replay source it is not a restart-health signal. On a live modem
    it is.
    """
    row = server.ensure_running()
    base = _base(row)
    before = row["kismet.datasource.ipc_pid"]

    server.get(base + "close_source.cmd")
    server.await_running(False)
    server.get(base + "open_source.cmd")
    row = server.await_running(True)

    assert row["kismet.datasource.ipc_pid"] != before, (
        f"the capture process was not respawned (pid still {before}); the "
        f"source reports running, which is the server's own flag, not "
        f"evidence that anything came back")


def test_a_replay_source_reports_mask_preset_as_replay_not_a_preset(server):
    """The limit of this harness, asserted rather than left as prose.

    ``capture_cell_diag.c`` sets ``mask_preset`` to the literal ``"replay"``
    on a replay source: there is no modem and no LOG_CONFIG handshake, so
    there is no armed mask to report. The mask selector therefore cannot be
    round-tripped offline the way the F3 selector can; it needs a modem.

    Pinned so that if a future change makes a replay source report a real mask
    preset, this test fails and says the offline gap just closed.

    Waited for rather than read once: every celldiag stats field is empty
    until the binary's first stats interval, so a bare read here fails with
    ``'' != 'replay'`` and looks like the claim is wrong.
    """
    server.ensure_running()
    server.await_field("mask_preset", "replay")
