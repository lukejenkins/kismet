# `kismet_cap_cell_diag` — Qualcomm DIAG cell-observation capture source

A Kismet datasource that opens a modem's **DIAG** port, arms a LOG mask, deframes
the HDLC stream, and forks a **Python bridge decoder** (`kismet_diag_decode.py`,
in this directory) that turns DIAG records into `cell_observation` JSON.
Observations are merged with the modem's AT-sourced cells in `phy_cell`, tagged
`src=diag`.

Source spec: `celldiag-<15-digit IMEI>` (e.g. `-c celldiag-123456789012345`).

## The decoder: fetch it once, configure nothing

The DIAG bytes are deframed in C, but **decoded in Python** by the
`kismet_diag_decode.py` bridge, using the `diaggrok` decoder library. Fetch the
library once, at the pinned release this tree was tested against:

```sh
make -C capture_cell_diag -f standalone.mk deps   # fetches the pinned diaggrok into .deps/
```

That is the whole setup. There is no source option, environment
variable or build flag that names a decoder:

- **Decoder root:** the binary's own directory, or one of its 4 parents, that
  holds `.deps/diaggrok/src` -- so a binary in `capture_cell_diag/` or in
  `capture_cell_diag/build-baseline/` finds it. An installed binary (`make
  install`) falls back to the copy installed in
  `<datadir>/kismet/cell`. If neither has a decoder, the source **refuses to
  open** and names `make -f standalone.mk deps`, rather than start a bridge that
  cannot import its decoder and relays nothing.
- **Bridge:** `kismet_diag_decode.py` beside the decoder root.
- **Interpreter:** `python3` from `$PATH` (then `/usr/bin/python3`), with the
  decoder root's `.deps/diaggrok/src` put **first** on the child's
  `PYTHONPATH`. `diaggrok` declares no third-party dependencies, so a bare
  system `python3` is a complete install.

To decode with a different `diaggrok` (a working tree you are changing), make
`.deps/diaggrok` be it -- e.g. a symlink to a directory with the same
`src/diaggrok` layout -- and put an interpreter that can run it first on
`PATH`. `make -f standalone.mk deps` refuses to fetch into such a link; pass
`DIAGGROK_DIR=<another directory>` to fetch the pinned release beside it.

A definition that still carries the removed `helper=` option is **refused**
at open, not ignored: it used to select the decoder, and silently ignoring it
would switch that source to a different decoder without a word.

**If the decoder is missing, the source fails the open with an actionable
message**, e.g. `celldiag: decoder not found - this binary's tree (...) has no
fetched decoder`, rather than spawning a child that exits asynchronously and
leaves a bare `obs=0 | helper=dead`, easily misread as "no cells in range."

## The Python-decode baseline

The **Python-decode baseline** is the reference configuration:

> `kismet_cap_cell_diag` built with **zero native decode objects** — plain
> `make` — decoding every DIAG payload via the forked Python bridge
> `kismet_diag_decode.py` in this directory, producing the full and correct set
> of Kismet `Cellular` devices.

The C side of that configuration is small: HDLC unescape + CRC-CCITT, the
`LOG_CONFIG` narrow-mask handshake, a DIAG port open, and a pipe to the Python
bridge. The optional C++ decode legs are layered on top of it under these
rules:

| # | Rule |
|---|------|
| 1 | **The baseline must remain buildable, runnable and correct.** No native-decode change may make it unbuildable, unrunnable or incorrect. |
| 2 | **Native decode is additive and optional.** `make NATIVE=1` adds the C++ legs. `nativedecode=off` is the **default** and a fully supported production mode with **equal output**. Equality is measured, not assumed: the binary checks it on every run and the offline replay A/B asserts it. |
| 3 | **Python may not fall behind.** Any decode capability added in C++ must exist in the Python bridge too, in the same change. |
| 4 | **Improving the Python path is welcome.** Existing native work need not be reverted or cleaned up to make room for it. |
| 5 | **Native coverage is never a release gate.** |

These rules cover the DIAG decode path only. `capture_cell_at/` is pure C
(`at_serial.inc`, `cellat_options.c`, `cellat_portscan.c`) and stays that way;
`kismet_cap_cell_at` being a pure C `capture_framework` binary is Kismet's own
idiom.

### What enforces each rule

