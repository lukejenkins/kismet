# `celltools/` — host checks and Python harnesses for the cell capture sources

Test and diagnostic tooling for `capture_cell_at/`, `capture_cell_diag/`, and
`diagspec/` that is **too high-level to be a C unit test**: it drives built
binaries, inspects the build tree, or asserts state that lives on the host
rather than in the tree.

The C unit tests stay where they are — `make check` in each capture directory
builds and runs `test_diag_*.c` / `test_*at*.c`. Nothing here replaces those.

## Why this is a directory of its own

The cell work lives in top-level directories that upstream Kismet does not
have (`capture_cell_at/`, `capture_cell_diag/`, `diagspec/`, and this one).
That is deliberate:

- **Merges from upstream never touch these paths**, so catching up costs nothing
  here.
- **The cell tooling stays self-contained.** Putting these files in upstream's
  `tools/` would mix upstream code and cell-specific code in one directory, and
  make it harder to see which is which.

## Running

```sh
make -C celltools check          # everything
python3 -m pytest celltools/tests -q
python3 -m pytest celltools/tests/test_kismet_host_preflight.py -q
```

**These tests depend on nothing outside this tree.** Standard library plus
`pytest`; no project virtualenv, no sibling checkout, no network. That is a
requirement, not a happy accident — a harness that needs an external checkout to
run is a harness that silently does not run for anyone who lacks one, and a
green suite then means "nobody measured this."

If a future harness here genuinely needs an external decode oracle to define
correctness, it must **skip with the reason named in the skip message** — not
fail, and not pass vacuously. See *The oracle rule* below.

## What is here

| path | what it checks |
|---|---|
| `kismet-host-preflight.sh` | that this **host** is wired to *this* build tree: the `/usr/local/etc/kismet*.conf` symlinks, `kismet_site.conf`'s `helper_binary_path` entries, the celldiag decoder (the tree's fetched `.deps/diaggrok`, or the installed one, importable by `python3`), the data files, whether the `kismet` on `PATH` predates the tree, and a REST credential (there is no way to disable REST auth) |
| `tests/test_kismet_host_preflight.py` | the above, against a synthetic host built under `tmp_path` — it never reads or writes the real `/usr/local` |
| `celldiag_parity.py` | **the foundation.** Finds the built capture binaries in this tree, names *which* silence a miss is (`no-tree` / `not-built` / `not-configured` / `not-configurable` / `build-failed` / `env-bad`), derives each binary's real link closure from the makefile, and answers "is this binary older than its own sources?" — once, so the probe and the test guard cannot drift into disagreeing |
| `decode_oracle.py` | resolving the reference decoder the way the binary does, and the vocabulary for skipping when there is none. See *The oracle rule* below |
| `celldiag_parity_status.py` | the ran-vs-skipped **gauge**: `--build`, `--check`, `--run`. Its verdict comes from pytest's junit report — what ran — not from what could have run |
| `check_binary_staleness.py` | the advisory staleness probe, for whatever advances this tree. Never builds, never fails its caller unless asked (`--check`) |
| `tests/test_celldiag_replay_ab.py` | drives the compiled `kismet_cap_cell_diag` over the datasource IPC with a replay file, and compares its emitted observations against the reference decoder |
| `tests/test_celldiag_logstream_parity.py` | `diag_logstream_dump` vs the reference HDLC→LOG extractor, record for record and counter for counter |
| `tests/test_cellat_ipc.py` | drives `kismet_cap_cell_at` against a scripted AT responder on a pty — no modem, no server |
| `tests/test_celldiag_link_deps.py` | the makefile parser, against a synthetic tree: no build, no compiler |
| `tests/test_celldiag_replay_gate_nonmutating.py` | that `replay-gate` restores the mode the tree entered in — `native`, `baseline`, or **unbuilt**. Synthetic `CELLDIAG_DIR`, so the starting state is a fixture rather than a comment and it runs with no compiler |
| `tests/test_binary_staleness.py` | the staleness predicate and the probe CLI, same synthetic tree |
| `tests/test_celldiag_parity_discovery.py` | the discovery + skip-reason contract: the statuses must stay mutually distinguishable, and each actionable one must carry **its own** remedy |
| `tests/test_celldiag_profile_target_codes.py` | `capture_cell_at/profiles/*.json` `diag.target_codes` vs `DIAG_TARGET_CODES` in `capture_cell_diag/diag_config.c` |
| `tests/kismet_capture_ipc.py`, `tests/fake_at_modem.py` | helpers: the v3 `kismet_external` framing + capture driver, and the pty AT responder |

