/* diag_stats.h - per-source health counters for the celldiag source.
 *
 * A small "is the data actually flowing?" readout the operator can glance at to
 * confirm a modem is producing DIAG observations. The counters are updated at
 * three seams in capture_cell_diag.c - the DIAG read, helper liveness, and the
 * observation relay - and formatted into a one-line status the capture loop
 * emits on the Kismet message bus every `stats_interval=` seconds.
 *
 * All of this is pure libc (no Kismet dependency) so the branching logic - the
 * rolling observations/sec window, the health verdict, and the cadence gate -
 * is unit-tested standalone in test_diag_stats.c (`make check`).
 *
 * The formatted line is a stable KEY=VALUE form so it is both human-glanceable
 * and trivially parseable. The web UI gets the same counters through the JSON
 * form below.
 */
#ifndef DIAG_STATS_H
#define DIAG_STATS_H

#include <stddef.h>
#include <stdint.h>
#include <time.h>

/* Rolling observations/sec is measured over a fixed trailing window. Each
 * observation timestamp is retained in a ring; the rate is the count of
 * timestamps newer than (now - WINDOW) divided by WINDOW. If more than SLOTS
 * observations land inside the window the oldest are overwritten and the rate
 * saturates (undercounts) - fine for a health readout, and documented so a
 * reader does not mistake the ceiling for the true peak. */
#define DIAG_STATS_RATE_WINDOW_SEC  10
#define DIAG_STATS_RATE_SLOTS       1024

/* Longest a mask-preset label we bother to store. */
#define DIAG_STATS_MASK_MAX         32

/* Room for the whole extras suffix (config strings, census counters, the
 * native-tap group, the switch groups, the CRC census and the bridge queue
 * group) inside the JSON object, with enough left over for an escaped
 * rawlog_path. A path too long to fit degrades to "path omitted, counters
 * still sent" rather than to a clipped object that parses as nothing.
 *
 * Grow this whenever a new group is added. If the bounded keys eat the path
 * budget, the drop_path retry makes the output LOOK correct (valid JSON,
 * counters present) while rawlog_path silently goes dead on every emit. The
 * retry is a last-resort degradation, not a sizing strategy. */
#define DIAG_STATS_EXTRA_MAX        1440

/* The JSON row type the helper sends once at end-of-stream with the CRC
 * census's final counts (schema diag-crc-census/1). The server logs it to the
 * kismetdb `data` table under the source, as it does a RawDiagDrop row. */
#define DIAG_CRC_CENSUS_TYPE        "DiagCrcCensus"

/* The JSON row type the helper sends once, at the first chunk of
 * decoder input it had to drop (schema diag-bridge-drop/1). Logged to the
 * kismetdb `data` table under the source, like RawDiagDrop. */
#define DIAG_BRIDGE_DROP_TYPE       "DiagBridgeDrop"

typedef struct {
    /* DIAG port / read side */
    uint64_t bytes_read;        /* total raw HDLC bytes read from the source */
    uint64_t read_errors;       /* read() errors observed on the DIAG source */
    time_t   last_read_ts;      /* wall time of the last non-empty read (0=never) */
    int      port_open;         /* 1 while the DIAG source fd is open */

    /* Decode helper side */
    int      helper_alive;      /* 1 while the bridge helper is believed running */
    uint64_t helper_restarts;   /* times the helper was (re)spawned after the first */

    /* Observation side */
    uint64_t obs_total;         /* cell_observation lines relayed since open */
    uint64_t obs_since_status;  /* relayed since the last status emit (reset on emit) */
    time_t   last_obs_ts;       /* wall time of the last relayed observation (0=never) */

    /* Cadence / mask label */
    time_t   last_status_ts;    /* wall time of the last status emit */
    char     mask_preset[DIAG_STATS_MASK_MAX];  /* active mask label, "" if unset */

    /* Rolling-rate ring (private) */
    time_t   rate_ring[DIAG_STATS_RATE_SLOTS];
    size_t   rate_head;         /* next write slot */
    size_t   rate_count;        /* live entries (<= SLOTS) */
} diag_stats_t;