| rule | enforced by |
|---|---|
| 1 (buildable) | `make -f standalone.mk baseline` — builds with **no autoconf input at all**, on a tree that has never run `./configure`. Output is `build-baseline/kismet_cap_cell_diag`, **not** `./kismet_cap_cell_diag`: it is a *check* artifact, and writing it to the shared path would clobber a configured tree's helper |
| 1 (correct) | `make -C ../celltools replay-gate` — builds both configurations out-of-tree, replays one capture through each, asserts observation-for-observation equality. Proves the two binaries differ via `nm` *before* comparing, and rejects an empty observation set. **Non-mutating**: it restores whichever mode this tree was in — including *unbuilt* — so a measurement target never leaves you measuring a different artifact than the one you built. `make -C ../celltools replay-gate-restore` puts the tree back by hand after an interrupted run |
| 1 (decodes, on in-tree input) | `make -f standalone.mk functional` — replays the in-tree canned capture `fixtures/functional.hdlc` through the decode bridge under a system `python3` with only the pinned decoder on `PYTHONPATH`, and asserts a **non-empty** observation set matching `fixtures/functional.expected.json`. This is the only rule-1 check that needs no external capture: the `replay-gate` row above needs one from `$CELL_CAPTURES` and *skips* without it, and a skip is not a proof. Needs no compiler, no autoconf, no libwebsockets and no Kismet server; it is not Linux-only. The fixture is **synthesised, not scrubbed**, so it holds no real identifiers. Regenerate with `python3 fixtures/make_functional_fixture.py --out fixtures/functional.hdlc` |
| 2 (additive) | `../diagspec/check_binary_symbols.sh` role `baseline` — legs **absent**, `extern "C"` surface **present** |
| 2 (queryable) | `diag_native_available()` — the configuration is a runtime property, not something readable only with `nm`; a baseline binary **refuses** `nativedecode=shadow` and `nativedecode=on`, and names the rebuild |
| 3, 4, 5 | review — these are judgement rules and no test replaces reading them |

**Rule 3 cannot be satisfied inside this checkout alone.**
`../diagspec/enrichment/` carries **`cpp/` only** — there is no Python decode
semantics anywhere in this tree. The bridge here only renders what `diaggrok`
decodes, so the parity target for a new C++ leg is always upstream: the
`diaggrok` parsers and observation mapping the bridge imports. A leg added here
with nothing done in `diaggrok` has not met rule 3.

## Build configurations: `make` vs `make NATIVE=1`

The default build is the shipping configuration.

```sh
make              # Python-decode baseline (the DEFAULT)
make NATIVE=1     # + the C++ decode legs, for nativedecode=shadow|on
```

**`make` — the Python-decode baseline.** Eleven pure-C objects plus
`diag_native_decode_stub.c`. No C++, no `../diagspec/` dependency, no
`libstdc++` in `ldd`. Every DIAG payload is decoded by the forked Python
bridge. It must stay buildable, runnable and correct; native decode is
additive and optional, and anything the C++ side learns to decode the Python
bridge must learn in the same change.

**`make NATIVE=1`** needs the C++ decode sources under `../diagspec/generated/`
and `../diagspec/enrichment/`; a tree without them stops at once with that
reason. It additionally links the extern `"C"` shim and the C++
decode legs, which is what `nativedecode=shadow` measures with. It is opt-in
because the *runtime* default is `nativedecode=off`: an unconditional build
links, verifies and ships ~15 C++ objects the default configuration never calls,
and a build should contain what it executes.

A binary built the default way **refuses** `nativedecode=shadow` at source-open
time and names the rebuild. It does not quietly arm a tap with nothing behind
it: the stub answers "no leg" to every code, so the tap would report
`bridge-only=100%` — a figure identical in shape to a real stream carrying
nothing the legs handle, which is the exact measurement shadow mode exists to
produce.

### `nativedecode=<mode>` — the runtime switch

| mode | who renders the `cell_observation` | build |
|---|---|---|
| `off` *(default)* | the Python bridge, exclusively | any |
| `shadow` | the Python bridge; the C legs decode and **count**, emitting nothing | `NATIVE=1` |
| `on` | the **C legs**; the bridge's `cell_observation` line is suppressed | `NATIVE=1` |

**`off` is a fully supported production mode and stays the default** (rule 2
above). `on` is additive and optional.

**`on` does not stop the helper.** It suppresses exactly one line kind. The
bridge is still fed every byte and still produces the `diag_inventory` census,
the `diag_f3` relay and the `gps_fix` line — it is their only producer — so `on`
is a change of *renderer*, not a decision to run without Python.

That is also what makes the A/B free: both renderings of the same stream exist
at once, so the source counts them (`emitted` vs `bridge-suppressed` in the tap
readout) and reports their agreement itself at spindown, at `MSGFLAG_ERROR` if
they differ. The off-vs-on comparison is therefore a property of **every run on
real hardware**, not of a fixture somebody remembered to capture.

