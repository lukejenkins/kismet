/* diag_inventory.h - the per-(log_code, version) census, natively.
 *
 * The `diag_inventory` line used to have exactly one producer: the Python
 * decode helper (kismet_diag_decode.py, wrapping diaggrok.inventory's
 * DiagInventory and rendering `summary(now, wire=True)`). A helper-less build
 * would publish no census at all. This is the same census in C, so the line
 * survives the helper.
 *
 * WHAT "THE SAME" MEANS. Two halves, each pinned separately:
 *
 *   counting  -- the (code, byte-0) key, the two-tier bound (max_codes distinct
 *                codes, max_versions_per_code versions within one), the four
 *                counters (count = decoded + dropped, emitted, enrich_failed),
 *                and the three findings: unrecognized (seen, never decoded),
 *                silent (an emit-contract code that decoded and emitted
 *                nothing), enrich_failed. diaggrok.inventory's rules, verbatim.
 *   rendering -- the bounded wire form, BYTE FOR BYTE: JSON key order, the
 *                `ts` float as Python's repr(round(now, 3)), the `status`
 *                one-liner (8 named per list, "+N more"), the 24-row
 *                flagged-first `table`, and the flag arrays cut to 32 with
 *                the true lengths in `*_total`.
 *
 * Both halves are checked against the Python class by a differential test
 * (tests/test_native_inventory_parity.py): random record streams, and the
 * per-record facts the helper itself records on every replay fixture.
 *
 * WHAT THIS DOES NOT DECIDE. Whether a record was `decoded`, and how many
 * observations it `emitted`, are the CALLER's facts -- the decode path knows,
 * the census only counts. In the Python helper that is diaggrok.parse(); in
 * the native path it is whether a native leg claimed the record. The two agree
 * on every code a native leg covers and on nothing else, so a native census of
 * a stream carrying codes no native leg handles would report them as
 * unrecognized. The native census can do better, which is the third state:
 *
 *   undecoded_native -- a code diaggrok has a parser for and no native leg
 *                       reads (diag_inventory_record_undecoded). Not a
 *                       finding about the stream: the native-coverage to-do
 *                       list, so it is listed by CODE and never escalates.
 *                       `unrecognized` stays what diaggrok's census would say:
 *                       no decoder anywhere reads the record.
 *
 * The state exists only where the census is armed with
 * diag_inventory_set_native_coverage(); off (the default), the line is the
 * Python wire form byte for byte, which is what the differential tests hold
 * it to. "Which codes diaggrok parses" is a committed, generated table
 * (diag_inventory_codes.h), not a build step: a build that ran diaggrok would
 * put Python back into the shipping binary.
 *
 * Never forwarded raw: the rendered line is consumed in-process
 * like the helper's was -- its `status` to the message bus, its counts into
 * diag_stats, its `table` onto the diag_census control line.
 *
 * Pure libc, no Kismet types.
 */
#ifndef DIAG_INVENTORY_H
#define DIAG_INVENTORY_H

#include <stddef.h>
#include <stdint.h>

/* diaggrok.inventory.DiagInventory's defaults. */
#define DIAG_INVENTORY_MAX_CODES          512
#define DIAG_INVENTORY_MAX_VERSIONS       16

/* The wire form's bounds (kismet_diag_decode.py DiagInventory). */
#define DIAG_INVENTORY_WIRE_LIST_MAX      32   /* entries per flag array */
#define DIAG_INVENTORY_TABLE_ROWS         24   /* rows in `table` */
#define DIAG_INVENTORY_STATUS_NAMED       8    /* keys named per status list */

/* Room for one rendered wire line. The line is bounded by construction: the
 * fixed keys, a status naming at most 4 x 8 keys, a 24-row table (worst-case
 * row 121 bytes) and four arrays of at most 32 entries. That is under 6 KiB
 * with every counter at its u64 maximum, so 8 KiB never truncates, and it sits
 * well under the server's 16 384-byte frame cap -- not that the line is ever
 * sent as a frame. */