/* Zero the counters and seed the cadence baseline at `now` so the first status
 * fires ~one interval after open (not immediately). */
void diag_stats_init(diag_stats_t *s, time_t now);

/* Record a successful read of `nbytes` at `now`. */
void diag_stats_on_read(diag_stats_t *s, size_t nbytes, time_t now);

/* Record a read() error on the DIAG source. */
void diag_stats_on_read_error(diag_stats_t *s);

/* Record one relayed cell_observation at `now` (feeds obs counters + rate). */
void diag_stats_on_obs(diag_stats_t *s, time_t now);

/* Helper-liveness transitions. spawn() is the first start; restart() is any
 * subsequent respawn (increments helper_restarts). Both set helper_alive=1. */
void diag_stats_on_helper_spawn(diag_stats_t *s);
void diag_stats_on_helper_restart(diag_stats_t *s);
void diag_stats_set_helper_alive(diag_stats_t *s, int alive);

/* Simple state setters. */
void diag_stats_set_port_open(diag_stats_t *s, int open);
void diag_stats_set_mask_preset(diag_stats_t *s, const char *preset);

/* Rolling observations/sec over the trailing DIAG_STATS_RATE_WINDOW_SEC. */
double diag_stats_obs_per_sec(const diag_stats_t *s, time_t now);

/* One-word health verdict for the current state, relative to `stall_after`
 * seconds of read silence:
 *   "NODATA"      - never read a byte from the source
 *   "HELPER-DEAD" - the decode helper is not running
 *   "PORT-CLOSED" - the DIAG source fd is closed
 *   "STALLED"     - reads have stopped (last read older than stall_after)
 *   "RX-NO-OBS"   - bytes arriving, but no observation within stall_after
 *   "FLOWING"     - a recent observation was relayed
 *
 * The two producer states are checked BEFORE the recency states, and both
 * are checked before FLOWING can be returned. Recency is meaningless once the
 * producer is gone: a verdict read only from `last_read_ts` / `last_obs_ts`
 * would keep reporting FLOWING beside `helper=dead port=closed` for a full
 * `stall_after` window. The invariant: the word can never contradict the
 * fields printed next to it.
 * Returns a static string (never NULL). */
const char *diag_stats_verdict(const diag_stats_t *s, time_t now,
                               time_t stall_after);

/* Would a status be due at `now` given `interval` seconds (0 disables)?
 * Returns 1 if due, 0 otherwise. */
int diag_stats_due(const diag_stats_t *s, time_t interval, time_t now);

/* Format the one-line KEY=VALUE status into buf. `stall_after` selects the
 * verdict thresholds (typically the status interval). Returns the strlen
 * written (>=0), or -1 if buf/buflen is unusable. Does NOT mutate `s`. */
int diag_stats_format(const diag_stats_t *s, time_t now, time_t stall_after,
                      char *buf, size_t buflen);

/* The fields the datasource registers that diag_stats does NOT own.
 *
 * Every registered kismet.datasource.celldiag.* tracker field needs a producer;
 * one nobody emits sits at a permanent zero/"" that the web UI renders as a
 * healthy reading ("rawlog_bytes: 0" reads as "on, nothing written yet"). This
 * struct carries the config-owned strings and the python-helper census, passed
 * in by the emit site that owns them, so one JSON object carries all of them.
 *
 * The `*_valid` gates are load-bearing, not defensive noise. A census that
 * has never been reported is NOT a census reporting zero -- emitting
 * "inventory_unrecognized":0 before the helper has ever sent a diag_inventory
 * line asserts "every code is recognized", which is exactly the
 * dead-field-renders-as-a-healthy-zero failure this struct exists to end. When
 * a group is invalid its keys are OMITTED, and apply_diag_stats_json() leaves
 * the corresponding tracker fields untouched.
 */