A baseline build refuses `on` for a **stronger** reason than it refuses
`shadow`: shadow over the stub publishes a wrong measurement, whereas `on` over
the stub would suppress the only renderer present and emit nothing in its place
— a source that opens cleanly, reports a healthy byte rate and produces zero
observations, which on a live capture is indistinguishable from "no cells in
range".

Both configurations are asserted at link time by
`../diagspec/check_binary_symbols.sh`, at opposite polarities: role `helper`
requires every declared leg to be present, role `baseline` requires all of them
to be absent *while* the extern `"C"` surface the capture path calls survives.
Switching `NATIVE=` relinks — `.native_mode` records which mode the on-disk
binary was built under, because `make` after `make NATIVE=1` is otherwise
up-to-date by timestamp and would leave the wrong binary in place while exiting 0.

`make check` builds and runs all eleven selftests plus the C++ shim's own in
**both** modes, so the native work stays compiled and testable rather than
rotting behind an off switch.

## Common source options

| option | meaning |
|---|---|
| `diagport=/dev/ttyUSBx` | override DIAG-port auto-detection |
| `spc=000000` | send a `DIAG_SPC_F` unlock (6 digits) before `LOG_CONFIG` — needed on SPC-gated modems (EG25-G / EC2x); harmless to omit on SDX24/SDX55 |
| `nomask=true` | passive LOG tap — skip the `LOG_CONFIG` handshake (raw stream only) |
| `mask=full` | subscribe to every advertised LOG code — HIGH volume, diagnosis not wardriving. Whichever mask the capture armed is **cleared when it lets go of the port** — on a REST close, a Ctrl-C, or an open that fails after the handshake — with `LOG_CONFIG` DISABLE, so a `mask=full` stop does not leave the modem streaming megabytes per minute at the next DIAG client. `nomask=true` arms nothing and clears nothing |
| `f3=all` | arm every F3 debug subsystem at every severity (`0x79`/`0x99` flood) — diagnosis not wardriving |
| `f3=high` / `f3=error` | as `f3=all`, minus sites that only ever print below HIGH / below ERROR — ~94% / ~68% of records kept ([why subtractive](#f3-severity-floors)) |
| `qsh=on` | full F3 surface — arm the QSH-trace `0x9001` maskset (`0x9D`/`0x92`) + `0x60` events + a `diag_id` binding request as a **gated tier** over the same DIAG fd. Off by default; for debug captures only (matches diaggulp `--qsh-*` defaults). Non-fatal on parts that don't implement it (SDX20 status≠0 / MDM9607 `BAD_CMD`). **Disarmed on stop**, on a REST close or a Ctrl-C: the QSH-trace latch (it survives reboots), F3, and the `0x60` event stream the arm turned on. A passive `celldiag_probe --no-mask` tap after the stop confirms it. |
| `replay=<file>` | decode a saved raw DIAG capture instead of a live port |
| `rawlog=<spec>` | tee the raw DIAG stream to disk (`%m` = model, `%i` = IMEI, `%t` = UTC stamp). **One tee per capture session, never truncated:** a reopen (a restart-to-apply toggle, the whole-modem switch, Kismet's error reopen) that finds the file non-empty tees to `<stem>-<UTC ts><ext>` instead, and the `.capture_meta.json` / `.clock_anchor.jsonl` companions follow it. Each tee then matches exactly one kismetdb DIAG stream |
| `rawpackets=off` | stop sending the raw DIAG stream into Kismet as DLT 147 packets (default **on**; see [below](#raw-diag-in-the-kismetdb-rawpackets)) |
| `enabled=false` | register the source but leave it idle (no port open, no helper) |

## Runtime options

Kismet ships exactly **one** generic runtime setter for a datasource —
`POST /datasource/by-uuid/<uuid>/set_channel` — so every knob multiplexes
through that single string, in the same `key=value` spelling used above:

| runtime setting | effect |
|---|---|
| `rawlog=<spec>` | start or **redirect** the tee mid-capture. The new sink is opened *before* the old one is closed, so a bad path costs you nothing |
| `rawlog=off` / `rawlog=none` | stop the tee. `rawlog_path` is deliberately **retained** — a stopped tee must stay distinguishable from one that was never configured |

**The retention above is why `rawlog_active` exists.** Keeping the
path across a stop is right — it answers *"where did the bytes I already
captured go?"* — but it means the path answers only that, and never *"is it
writing?"*. `rawlog_active` reports the sink's fd directly, and the three
states are read from the pair:

| `rawlog_path` | `rawlog_active` | meaning |
|---|---|---|
| absent / `""` | absent / `0` | no `rawlog=` was ever configured |
| set | `true` | the tee is open and writing |
| set | `false` | the tee **stopped**; the file named is complete |

A consumer that derives liveness from the path alone collapses the last two
rows and can never offer to restart. `rawlog_bytes` no longer rising is the same
information, but it is a *derivative*: it needs two ticks and a memory of the
previous one, so nothing renderable from a single object can use it.

`mask=` and `f3=` are **recognised and refused** at runtime, with a message
saying so: applying either means re-issuing a `LOG_CONFIG` /
`SET_ALL_RT_MASKS` handshake on the DIAG fd the capture thread is blocked
reading. Use `close_source` + `open_source` with a new definition instead. An
unknown key is refused *differently*, so "not settable now" and "no such
option" never look alike — the operator's next action differs.

**A refused setting is reported in the MESSAGE, not the status code.** The
framework maps only a *negative* `chancontrol_cb` return to failure on the
wire, and a negative return also sets `spindown` — so reporting a typo through
the status code would end a running wardrive. celldiag returns 0 (not fatal,
and the bogus string is not adopted as the source's "channel") and sends the
reason as a `MSGFLAG_ERROR` bus line.

**An APPLIED setting returns 0 too**, as in cellat. A return above 0 records
the string as the source's **channel**, so the server would display
`rawlog=/x` as `kismet.datasource.channel` and **replay** it after an error
re-open, re-arming a tee the operator may have stopped since. A runtime setting
is a one-time change to a running process; "channel" is reserved for band/RAT
locks (cellat's).

### What the source reports about its switches and its bring-up

Beside the fields above, the `diag_stats` line carries:

| field | meaning |
|---|---|
| `switches_reported` | `1` once a helper that reports the next three groups has sent a line. Read nothing below while it is `0` |
| `rawpackets_on`, `rawpackets_slices`, `rawpackets_dropped` | `rawpackets=` in force, raw DIAG slices delivered to the kismetdb, and slices lost to a full ring |
| `qsh_requested`, `qsh_armed` | `qsh=on` was asked for, and the arm was sent this session. Live sources only |
| `anchor_count`, `anchor_last_epoch` | ClockAnchor rows completed; `0` epoch = none yet. Live sources only |
| `bringup_ms`, `bringup_phases`, `bringup_error` | the deferred bring-up: phases finished so far (`at_scan=1204ms port_detect=3ms …`, sent after each phase), the total, and why it failed |

So that a failed bring-up does not leave the source reading only `IPC
connection closed`, the helper sends `bringup_error` (and `mask_preset`
`failed`) before it exits, and the server
uses it as the source's error reason: `bring-up failed: Could not locate DIAG
interface … Pass diagport=/dev/ttyUSBx explicitly.` The fields are cleared at
every open, so a retry that succeeds does not keep the last attempt's error.

### Frames that fail CRC: a stream losing bytes

A busy stream can lose byte runs between the modem and `read()`: the tty
overflows, the kernel logs nothing, and every integrity check on the kismetdb
passes, because the kismetdb faithfully holds what the helper *read*. The one
trace is DIAG frames that fail their CRC. The helper counts them in **every**
build and mode. With `nativedecode=` armed the tap's own stream counts them;
otherwise a count-only census (unescape + CRC, no record extraction) is fed the
same bytes.

| where | what |
|---|---|
| `diag_stats`: `crc_checked`, `crc_ok`, `crc_bad`, `crc_damage_alerts` | the running census; the server's `kismet.datasource.celldiag.crc_*` fields. `crc_reported` is `0` until the first report, so a `0` below it is never a measurement |
| ERROR line `… DIAG stream losing bytes: N CRC-bad frame(s) in the last Ts …` | once per `stats_interval` window with **≥ 10** CRC-bad frames, and once more at end-of-stream for the last window |
| the source's **warning** | set by the server when `crc_damage_alerts` grows; it stays until the source reopens |
| `data` row `type="DiagCrcCensus"` | sent once at end-of-stream, after the final flush: `{"schema":"diag-crc-census/1","session_id":N,"frames","crc_checked","crc_ok","crc_bad","short","oversize","damage_alerts","counter":"native"\|"census"}`. `session_id` (the raw stream's) is present only with `rawpackets=on`. This row, not the `datasources` row, is the record: that one is rewritten every 30 s and its shutdown rewrite does not land |

A healthy stream shows at most the partial first and last frames (≤ 2 bad in
the large majority of captures), so the floor of 10 bad frames per window
never fires on the edges. A damaged frame is counted and dropped, never decoded.

**What causes it:** not host load but DIAG throughput. If the read loop hands
each chunk to the Python decoder with a blocking pipe write, then when the
decoder falls behind a burst the loop stops reading the tty and the USB-serial
driver drops the overflow. The read loop is therefore decoupled from the
decoder (next section).

### The decoder never stalls the read loop

The pipe into the Python decoder is non-blocking. A read the decoder cannot take
at once is **queued** in memory, and the poll loop writes the queue into the pipe
as it drains. The read loop never waits on the decoder, so a burst the decoder
cannot keep up with does not back up into the tty.

If the queue fills (32 MiB by default, about 10 s of a 3 MB/s burst), a whole
read is **dropped from decoding only**:

- **The capture stays whole.** `rawlog=`, the kismetdb raw stream
  (`rawpackets=`), the native tap and the CRC census see every byte before the
  queue is involved.
- **What is lost is observations** from the dropped span, and the source says so.
- **A replay never drops.** Its input can wait, so a replay reads only while the
  queue has room.

| where | what |
|---|---|
| `diag_stats`: `bridge_queued`, `bridge_peak`, `bridge_dropped_bytes`, `bridge_dropped_chunks` | The queue now, its high-water mark, and what overflow dropped. These are the server's `kismet.datasource.celldiag.bridge_*` fields. `bridge_reported` is `0` until the first report. |
| ERROR line `… decoder fell behind: …` | Once, at the first drop. |
| the source's **warning** | Set by the server at the first drop. |
| `data` row `type="DiagBridgeDrop"` | Sent once, at the first drop: `{"schema":"diag-bridge-drop/1","session_id":N,"first_drop_stream_offset","queue_bytes","dropped_chunks","dropped_bytes"}`. `session_id` is present only with `rawpackets=on`. The running totals are in `diag_stats`. |

`CELLDIAG_BRIDGE_QUEUE_BYTES=<n>` in the helper's environment sets the queue
size. Unset, invalid or `0` means the default. The tests use a small queue so
they reach overflow in test time.

The web panel has a switch for `rawpackets=` and `qsh=` and a cadence field for
`clock_anchor_sec=`. Each is a close, `update_definition`, and re-open: none
of the three can change under a running capture.

NOTE: `set_tune_capable(true)` is what makes any of this reachable, and it is what
Kismet's generic datasource UI keys on to render **channel** affordances — so
celldiag's source row offers a channel control for a source with no channels.
Accepted deliberately: it is currently the only surface through which the knob
can be driven at all. See the note in `datasource_cell_diag.h`.

## Raw DIAG in the kismetdb (`rawpackets=`)

By default celldiag sends the raw DIAG byte stream itself into Kismet, not only
the observations decoded from it. The stream arrives as packets with link type
**147 (`LINKTYPE_USER0`)**, and the server logs them like any link frame:
they land in the kismetdb **`packets`** table, and in a pcapng log if one is
enabled. A drive's `.kismet` therefore carries the raw stream, and `rawlog=` is
not the only copy. `rawlog=` remains as the external file tee;
`rawpackets=off` turns the in-db copy off.

**Each packet is a slice of the stream, not a single frame.** The full contract
is in `diag_rawpkt.h`:

| bytes | field | |
|---|---|---|
| 0–3 | magic | `CDRH` |
| 4 | version | `1` |
| 5 | flags | bit 0 = `FRAGMENT` (slice does not end on a frame boundary); bit 1 = `END` (end of stream, see below) |
| 6–7 | header_len | `24`, little-endian — skip this many bytes to reach the payload |
| 8–15 | session_id | LE, host `CLOCK_REALTIME` ns when this stream began |
| 16–23 | offset | LE, byte offset of the payload within the stream |
| 24– | payload | the bytes exactly as read from the DIAG port (HDLC-escaped, CRC and `0x7E` included) |

For one `(datasource, session_id)`, sort the slices by `offset` and
concatenate the payloads. The result is the stream, byte for byte: the same bytes
a `rawlog=` tee opened at the start of the capture writes. A gap between one
slice's end and the next slice's offset is exactly the bytes that were not
delivered.

**A Kismet stop lets the source finish first (the graceful close).**
The server asks the helper to close and waits for its teardown, so the residual,
the `END` slice and the `END` clock anchor all land. Whether the stop is a REST
`close_source`, a server SIGTERM or a terminal Ctrl-C (SIGINT to the whole
process group), the in-db stream is byte-identical to its `rawlog=` tee and
ends in a confirmed `END` with no gaps. The rest of this note is about a stop
WITHOUT it: an older helper, a close that ran out of its grace, or
`datasource_graceful_close=false`.

**Without the graceful close, the in-db stream can end one read before the
stop.** Every delivered slice still equals the tee at its offset, but the read
in flight when the server closes its pipe is lost: nothing carries it to the
server after that. A SIGTERM to the server can therefore leave a stream short
by the frames read in the last few hundred milliseconds. Helpers built before
the capture framework woke its I/O loop on commit can lose up to about a second
of tail. Keep `rawlog=` when the last frame matters.

**How a reader sees the end of a stream.** A gap needs a delivered slice
after it. So a slice dropped after the last delivered one leaves no gap, and
without a marker a stream that lost its tail would read as whole. There are three
witnesses:

- **The `END` slice.** A stream that reaches its teardown closes with one: an
  empty payload, and `offset` = the final stream length, counting every byte,
  delivered or dropped. Tail loss is then an ordinary gap, the one before `END`.
  - A Kismet-driven stop reaches the teardown through the graceful close, so
    it ends in `END`. A stream still has none when it comes from an older
    helper, a close that ran out of its grace, a server run with
    `datasource_graceful_close=false`, or a source that died (next witness but
    one). Its end is then unconfirmed: whole, or short by the read(s) in flight
    at the close (see above).
- **The `RawDiagDrop` row.** The first time a live source drops a slice, it also
  writes one `data`-table row, `type="RawDiagDrop"`:
  `{"schema":"raw-diag-drop/1","session_id":…,"first_drop_offset":…,"first_drop_len":…}`.
  - This row is the only per-source record of a drop. The `messages` table,
    where the ERROR line lands, records no datasource.
  - With it, a reader knows a live stream is holed even when the drop was the
    tail.
  - Without the graceful close, neither witness covers the reads lost at the
    stop (see above): they were not dropped, so no row fires, and no `END`
    follows them. A first drop in the last moments before such a stop is
    invisible too, when neither the row nor `END` can land.
- **The `RawDiagAbort` row, written by the SERVER.** A source that dies
  mid-stream reaches no teardown: its helper killed by the capture framework's
  15 s PING watchdog while the server stalled, a crash, an OOM kill. There is no
  `END`, and the helper's out-ring (up to 4 MiB) dies with it, queued, not
  dropped, so no `RawDiagDrop` row either. The helper cannot report that, so
  the server's celldiag datasource does. It follows each raw session from the
  slice headers, and when the source fails after data and before `END` (and the
  server is not itself stopping), it writes one `data`-table row:
  `{"schema":"raw-diag-abort/1","session_id":…,"delivered_end":…,"slices":…,"reason":…}`.
  - `delivered_end` is one past the highest stream byte the server received.
    The rest of the stream is gone, and no offset can show how much.
  - A server stall long enough to trip the watchdog writes one row per lost
    session, with `delivered_end` matching the delivered slices exactly. A
    `rawlog=` tee, if one was running, holds the bytes the row says are
    missing.
  - A stop the server asked for is not a death and writes no row.

**Order comes from `offset`, never from row order.** Kismet hands packets to
one of four or more worker threads at random, and each thread inserts its own
rows. Remote sources also have their timestamps overwritten on arrival, and
`kismetdb_to_pcap` selects with no `ORDER BY`. In practice a large fraction of
adjacent rows are out of order, and a row-order concatenation leaves most
frames misplaced.

**Strip `header_len` bytes before deframing.** The header is binary and can
contain `0x7E`. A reader that forgets to strip it merges the header into the
slice's first frame, which then fails its CRC. That loses one frame per packet,
silently.

```python
import sqlite3, struct, collections
db = sqlite3.connect("file:drive.kismet?mode=ro", uri=True)
streams = collections.defaultdict(list)
for src, blob in db.execute("SELECT datasource, packet FROM packets WHERE dlt = 147"):
    magic, ver, flags, hlen, session, offset = struct.unpack_from("<4sBBHQQ", blob)
    streams[(src, session)].append((offset, blob[hlen:]))
for key, slices in streams.items():
    hdlc = b"".join(p for _, p in sorted(slices))   # == the rawlog= .hdlc
```

For real use, `log_tools/kismetdb_to_hdlc` does this properly:

- It reads only celldiag sources, and writes one `.hdlc` per stream, named like
  a `rawlog=` tee.
- It reports gaps, overlaps, and malformed, truncated or unaligned slices on
  every run. It also reports tail loss before an `END`, a bad `END`, and any
  `RawDiagDrop` row. `--strict` refuses to write anything if any are present.
- A stream with no `END` gets a NOTE that its end is unconfirmed and its tail
  may be short (see above). That is not a `--strict` failure, because
  drives recorded by helpers without the graceful close lack `END`;
  `--require-end` makes it one.
- A session with a `RawDiagAbort` row and no `END` gets a WARNING that the
  source DIED mid-stream, and `--strict` refuses it.
- `-l` lists the streams, and `-O <dir>` writes all of them.

Slices hold whole HDLC frames only: every slice ends just after a `0x7E`, so a
lost slice costs whole frames and never corrupts its neighbours. There are two
exceptions, both flagged `FRAGMENT`: a run of more than 12 KiB with no `0x7E` in
it (not DIAG), and the partial frame left when a stream ends. A slice carries the
frames one host read completed, which keeps the per-row overhead to a few
percent. One row per frame would store the stream 2–8× over, since a row has
about 150–200 bytes of fixed columns and the median DIAG frame is 26–107 B.
The 12 KiB cap is set by the **server's** 16 KiB IPC frame limit
(`MAX_EXTERNAL_FRAME_LEN`): a bigger frame gets the whole source errored,
not just dropped.

**Loss policy.**
- **Live source:** when Kismet's capture ring is full, the source drops the
  slice. It writes one `RawDiagDrop` row, then one ERROR line, and counts later
  drops. Waiting would stop the DIAG read, and the overflow would then cost
  `rawlog=` and decoding too.
- **Replay:** the source waits instead, so replaying an `.hdlc` into a
  `.kismet` is lossless.
- **Teardown:** nothing reads the port any more, so the residual and `END` wait
  too (up to about 2 s on a live source).

A replay, or any stop that reaches teardown, ends with a totals line:
`raw DIAG into the packets table (DLT 147): N slice(s), B delivered; …; END at
offset N delivered`. A Kismet-driven stop cancels the capture thread first, so
on that path there is no totals line and no `END`. The stream can also end
short by the read(s) in flight at the close, in whole frames (see above). Setting
`kis_log_packets=false` in `kismet_logging.conf` keeps the slices out of the
kismetdb entirely.

## F3 severity floors

`f3=high` and `f3=error` vary one u32 in the `SET_ALL_RT_MASKS` body — the
**runtime level mask**, which the modem ANDs against each emitting site's
compiled-in `ss_mask`. A site prints iff the AND is nonzero.

| preset | mask | records kept |
|---|---|---:|
| `all` | `0xFFFFFFFF` | 100.0% |
| `high` | `0xFFFFFFFC` | 94.2% |
| `error` | `0xFFFFFFF8` | 68.4% |

**The floors are subtractive, and there is deliberately no selective one.**
The low 5 mask bits are a severity ladder (across qdbs from several vendors,
the share of error-worded format strings rises monotonically with the bit);
bits ≥5 are **per-SSID custom** meanings, not higher severities. So the
intuitive reading of "severity floor" — send `ERROR|FATAL`, i.e. `0x00000018`
— is a **mute button**: about two thirds of F3 records come from sites carrying
only custom bits, so that mask keeps roughly **1%** of the stream and drops the
ML1 / tuner / GNSS prints F3 is wanted for. Clearing low ladder bits from
all-ones silences ladder-only sites and keeps every custom-bit site.

`f3=` therefore takes **named presets only** — a raw mask would hand an operator
that footgun with no way to warn. The percentages in the table are typical
retention on a SIM8202G-M2 drive capture.

**The floor is enforced by the modem at arm time, not by the helper.** There
is no post-filter, so `f3=high|error` on a `replay=` source relays the file's F3
unfiltered — the floor a capture was taken with is a property of that capture.

## Per-modem DIAG quirks

- **EG25-G / EC2x / EG2x (MDM9607):** `LOG_CONFIG` (SET_MASK) is **SPC-gated** —
  the modem rejects the mask until a `DIAG_SPC_F` unlock is sent.
  Pass `spc=000000` (factory default), or set `diag.spc` in the model profile so
  it arms automatically. LM960 (SDX24) and RM500Q (SDX55) do not gate it — omit
  `spc=` there.
- Standalone `./celldiag_probe /dev/ttyUSBx [bytes] [--no-mask]` deframes without
  the Python bridge — use it to isolate port/HDLC problems from decoder problems.

## Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `celldiag: decoder not found` at open | no `.deps/diaggrok` in the binary's tree (nor an installed one) | `make -f standalone.mk deps` in `capture_cell_diag/` (then `make install` again, for an installed binary) |
| `no Python interpreter found` / `decoder interpreter not runnable` | no `python3` on `PATH` or at `/usr/bin/python3` | install `python3` |
| `the helper= source option was removed` | a definition written for an older version | delete `helper=…` from the definition |
| decode opens but `obs=0` / `RX-NO-OBS` | the `python3` first on `PATH` cannot run the decoder in `.deps/diaggrok` | `celltools/kismet-host-preflight.sh` [3/6] runs the same import and names the interpreter |
| `decoder script not found` | a decoder root with no `kismet_diag_decode.py` beside it | re-run `make install`, or build from a complete tree |
| `LOG_CONFIG handshake failed ... SPC-gated?` | EC2x SPC gate | pass `spc=000000`; see [Per-modem DIAG quirks](#per-modem-diag-quirks) |
| `DIAG SPC unlock (spc=000000) rejected` after ~20 s, in a multi-source open, on a unit whose SPC works alone | another process wrote into the DIAG port before the open (a `cellat` IMEI scan writes `AT\r\n` to every port it can claim); the modem glued those bytes to the SPC frame, whose CRC then failed | fixed in current helpers: the DIAG port open sends one lone `0x7E` first, which ends any such partial frame. Rebuild an older helper |
| the scan reads `AT identify: IMEI … not found while N port(s) were held by another process` | a concurrent source was probing this modem's AT ports at that instant | nothing: one rescan follows after 250–775 ms. `CELLDIAG_SCAN_PORTS="<path> <glob> …"` **replaces** the scanned set (`/dev/ttyUSB*` + `/dev/ttyACM*`); `atport=` names one AT node and skips the scan |
| the scan reads `AT identify: port(s) still closing after another probe, open() gave up at 1000 ms and skipped them: ttyUSB18(open-timeout)` | another probe's close of that tty is still waiting out its 30 s `closing_wait` (a port that never read its `AT\r\n`), and Linux blocks every new `open()` of a tty in its final close | nothing: the scan skips the port after 1 s instead of blocking ~30 s. The port is never the target: an AT port reads what it is sent. cellat logs the same line as `cellat AT scan: port(s) still closing …` |
| the web UI lists `RM520N-GL — AT commands` but no `RM520N-GL — DIAG`, or a bare `celldiag-<imei>` for a PCIe modem fails `Modem IMEI … not found on any serial port or non-USB (MHI/wwan) AT node` | an older helper whose lister and bare-definition bring-up walked USB ttys only. Current helpers fall back to the non-USB AT nodes after the USB pass (a held `/dev/mhi_DUN` is read through its holder's vouch) and pick the DIAG node by name | if the row is still missing, `--list` prints `not listing the non-USB modem(s) at …` with the reason: usually more than one modem's DIAG node by name (two PCIe modems), which no name can disambiguate. Name both nodes: `atport=/dev/mhi_DUN,diagport=/dev/mhi_DIAG` |
| an MHI/wwan `diagport=` source (the PCIe RM520N-GL's `/dev/mhi_DIAG`) reads model `unknown` and says `no non-USB AT node answered as this IMEI (held by the paired cellat source, or absent)` | its only AT node, `/dev/mhi_DUN`, was held for the paired cellat's session when the bring-up scanned it | normally nothing: the bring-up scans the non-USB AT nodes only (`/dev/mhi_DUN`, `/dev/wwan*at*`) and reads the model there, `AT identity from /dev/mhi_DUN`, before the cellat source takes the node. A node held throughout costs one rescan, then the source opens without AT identity as before. `atport=/dev/mhi_DUN` names the node explicitly |
| `bytes=` climbs but `obs=0` | helper died mid-stream | check the Kismet log for `celldiag helper:` stderr line |
| under `mask=wardrive`, 2G/3G codes flood in after an airplane / `AT+CFUN=4→1` cycle (`0x508F`, `0x5075`, `0x41C1`, `0x4176`, `0x7001` …) | an older helper. `SET_MASK` is per log type, and a narrow arm that writes only the types holding a target code (1 and 0xB) leaves armed whatever mask a PREVIOUS DIAG client left on types 4/5/7/10/13. That mask sits near-silent while the modem idles on LTE and floods once the cycle forces a multi-RAT search, so it looks like the modem dropping the mask. It does not: current helpers send an all-zero mask to every advertised type without a target code (as diaggulp does), and the cycle then carries no off-mask codes | rebuild the helper; nothing to configure. A WCDMA task may keep emitting `0x4116` for ~30 s after the arm (a cached enable) before it stops |