#define DIAG_INVENTORY_LINE_MAX           8192

/* The emit contract (diaggrok.inventory.EMIT_CONTRACT_CODES): the codes a
 * decoded record can produce a downstream observation for. Gates `silent`
 * only; it filters nothing that is counted. */
extern const uint16_t DIAG_INVENTORY_EMIT_CONTRACT_CODES[];
extern const size_t   DIAG_INVENTORY_EMIT_CONTRACT_COUNT;

/* The subset that structurally cannot emit on any capture
 * (diaggrok.inventory.NEVER_EMITS_CODES): 0x14D8, whose lat/lon/alt the
 * reference parser hardcodes to 0.0. Excluded from silent_actionable, which is
 * what the relay escalates on, and nowhere else. */
extern const uint16_t DIAG_INVENTORY_NEVER_EMITS_CODES[];
extern const size_t   DIAG_INVENTORY_NEVER_EMITS_COUNT;

int diag_inventory_is_emit_contract(uint16_t code);
int diag_inventory_is_never_emits(uint16_t code);

typedef struct diag_inventory diag_inventory_t;

/* 0 for either cap picks the diaggrok default. max_versions_per_code is
 * clamped to 257 (a byte-0 version is 0..255, or -1 for an empty payload).
 * NULL on allocation failure. */
diag_inventory_t *diag_inventory_new(size_t max_codes, size_t max_versions_per_code);
void diag_inventory_free(diag_inventory_t *inv);

/* Tally one record -- DiagInventory.record(). `version` is payload byte 0, or
 * -1 for an empty payload (the helper's convention, so an empty frame is still
 * counted). `emitted` is how many downstream records it produced. A record
 * past either cap is counted in `overflow` and nowhere else. */
void diag_inventory_record(diag_inventory_t *inv, uint16_t code, int version,
                           int decoded, uint64_t emitted, int enrich_failed);

size_t   diag_inventory_distinct(const diag_inventory_t *inv);
uint64_t diag_inventory_overflow(const diag_inventory_t *inv);

/* Arm the undecoded_native state (see the top of this file). Off by default. */
void diag_inventory_set_native_coverage(diag_inventory_t *inv, int on);

/* Tally one record no native leg reads, of a code diaggrok has a parser for:
 * counted, neither decoded nor dropped, and its code joins undecoded_native.
 * Same admission and caps as diag_inventory_record. */
void diag_inventory_record_undecoded(diag_inventory_t *inv, uint16_t code,
                                     int version);

/* Does diaggrok register a parser for `code`? From the generated table. */
int    diag_inventory_diaggrok_knows(uint16_t code);
size_t diag_inventory_diaggrok_count(void);

/* Render summary(now, wire=True) without its trailing newline. snprintf
 * contract: returns the full length, and a value >= out_len means `out` holds
 * a truncated line the caller must not use. -1 on a NULL argument or an
 * allocation failure. */
int diag_inventory_wire(const diag_inventory_t *inv, double now,
                        char *out, size_t out_len);

/* The census cadence, as the helper's emitter runs it: armed at `now` with
 * `interval` seconds (<= 0 = no periodic line, the final one only), and
 * asked after every record. Returns 1 -- and restarts the interval at `now` --
 * when a periodic line is due. */
typedef struct {
    double interval;
    double last;
} diag_inventory_cadence_t;

void diag_inventory_cadence_arm(diag_inventory_cadence_t *c, double interval,
                                double now);
int  diag_inventory_cadence_due(diag_inventory_cadence_t *c, double now);

/* Python repr() of a double, as json.dumps writes a float: the shortest
 * string that round-trips, fixed notation with a ".0" floor inside
 * [1e-4, 1e16), repr's exponent form outside it. Exposed for the selftest. */
void diag_inventory_py_float(char *buf, size_t n, double v);

/* Python round(v, 3): the correctly rounded 3-place decimal, read back. */
double diag_inventory_py_round3(double v);

#endif
