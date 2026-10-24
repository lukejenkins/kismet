# SPDX-License-Identifier: Apache-2.0
"""sigchld_probe -- runtime probe of the capture framework's SIGCHLD handling.

## What it measures

The capture framework delivers SIGCHLD to a dedicated signal thread, which
reaps every exited child of the process. Two behaviours are worth measuring
from outside, with no source reading and no debugger:

1. **A spinning reaper.** After the first child exit, does one thread stay
   runnable at ~100% CPU until the process exits? (Measured: yes. On a
   replay source, the first thread created after the helper is spawned goes
   from 0% to 90-110% one 50 ms sample after the helper exits.)
2. **A close that never completes.** When shutdown is already under way as a
   child exits, does the source still finish its close? (Measured: it did,
   in 170 trials with the helper killed 0-120 ms after the close request,
   idle and busy ring alike. Not reproduced is not disproved.)

Use it to verify a fix to the reaper: after the fix, ``close-normal`` and
``kill-helper`` must show no thread above ~50% CPU at any point, and every
``close-kill`` trial must still finish. The sampling functions
(:func:`threads`, :func:`cpu_pct`, :func:`hot_threads`) are importable, so a
gate can assert "no fork after start" and "no thread spinning while idle"
against a helper-less build with the same code.

## How

The binary runs through celltools' own IPC driver
(``tests/kismet_capture_ipc.CaptureRun`` via the replay harness), with
``replay=`` set to a FIFO. A writer thread sends a corpus fixture and keeps
the write end open, so the source never sees EOF and stays alive until the
scenario acts. A side thread samples ``/proc/<pid>/task/*`` for each
thread's name and CPU ticks. The replay fixtures live outside the tree; set
``CELL_CAPTURES``.

Scenarios:
  kill-helper   SIGKILL the helper mid-run; trace threads until exit
  close-normal  send CLOSEREQ; the helper exits during teardown
  close-kill    send CLOSEREQ, then SIGKILL the helper after a swept delay

Linux only (``/proc``). Prints one JSON object per trial.

  CELL_CAPTURES=<dir> python3 celltools/probes/sigchld_probe.py \\
      capture_cell_diag/build-replay-gate/kismet_cap_cell_diag.baseline \\
      close-kill --trials 25 --fine --busy
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

CELLTOOLS = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(CELLTOOLS), str(CELLTOOLS / "tests")]

HZ = os.sysconf("SC_CLK_TCK")
SCENARIOS = ("kill-helper", "close-normal", "close-kill")
#: Kill delays after the close request, in seconds. The fine sweep covers the
#: window in which an exit can race the close; the coarse set spans past it.
COARSE_DELAYS = (0.0, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2)
FINE_DELAYS = tuple(round(0.005 * i, 3) for i in range(25))
#: Above this, a thread on an idle source is spinning, not working.
HOT_PCT = 50.0


# ── /proc sampling (importable) ──────────────────────────────────────────────
def _stat_fields(stat: str) -> list[str]:
    """The fields after ``comm``; comm may itself contain spaces or parens."""
    return stat[stat.rindex(")") + 2:].split()


def threads(pid: int) -> dict[int, tuple[str, int, str]]:
    """``{tid: (comm, utime+stime ticks, state)}`` for every thread of `pid`."""
    res = {}
    for t in Path(f"/proc/{pid}/task").iterdir():
        try:
            f = _stat_fields((t / "stat").read_text())
            res[int(t.name)] = ((t / "comm").read_text().strip(),
                                int(f[11]) + int(f[12]), f[0])
        except (OSError, ValueError):
            pass
    return res


def cpu_pct(a: dict, b: dict, dt: float) -> dict[int, tuple[str, float, str]]:
    """Per-thread CPU% between two :func:`threads` snapshots taken `dt` apart."""
    return {tid: (b[tid][0], round(100.0 * (b[tid][1] - a[tid][1]) / HZ / dt, 1),
                  b[tid][2])
            for tid in b if tid in a}


def hot_threads(pid: int, dt: float = 1.0, limit: float = HOT_PCT) -> dict:
    """Threads of `pid` above `limit`% CPU over a `dt`-second window."""
    a = threads(pid)
    time.sleep(dt)
    return {tid: v for tid, v in cpu_pct(a, threads(pid), dt).items()
            if v[1] > limit}


def children(pid: int) -> list[int]:
    out = []
    for t in Path(f"/proc/{pid}/task").iterdir():
        try:
            out += [int(c) for c in (t / "children").read_text().split()]
        except OSError:
            pass
    return out


def _proc_state(pid: int | None) -> str | None:
    if not pid:
        return None
    try:
        return _stat_fields(Path(f"/proc/{pid}/stat").read_text())[0]
    except OSError:
        return "gone"


def own_child(binary: str, deadline: float) -> int | None:
    """The pid of THIS process's child running `binary`.

    Matching on the executable alone also matches a previous trial's
    process that is still exiting, which then vanishes under the sampler.
    """
    want = os.path.realpath(binary)
    while time.monotonic() < deadline:
        for p in Path("/proc").iterdir():
            if not p.name.isdigit():
                continue
            try:
                ppid = int(_stat_fields((p / "stat").read_text())[1])
                if ppid == os.getpid() and os.readlink(p / "exe") == want:
                    return int(p.name)
            except (OSError, ValueError):
                pass
        time.sleep(0.05)
    return None


def helper_of(pid: int) -> int | None:
    for c in children(pid):
        try:
            if b"kismet_diag_decode.py" in Path(f"/proc/{c}/cmdline").read_bytes():
                return c
        except OSError:
            pass
    return None


def trace_until_exit(pid: int, step: float = 0.05, limit: float = 30.0,
                     helper: int | None = None) -> tuple[list[dict], float]:
    """Per-thread CPU% in `step` slices until `pid` exits (or `limit`)."""
    out, t0 = [], time.monotonic()
    try:
        a = threads(pid)
    except OSError:
        return out, 0.0
    while time.monotonic() - t0 < limit:
        time.sleep(step)
        try:
            b = threads(pid)
        except OSError:
            break
        if not b:
            break
        out.append({"t": round(time.monotonic() - t0, 2),
                    "helper": _proc_state(helper),
                    "cpu": {str(k): v for k, v in cpu_pct(a, b, step).items()}})
        a = b
    return out, round(time.monotonic() - t0, 2)


# ── one trial ────────────────────────────────────────────────────────────────
def trial(binary: str, scenario: str, tmp: Path, delay: float = 0.0,
          busy: bool = False) -> dict:
    import test_celldiag_replay_ab as ab  # needs CELL_CAPTURES for the fixture

    fixture = ab._fixture_or_skip(tmp)
    replay = tmp / "replay.fifo"
    if replay.exists():
        replay.unlink()
    os.mkfifo(replay)
    hold = threading.Event()

    def feed():
        data = fixture.read_bytes()
        try:
            with open(replay, "wb") as w:
                w.write(data)
                w.flush()
                # busy: keep records flowing through the close, so the
                # output ring has traffic when the helper's exit is reaped.
                while not hold.wait(0.05 if busy else 60):
                    if busy:
                        w.write(data)
                        w.flush()
        except OSError:
            pass

    threading.Thread(target=feed, daemon=True).start()
    rec = {"scenario": scenario, "delay": delay, "busy": busy}
    state = {"close": False}
    run = ab.CellDiagRun(binary, ab._definition(replay), timeout=40.0,
                         close_when=lambda r: state["close"],
                         graceful_close_s=20.0, poll_s=0.05)

    def watch():
        pid = own_child(binary, time.monotonic() + 10)
        rec["pid"] = pid
        if pid is None:
            return
        time.sleep(3.0)
        h = helper_of(pid)
        rec["helper_pid"] = h
        a = threads(pid)
        time.sleep(2.0)
        rec["before"] = {str(k): v for k, v in cpu_pct(a, threads(pid), 2.0).items()}
        if scenario == "kill-helper" and h:
            os.kill(h, signal.SIGKILL)
        if scenario in ("close-normal", "close-kill"):
            state["close"] = True
        if scenario == "close-kill" and h:
            end = time.monotonic() + 5
            while run.close_sent_mono is None and time.monotonic() < end:
                time.sleep(0.002)
            time.sleep(delay)
            try:
                os.kill(h, signal.SIGKILL)
                rec["killed_after_close_s"] = round(
                    time.monotonic() - run.close_sent_mono, 4)
            except (ProcessLookupError, TypeError):
                rec["killed_after_close_s"] = "helper already gone"
        rec["trace"], rec["alive_after_event_s"] = trace_until_exit(pid, helper=h)
        if scenario == "kill-helper":
            state["close"] = True

    w = threading.Thread(target=watch, daemon=True)
    w.start()
    t0 = time.monotonic()
    run.run()
    w.join(timeout=5)
    hold.set()
    rec["close_sent"] = run.close_sent_mono is not None
    rec["close_to_exit_s"] = (round(run.exit_mono - run.close_sent_mono, 3)
                              if run.close_sent_mono and run.exit_mono else None)
    rec["exited_by_itself"] = run.exit_mono is not None
    rec["exit_code"] = run.exit_code
    rec["observations"] = len(run.observations)
    rec["wall_s"] = round(time.monotonic() - t0, 2)
    rec["peak_cpu_pct"] = max((v[1] for s in rec.get("trace", [])
                               for v in s["cpu"].values()), default=0.0)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("binary")
    ap.add_argument("scenario", choices=SCENARIOS)
    ap.add_argument("--trials", type=int, default=7)
    ap.add_argument("--fine", action="store_true",
                    help="sweep the kill delay 0-120 ms in 5 ms steps")
    ap.add_argument("--busy", action="store_true",
                    help="re-feed the fixture every 50 ms through the close")
    ap.add_argument("--trace", action="store_true",
                    help="keep the per-slice thread trace in the output")
    args = ap.parse_args()
    if not sys.platform.startswith("linux"):
        print("sigchld_probe: needs /proc (Linux)", file=sys.stderr)
        return 2
    delays = FINE_DELAYS if args.fine else COARSE_DELAYS
    with tempfile.TemporaryDirectory() as td:
        for i in range(args.trials):
            d = Path(td) / f"{args.scenario}-{i}"
            d.mkdir()
            r = trial(args.binary, args.scenario, d,
                      delay=delays[i % len(delays)], busy=args.busy)
            if not args.trace:
                r.pop("trace", None)
            print(json.dumps(r), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
