"""The bridge's live GSMTAP sink (``--gsmtap HOST:PORT``, the C source's
``gsmtap=`` option).

While a celldiag source runs, every GSMTAP-expressible record the bridge decodes
(LTE RRC ``0xB0C0`` -- SIB1..SIBn, paging, RRC signalling -- and LTE NAS
``0xB0Ex``) is sent as one UDP datagram to HOST:PORT, so a Wireshark listening
on that port dissects the SIBs with ``lte-rrc`` live, next to Kismet.

The bytes are pinned, not re-derived: the expected frame below is the SAME
expectation ``diagspec/wirein_selftest_0xb0c0.cpp`` pins the native C++ framer
to. So the bridge's live sink and the native leg are held to one reference,
and a drift in either fails a test rather than a Wireshark session.

The payloads are SYNTHETIC (see that selftest), not lifted off a modem: this
tree ships no measured radio data.
"""
from __future__ import annotations

import importlib.util
import io
import json
import socket
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent


def _load_bridge():
    path = HERE / "kismet_diag_decode.py"
    spec = importlib.util.spec_from_file_location("kismet_diag_decode", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kdd = _load_bridge()

_IMEI = "0"
_B0C0 = 0xB0C0

#: Synthetic v20 BCCH-DL-SCH record and the GSMTAP frame diaggrok (and the
#: native diag_0xb0c0_to_gsmtap) produce for it -- wirein_selftest_0xb0c0.cpp.
_FRAMED_PAYLOAD = bytes.fromhex("140f3000d9000208f93f0100021000000002000102")
_FRAMED_EXPECT = bytes.fromhex("02040d000000000000000000050000000102")
#: Parses, but resolves to a channel with no GSMTAP LTE-RRC sub-type.
_DECLINED_PAYLOAD = bytes.fromhex("140f300064007f080320010005000000000000")


@pytest.fixture
def receiver():
    """A real loopback UDP socket on an ephemeral port: what Wireshark is."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(2.0)
    yield s
    s.close()


def _drain(sock):
    got = []
    sock.settimeout(0.2)
    try:
        while True:
            got.append(sock.recv(65535))
    except socket.timeout:
        pass
    return got


def _sink_to(receiver):
    host, port = receiver.getsockname()
    return kdd.GsmtapLive(host, port)


def test_a_sib_record_reaches_the_port_byte_identical_to_the_pinned_frame(receiver):
    sink = _sink_to(receiver)
    out = io.StringIO()
    kdd._emit_records([(_B0C0, 0, _FRAMED_PAYLOAD)], _IMEI, out, {},
                      clock=lambda: 1.0, gsmtap=sink)
    assert receiver.recv(65535) == _FRAMED_EXPECT
    assert sink.sent == 1 and sink.declined == 0 and sink.errors == 0


def test_a_declined_record_sends_nothing_and_is_counted_as_declined(receiver):
    sink = _sink_to(receiver)
    kdd._emit_records([(_B0C0, 0, _DECLINED_PAYLOAD)], _IMEI, io.StringIO(),
                      {}, clock=lambda: 1.0, gsmtap=sink)
    assert _drain(receiver) == []
    assert sink.sent == 0 and sink.declined == 1


def test_a_code_with_no_gsmtap_encoder_is_not_counted_at_all(receiver):
    """A 0xB193 measurement is not a GSMTAP candidate; counting it as
    `declined` would make the declined tally read as a framing problem on
    every drive."""
    import struct
    # A 0xB193 diaggrok DECODES (v=1, one synthetic v3 subpacket, as in
    # test_kismet_diag_decode.py): an unparseable one never reaches the tap,
    # and this test would pass with the eligibility check deleted.
    b193 = (bytes([1, 1]) + struct.pack("<H", 0) + bytes([0, 3])
            + struct.pack("<H", 4) + struct.pack("<HH", 800, 6) + bytes(4))
    assert kdd.diaggrok.parse(0xB193, 0, b193) is not None
    sink = _sink_to(receiver)
    kdd._emit_records([(0xB193, 0, b193)], _IMEI, io.StringIO(), {},
                      clock=lambda: 1.0, gsmtap=sink)
    assert _drain(receiver) == []
    assert (sink.sent, sink.declined, sink.errors) == (0, 0, 0)


def test_the_sink_does_not_change_what_goes_to_kismet(receiver):
    """A tap, not a filter: the JSON stream is identical with and without it."""
    recs = [(_B0C0, 0, _FRAMED_PAYLOAD), (_B0C0, 0, _DECLINED_PAYLOAD)]
    a, b = io.StringIO(), io.StringIO()
    kdd._emit_records(recs, _IMEI, a, {}, clock=lambda: 1.0)
    kdd._emit_records(recs, _IMEI, b, {}, clock=lambda: 1.0,
                      gsmtap=_sink_to(receiver))
    assert a.getvalue() == b.getvalue()


def test_a_send_failure_does_not_kill_the_stream_or_read_as_an_enrich_failure(
        receiver, monkeypatch, capsys):
    """A sendto error is a transport fault in the sink. It must neither stop
    decoding nor land in the census's enrich_failed column, which would
    point an operator at the decoder."""
    sink = _sink_to(receiver)

    def boom(*a, **kw):
        raise OSError(101, "Network is unreachable")

    monkeypatch.setattr(sink, "_send", boom)
    inv = kdd.DiagInventory(wire_keys=True)
    out = io.StringIO()
    kdd._emit_records([(_B0C0, 0, _FRAMED_PAYLOAD)] * 3, _IMEI, out, {},
                      clock=lambda: 1.0, inventory=inv, inv_interval=0,
                      gsmtap=sink)
    assert sink.errors == 3 and sink.sent == 0
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    census = [x for x in lines if x.get("type") == "diag_inventory"][-1]
    assert all(not k.get("enrich_failed") for k in census.get("keys", []))
    # One stderr line for the first failure, not one per record.
    assert capsys.readouterr().err.count("gsmtap send to") == 1


def test_finish_reports_the_tally_on_stderr(receiver, capsys):
    sink = _sink_to(receiver)
    kdd._emit_records([(_B0C0, 0, _FRAMED_PAYLOAD),
                       (_B0C0, 0, _DECLINED_PAYLOAD)], _IMEI, io.StringIO(),
                      {}, clock=lambda: 1.0, gsmtap=sink)
    err = capsys.readouterr().err
    assert "gsmtap" in err and "sent=1" in err and "declined=1" in err


@pytest.mark.parametrize("spec,want", [
    ("127.0.0.1:4729", ("127.0.0.1", 4729)),
    ("10.1.2.3:5000", ("10.1.2.3", 5000)),
    ("localhost:4729", ("127.0.0.1", 4729)),
])
def test_the_flag_parses_host_port(spec, want):
    assert kdd._parse_gsmtap_arg(spec) == want


@pytest.mark.parametrize("spec", [
    "127.0.0.1", ":4729", "127.0.0.1:", "127.0.0.1:0", "127.0.0.1:65536",
    "127.0.0.1:47x9", "no-such-host.invalid:4729", "::1:4729",
])
def test_a_bad_flag_value_is_a_usage_error_not_a_dead_helper(spec, capsys):
    with pytest.raises(SystemExit) as e:
        kdd.main(["--imei", "0", "--gsmtap", spec, "--replay", "/dev/null"])
    assert e.value.code == kdd.EXIT_USAGE


def test_without_the_flag_no_socket_is_opened(monkeypatch):
    """Default off: an unconfigured source must not open a socket."""
    opened = []
    real = socket.socket

    def spy(*a, **kw):
        opened.append(a)
        return real(*a, **kw)

    monkeypatch.setattr(socket, "socket", spy)
    kdd._emit_records([(_B0C0, 0, _FRAMED_PAYLOAD)], _IMEI, io.StringIO(),
                      {}, clock=lambda: 1.0)
    assert opened == []


def test_a_decoder_without_the_gsmtap_framer_still_decodes_and_hides_the_flag(monkeypatch):
    """The framer is diagmunge's, and a standalone build fetches only diaggrok
    The bridge must still import and decode there (the Python
    baseline is permanent), and must NOT advertise --gsmtap: the C source probes
    --help for it and disarms gsmtap= loudly when it is missing, rather than
    exec'ing an argv the bridge would die on at argparse."""
    import sys
    monkeypatch.setitem(sys.modules, "diagmunge.gsmtap", None)
    monkeypatch.setitem(sys.modules, "diagmunge.gsmtap_sink", None)
    bare = _load_bridge()
    assert bare._gsmtap is None
    out = io.StringIO()
    bare._emit_records([(_B0C0, 0, _FRAMED_PAYLOAD)], _IMEI, out, {},
                       clock=lambda: 1.0)
    with pytest.raises(SystemExit) as e:
        bare.main(["--imei", "0", "--gsmtap", "127.0.0.1:4729",
                   "--replay", "/dev/null"])
    assert e.value.code == bare.EXIT_USAGE
    with pytest.raises(SystemExit):
        bare.main(["--help"])


def test_the_flag_is_advertised_where_the_c_probe_looks(capsys):
    """helper_supports_flag() matches a --help line whose first token is the
    flag. Pin that the real help output has one."""
    with pytest.raises(SystemExit):
        kdd.main(["--help"])
    lines = capsys.readouterr().out.splitlines()
    assert any(l.lstrip().startswith("--gsmtap ") for l in lines)