⚠️ **The harnesses that need no oracle are the majority.** Discovery, link-dep
derivation, staleness and the profile check are pure introspection of this tree
and run everywhere. Only the two parity harnesses and the replay A/B need a
fetched decoder (`make -f standalone.mk deps` in `capture_cell_diag/`), and they
name that in their skip message when it is missing.

### `kismet-host-preflight.sh`

Run it before any live Kismet gate:

```sh
celltools/kismet-host-preflight.sh          # report only; exit 1 on any FAIL
celltools/kismet-host-preflight.sh --fix    # create MISSING symlinks only
```

`--fix` is deliberately timid: it creates absent symlinks and nothing else. It
will not replace an existing regular file and will not run `make install`.
Clobbering host state and installing system-wide binaries are the operator's
call, so both cases print the exact command instead of running it.

**Two different things are called "the helper", and the script checks one in
each of `[2/6]` and `[3/6]`.** `helper_binary_path` is the directory holding the
capture **binaries**, which the *server* needs in order to exec the capture
process. The decode bridge is the fetched decoder plus `kismet_diag_decode.py`,
run under `python3` from `PATH` (see the `capture_cell_diag/README.md` decoder
section), which the *capture process* needs in order to spawn Python. A host
can be green on all four `helper_binary_path` entries and still have every
celldiag source die on open, so the script checks both.

`[3/6]` resolves the decoder exactly as the capture binary does — the
build tree's `capture_cell_diag/.deps/diaggrok`, else the installed copy — so the
host check and the runtime check cannot disagree. It then runs the bridge's own
`import diaggrok` under the `python3` first on `PATH`, because a decoder that
interpreter cannot import is the silent `obs=0` failure. Every miss is a hard
`FAIL`: nothing in the runtime is configurable, so nothing is invisible.

Run it with the `PATH` you will run the gate with: the interpreter is found
there, so a preflight from a different shell may be checking a different
`python3`.

The `[4/6]` data-file group asserts the path the **consumer** opens, not merely
that a file of that name is somewhere on the host. Those are different
questions: a file in `KISMET_ETC` looks installed, but the server opens
`KISMET_SHARE` and logs
`Could not open ICAO database /usr/local/share/kismet/...`. Each entry carries
the name its consumer resolves under `KISMET_SHARE` — which differs from the
`conf/` name for the two bluetooth databases, whose built-in defaults omit
`.gz`. `kismet_uav.conf.yaml` is not checked: it is a build input compiled into
`kismet_uav.conf`, not a file the server reads.

`KISMET_ROOT` defaults to **the tree this script ships in**, not to a checkout
found by convention. A conventional default can resolve to a tree the operator
is not building, and a `PASS` would then mean "some tree is wired up" rather
than "this one is". `KISMET_ETC` / `KISMET_SHARE` / `KISMET_ROOT` / `KISMET_BIN`
all override,
which is how the tests point it at a synthetic host.

## The oracle rule

Some harnesses that belong here define this tree's decode path as correct **by
agreement with an external reference decoder**. That reference is not vendored
and will not be: two copies of a decode truth diverge, and the divergence is
invisible precisely because both sides look self-consistent.

So such a harness must:

1. locate the reference through the **same** mechanism the shipped binary uses —
   this tree's `capture_cell_diag/.deps/diaggrok`, run by `python3` from `PATH`
   (`decode_oracle.py`). One rule, not two, and nothing to configure: to measure
   against a different `diaggrok`, make `.deps/diaggrok` be it.
2. **Skip, with the reason in the message**, when no reference is reachable —
   naming *which* silence it is (`no-decoder` / `no-bridge` / `no-interpreter`).
   "Skipped" with no reason is how a suite reports zero coverage as health.

Anyone running these harnesses as a gate is then responsible for fetching a
decoder so the skip is never taken in a run that matters. The skip exists for
someone who genuinely has no oracle, not as a way to pass a gate.