typedef struct {
    /* Config-owned. NULL string = omit that key. */
    const char *f3_preset;      /* "off"|"all"|"high"|"error" -- the RESOLVED
                                 * preset name, never a boolean re-rendered as
                                 * one: a severity floor reported as "all" tells
                                 * the operator they see the full trace while
                                 * part of it is silenced at the modem */
    const char *rawlog_path;    /* resolved tee path; escaped, may contain "\ */
    uint64_t    rawlog_bytes;   /* bytes actually written to the tee sink */
    int         rawlog_valid;   /* 1 = emit rawlog_bytes (tee configured) */
    int         rawlog_active;  /* 1 = the sink is OPEN RIGHT NOW (rawlog_fd
                                 * >= 0). Not inferable from the two fields
                                 * above, and that is the point: a runtime
                                 * `rawlog=off` closes the sink and KEEPS
                                 * rawlog_path, deliberately, so the operator
                                 * can still find where the bytes went. Path
                                 * retention and tee liveness therefore diverge
                                 * permanently after the first stop, and a
                                 * consumer reading the path for liveness (such
                                 * as a web-UI toggle) can never offer to
                                 * restart. The bytes counter cannot answer it
                                 * either -- "stopped rising" needs two ticks
                                 * and a memory of the last one, which is a
                                 * derivative, not a state. Rides the
                                 * rawlog_valid gate WITH rawlog_bytes rather
                                 * than with the path, so the overflow retry
                                 * that drops the unbounded path still reports
                                 * whether the sink it is counting for is open */

    /* Helper census. All three keys emit together or not at all --
     * a distinct-code count without its unrecognized/silent companions invites
     * exactly the "0 means fine" misreading above. */
    uint64_t    inventory_distinct_codes;
    uint64_t    inventory_unrecognized;
    uint64_t    inventory_silent;
    int         inventory_valid;  /* 1 = a diag_inventory line has arrived */

    /* Native-decode shadow tap. These put the tap's counters in the JSON
     * object, not only in the human-readable message-bus line, so the web UI
     * can show the measurement the nativedecode=on decision turns on.
     *
     * All or none, and `native_records` is meaningless without `records`.
     * The switch-over question is a RATIO -- what share of records a native leg
     * claimed -- so publishing the numerator without its denominator would let
     * a consumer render "native=1031" as coverage when it might be 1031 of a
     * million. The two ship together or neither does.
     *
     * `enriched` matters because enrichment changes no observation COUNT, only
     * which FIELDS an observation carries. A UI watching native_obs alone sees
     * a perfectly healthy stream while every MCC/MNC/TAC/CID quietly vanishes.
     * It is the only counter in this group that moves when identity is gained
     * or lost.
     *
     * GNSS keeps its own units: a position fix is not a cell observation, and
     * folding them would make native_obs mean nothing. */
    uint64_t    native_total_records;  /* LOG records the C extractor recovered */
    uint64_t    native_records;        /* ...claimed by ANY native leg */
    uint64_t    native_obs;            /* cell observations those legs produced */
    uint64_t    native_declined;       /* cell leg ran, legitimately found nothing */
    uint64_t    native_enriched;       /* obs that gained identity from an
                                        * EARLIER SIB1 in the stream */
    uint64_t    native_gps_fixes;      /* usable positions from the GNSS leg */
    uint64_t    native_fallback_records; /* no native leg -- the bridge is still
                                          * the ONLY decoder for these; the
                                          * number that must reach 0 before the
                                          * Python bridge can be removed */
    int         native_valid;          /* 1 = the shadow tap is armed */

    /* The stream switches an operator can flip from the panel, read
     * back. Each group ships WHOLE when valid and is omitted whole when not,
     * for the rule above: a slice count without its on/off, or "qsh armed:
     * false" from a source that never resolved qsh=, reads as a measurement
     * that was never made. */
    int         rawpackets_valid;      /* 1 = emit the rawpackets= group */
    int         rawpackets_on;         /* rawpackets= in force (default on) */
    uint64_t    rawpackets_slices;     /* DLT 147 slices delivered */
    uint64_t    rawpackets_dropped;    /* slices lost to a full ring */
    int         qsh_valid;             /* 1 = a live source resolved qsh= */
    int         qsh_requested;         /* qsh=on was asked for */
    int         qsh_armed;             /* ...and the arm was sent this session */
    /* The CRC census -- frames that reached the CRC check, and how many
     * failed it. crc_bad is the one trace bytes lost between the modem and the
     * capture leave. Whole or not at all, and omitted until something is
     * counting, so "0 bad" always means measured. crc_damage_alerts counts the
     * check windows that crossed CRC_DAMAGE_ALERT_FRAMES; the server raises the
     * source's warning when it grows. */
    int         crc_valid;
    uint64_t    crc_checked;
    uint64_t    crc_ok;
    uint64_t    crc_bad;
    uint64_t    crc_damage_alerts;
    /* The decoder input queue. Whole or not at all, and omitted
     * until a decoder has been started. bridge_dropped_* > 0 means the decoder
     * fell behind and some decoder input was dropped -- never capture. */
    int         bridge_valid;
    uint64_t    bridge_queued;         /* bytes waiting for the decoder now */
    uint64_t    bridge_peak;           /* the most ever waiting */
    uint64_t    bridge_dropped_bytes;
    uint64_t    bridge_dropped_chunks;
    int         anchor_valid;          /* 1 = the ClockAnchor stream is live */
    uint64_t    anchor_count;          /* anchors completed */
    long long   anchor_last_epoch;     /* wall time of the last one; 0 = none
                                        * yet, and then the key is OMITTED */
} diag_stats_extra_t;

/* Format a machine-parseable JSON stats object into buf: the structured
 * companion to diag_stats_format, whose keys are the registered
 * kis_datasource_cell_diag.celldiag.<key> field suffixes so the datasource can
 * SET each tracker field by key. Carries "type":"diag_stats" so the C relay /
 * datasource routes it as a control line, not a cell_observation. Emits the six
 * diag_stats-owned counters (bytes_read, obs_total, obs_per_sec, last_obs_epoch,
 * helper_alive, mask_preset) plus, when `x` is non-NULL, the config/helper-owned
 * fields it carries.
 *
 * Truncation is a FAILURE, not a shorter update. A clipped JSON object does
 * not parse, so the consumer drops it and every field in that tick goes
 * unupdated -- strictly worse than emitting less. `rawlog_path` is the only
 * unbounded input (a filesystem path against a 1 KiB status buffer), so on
 * overflow this retries once WITHOUT that key; the ten bounded fields still get
 * through and the consumer keeps the path's prior value. Returns -1 (write
 * nothing) only if even the reduced object will not fit.
 *
 * Returns the strlen written (>0), or -1 on bad args / unfixable overflow.
 * Does NOT mutate `s`. */
int diag_stats_format_json_ex(const diag_stats_t *s, const diag_stats_extra_t *x,
                              time_t now, char *buf, size_t buflen);

/* The deferred bring-up's progress and, on failure, its reason, as a
 * diag_stats object of its own.
 *
 * The live bring-up runs AFTER the open was answered. If its failure reason
 * went only to the message bus, the source's error would read "IPC connection
 * closed" once the helper exited and the panel would say "decode helper DEAD":
 * the operator who clicked Enable would see neither the reason nor which step
 * failed. This object is sent after each phase and once on failure; the
 * datasource keeps the fields, and prefers bringup_error over the generic
 * IPC-closed reason when the helper then exits.
 *
 * `phases` is the laps so far ("at_scan=1204ms port_detect=3ms"), `elapsed_ms`
 * their total. A non-NULL `error` adds bringup_error (escaped), helper_alive 0
 * (no helper ran) and mask_preset "failed" (no mask was armed); progress lines
 * carry none of those, so they never overwrite live state.
 *
 * Returns the strlen written (>0), or -1 on bad args / overflow -- never a
 * clipped object. */
int diag_stats_format_bringup_json(const char *phases, long elapsed_ms,
                                   const char *error, char *buf, size_t buflen);

/* Back-compat shim: the six diag_stats-owned counters only, no extras. */
int diag_stats_format_json(const diag_stats_t *s, time_t now,
                           char *buf, size_t buflen);

/* Note that a status was emitted at `now`: advances the cadence baseline and
 * clears obs_since_status. Call right after emitting the formatted line. */
void diag_stats_mark_status(diag_stats_t *s, time_t now);

#endif /* DIAG_STATS_H */
