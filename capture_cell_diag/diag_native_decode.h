/*
    This file is part of Kismet

    Kismet is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.

    Kismet is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with Kismet; if not, write to the Free Software
    Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

    C-callable surface over the diagspec cell_observation decode legs -- the
    piece that lets THIS process decode DIAG natively.

    WHY THIS EXISTS. The pipe to the Python decode helper (raw HDLC bytes out,
    JSON lines in) is the boundary a native decoder replaces, one log code at a
    time. The diagspec C++ legs that do that decoding
    (diagspec/enrichment/cpp/diag_0xbNNN_observation.*, byte-parity-verified
    against the Python bridge) are also linked into the SERVER
    (datasource_cell_diag.cc) -- but the Python bridge does not run in the
    server. It is fork/exec'd by THIS helper, and the helper sends its decoded
    results upstream via cf_send_json(); no decodable DIAG record reaches the
    server. (Raw stream slices do, as DLT 147 packets, but only for the
    kismetdb/pcapng loggers -- a slice is a cut of the byte stream, not a
    record.) So a decode leg living in the server can never displace the
    bridge, however correct it is: it is on the wrong side of the fork.

    This shim puts the same legs on the right side. It is `extern "C"` over the
    C++ legs, returning flat C structs, so capture_cell_diag.c (C) can call them
    with no C++ in its translation unit. The helper is linked with $(CCLD) = gcc,
    so the link line gains $(CXXLIBS) = -lstdc++.

    NOT a reimplementation: every function here forwards to the identical diagspec
    leg the server links and the helper-side A/B harness pins byte-for-byte against
    diaggrok. There is exactly one decode implementation per log code.

    Ownership/lifetime: each decode_* fills a caller-provided fixed array and
    returns the count written, so there is no allocation to free and no
    C++-allocated memory crossing the boundary. A record with more cells than the
    caller's capacity is TRUNCATED and the return value tells the caller how many
    were written -- see diag_native_*_max().
*/

#ifndef __DIAG_NATIVE_DECODE_H__
#define __DIAG_NATIVE_DECODE_H__

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- Is native decode actually IN this binary? ----------------------------
 *
 * True in diag_native_decode.cpp (the real shim, which forwards to the diagspec
 * C++ legs); false in diag_native_decode_stub.c, the pure-C surface the
 * DEFAULT `NATIVE=` off build links instead.
 *
 * WHY A RUNTIME QUERY RATHER THAN AN #ifdef. The Python-decode baseline
 * requires that this helper build with ZERO native decode objects, and
 * capture_cell_diag.c calls the entry points below at link scope regardless of
 * runtime mode -- so omitting the legs is a link failure unless SOMETHING
 * defines the surface. The stub is that something. Given a stub, the remaining
 * question is how the caller learns it got one, and a preprocessor answer would
 * mean the two builds differ in the shape of capture_cell_diag.c itself:
 * unreachable code in each configuration, and a difference readable only with
 * nm. A function makes it a property both builds compile, both builds can be
 * asked, and an operator sees in a message rather than in a symbol table.
 *
 * The only caller obligation: `nativedecode=shadow` MUST be rejected when this
 * is false. A stub answers "no leg" to every code, so an armed tap would report
 * 100% bridge-only -- a number indistinguishable from a real stream carrying
 * nothing the legs handle, which is the measurement the tap exists to produce.
 * Failing the source open is the difference between "not built for this" and a
 * confident, fabricated zero. */
bool diag_native_available(void);

/* One decoded cell observation, flattened for C. Mirrors the union of the
 * per-code CellObservation structs; a field a given log code does not carry is
 * reported absent via its has_* flag rather than as a sentinel value, so a
 * consumer can never mistake "no measurement" for a real reading. */
typedef struct {
    /* "LTE" or "NR" -- the rat the emitted observation carries. */
    const char *rat;
    /* Origin log code, e.g. 0xB97F -- becomes the prov block's "origin". */
    uint16_t log_code;

    int32_t pci;
    int32_t earfcn;        /* NR-ARFCN when rat == "NR" */
    bool is_serving;

    bool has_rsrp;
    double rsrp;           /* dBm; only meaningful when has_rsrp */
    bool has_rsrq;
    double rsrq;           /* dB;  only meaningful when has_rsrq */
    /* Per-cell RSSI -- 0xB17F only, the DIAG leg's one rssi source. */
    bool has_rssi;
    double rssi;           /* dBm; only meaningful when has_rssi */

    /* Full-identity fields -- 0xB0C0 SIB1 only; absent on the measurement codes.
     * MCC/MNC are STRINGS, not integers: a 2-digit MNC can lead with a zero
     * ("310"/"01" is not "310"/1), so narrowing them to an int loses the
     * distinction the PLMN actually encodes. NUL-terminated. */
    bool has_identity;
    char mcc[8];
    char mnc[8];
    uint32_t tac;
    /* 64-bit, not 32. LTE's CellIdentity is 28 bits, but NR's NCI is
     * 36 -- so a uint32_t here would silently truncate the top 4 bits of every
     * 5G cell identity that arrives via cross-record enrichment, producing a
     * plausible-looking wrong cell rather than a visible failure. */
    uint64_t cell_id;

    /* 0xB0C0 MeasurementReport neighbour row, not the record's own
     * SIB1 identity cell. Selects that row's key order in
     * diag_native_obs_to_json. Set only by diag_native_lte_meas_neighbours. */
    bool meas_neighbour;
    /* True when the row has NO earfcn key at all: a CGI neighbour whose measId
     * did not map to a carrier. Inverted (absent, not has_) so that init_obs's
     * memset leaves every other code's earfcn present, as it always was. */
    bool earfcn_absent;

    /* LTE serving-cell config from a cached 0xB197, attached by
     * diag_native_lte_cell_config() -- the bridge's add_lte_cell_config. Rendered
     * after band/duplex as bandwidth_rb, bandwidth (MHz, none for 6 RB) and
     * tx_antennas, each only when set. */
    bool has_bandwidth_rb;
    int32_t bandwidth_rb;
    bool has_tx_antennas;
    int32_t tx_antennas;

    /* 0xB97F only: the first SSB beam of the cell (the bridge's one-entry
     * `beams` list), and the serving row's SSB index and burst period. */
    bool has_beam;
    int64_t beam_ssb_index;
    bool has_beam_rsrp_a;
    double beam_rsrp_a;
    bool has_beam_rsrp_b;
    double beam_rsrp_b;
    bool has_beam_rsrq_a;
    double beam_rsrq_a;
    bool has_beam_rsrq_b;
    double beam_rsrq_b;
    bool has_serving_ssb_index;
    int32_t serving_ssb_index;
    bool has_ssb_periodicity_ms;
    int32_t ssb_periodicity_ms;
} diag_native_obs_t;

/* Decode one log-code-stripped DIAG LOG_F payload natively.
 *
 * Returns the number of observations written into out_obs (0 is a NORMAL
 * outcome, not an error: a record with no measurement subpacket, a 0xB0C0 that
 * carries no SIB1, or a payload the reference parser rejects all yield 0 --
 * mirroring the Python bridge, which emits nothing for those).
 *
 * Returns -1 if log_code has no native leg, which is how a caller decides to
 * fall back to the Python bridge for that record. That distinction matters:
 * "no leg" and "leg ran, found nothing" must not collapse, or the migration
 * would silently drop records for un-migrated codes.
 */
int diag_native_decode(uint16_t log_code, const uint8_t *payload, size_t payload_len,
        diag_native_obs_t *out_obs, size_t max_obs);

/* True iff log_code has a native leg (i.e. diag_native_decode would not return
 * -1). Lets the caller route without a throwaway decode. */
bool diag_native_has_leg(uint16_t log_code);

/* ---- GNSS position ---------------------------------------------------------
 *
 * A SECOND entry point, not a fifth branch of diag_native_decode(). 0x1476 does
 * not produce a cell_observation: the helper already routes a decoded fix down a
 * different path (drain_helper_lines intercepts "type":"gps_fix" to CACHE the
 * position rather than relaying it upstream), and diag_native_obs_t has no shape
 * for lat/lon/alt. Folding it into the cell surface would mean either a struct
 * whose meaning depends on log_code, or a caller that has to know which fields
 * are live per code -- both of which are how "no measurement" starts reading as
 * a real value.
 *
 * WHY THIS MATTERS AT ALL. With every cell code native, the helper would STILL
 * fork the Python bridge for every GNSS record, and Kismet geo-tags every DIAG
 * cell observation from those fixes (local->last_gps, cf_send_json's `gps`
 * param). Position is the bridge's last load-bearing job.
 *
 * THERE IS NO 0x14D8 LEG, DELIBERATELY, although 0x14D8 is often described as
 * the backup GNSS position code. diaggrok's parse_0x14d8 hardcodes lat/lon/alt
 * to 0.0 -- bytes [+20..+43] are unbound carryover and no firmware populates
 * them -- so 0.0 fails the bridge's own validity gate and the 0x14D8 branch can
 * never emit a fix. On the RM520N-GL reference capture that is 53 of its 69
 * GNSS records producing nothing. A 0x14D8 leg would be a decode path that is
 * provably always empty. The helper-side slice test asserts this, so if a
 * future parser revision DOES bind those bytes, it fails and this comment stops
 * being true out loud rather than silently.
 */

/* Why a GNSS record produced no usable fix. Mirrors diaggrok.gnss_gate's
 * GPS_DECLINE_* taxonomy (the same constants kismet_diag_decode.py imports), so
 * a caller counting declines can distinguish the four findings instead of
 * folding them -- two of which are OPPOSITE: NO_POSITION (0,0) is a benign
 * engine-with-no-sky-view, while NO_TIME_SOLUTION is a chipset seed position
 * measured 215 km from truth on SDX62. DECLINE_NONE == a fix was
 * produced. There is deliberately no "not a GNSS code" member: this type is only
 * ever written for a code diag_native_has_gps_leg() already accepted. */
typedef enum {
    DIAG_NATIVE_GPS_DECLINE_NONE = 0,    /* a usable fix was produced */
    DIAG_NATIVE_GPS_DECLINE_UNPARSEABLE, /* payload too short/malformed */
    DIAG_NATIVE_GPS_DECLINE_NO_POSITION, /* coord out of band, incl. (0,0) */
    DIAG_NATIVE_GPS_DECLINE_PLACEHOLDER, /* vendor "no fix" sentinel */
    DIAG_NATIVE_GPS_DECLINE_NO_TIME,     /* gps_week == 0xFFFF, seed position */
} diag_native_gps_decline_t;

/* One decoded GNSS position fix. `valid` false means the record carried no
 * usable fix -- a short/unparseable payload, an out-of-band coordinate, or a
 * known vendor "no fix" placeholder (Qualcomm's Nevada default, Telit's
 * no-antenna sentinel, (0,0)). A caller must NOT geo-tag from an invalid fix, or
 * every cell in the run anchors at a fabricated coordinate. `decline_reason`
 * names WHICH gate refused (DIAG_NATIVE_GPS_DECLINE_NONE when valid). */
typedef struct {
    bool valid;
    double lat_deg;
    double lon_deg;
    double alt_m;
    diag_native_gps_decline_t decline_reason;
} diag_native_gps_fix_t;

/* Decode one log-code-stripped DIAG LOG_F payload into a position fix.
 *
 * Returns 1 when a fix was produced, 0 when the leg ran and declined (no usable
 * position -- a NORMAL outcome), and -1 when log_code has no GNSS leg, which is
 * how a caller decides to fall back to the Python bridge. Same three-way
 * contract as diag_native_decode(): collapsing "no leg" into "found nothing"
 * would silently drop every record of an un-migrated code.
 *
 * out_fix is always fully written when the return value is >= 0. */
int diag_native_decode_gps_fix(uint16_t log_code, const uint8_t *payload,
        size_t payload_len, diag_native_gps_fix_t *out_fix);

/* True iff log_code has a native GNSS leg. */
bool diag_native_has_gps_leg(uint16_t log_code);

/* The log codes with native CELL legs, for logging/inventory. Returns the count
 * and fills out_codes up to max_codes. */
size_t diag_native_codes(uint16_t *out_codes, size_t max_codes);

/* The log codes with native GNSS legs. Separate from diag_native_codes() rather
 * than merged into it, for the same reason the decode entry points are separate:
 * these route to a different surface with a different output type, and an
 * inventory that flattened them would say "5 native legs" while
 * diag_native_has_leg() answers false for one of them. */
size_t diag_native_gps_codes(uint16_t *out_codes, size_t max_codes);

/* ---- Cross-record SIB1 enrichment -----------------------------------------
 *
 * A THIRD surface. Everything above is a pure function of ONE record. The Python bridge
 * is not: `_LogEmitter.feed` calls `update_sib1_map` before
 * `result_to_observations`, so a bare measurement observation inherits MCC /
 * MNC / TAC / CellID from a SIB1 seen EARLIER in the stream, keyed by
 * (pci, earfcn). No per-record leg can do that, however byte-correct it is.
 *
 * On a real LTE wardrive, about half the observations (10,259 / 20,984) carry
 * identity that came only from this mechanism. Native decode without it loses
 * those fields with the observation COUNT UNCHANGED -- a silent field loss
 * that every counter-based check reports as green.
 *
 * Two identity sources, mirroring diaggrok's update_sib1_map:
 *   0xB0C0  LTE RRC OTA  -- key (pci, earfcn), via the 0xB0C0 observation leg
 *   0xB821  NR5G RRC OTA -- key (pci, arfcn),  via the 0xB821 identity leg
 *
 * ONE MAP, SHARED BY BOTH RATs, KEYED WITHOUT THE RAT. That is the bridge's
 * behaviour and this reproduces it deliberately: an LTE EARFCN and an NR-ARFCN
 * that happen to be the same integer, on the same PCI, collide -- and
 * first-write-wins means the earlier one persists. Adding `rat` to the key here
 * would be a genuine improvement AND an equivalence break, silently diverging
 * from the oracle the native legs are tested against. If it is worth fixing, fix it in
 * kismet_diag_decode.py first and port the change; do not fix it here alone.
 */

/* Opaque cross-record identity cache. Heap-allocated so an unarmed source pays
 * nothing, and so the C helper never needs a C++ type in its translation unit. */
typedef struct diag_native_sib1_map diag_native_sib1_map_t;

diag_native_sib1_map_t *diag_native_sib1_map_new(void);
void diag_native_sib1_map_free(diag_native_sib1_map_t *map);

/* Distinct keys learned so far -- the operator-facing "how much identity does
 * this stream actually know" number. */
size_t diag_native_sib1_map_size(const diag_native_sib1_map_t *map);

/* True iff log_code can contribute a map entry (0xB0C0 / 0xB821).
 *
 * This set is NOT a subset of diag_native_has_leg(): 0xB0C0 is both (it emits
 * an identity observation AND seeds the map) but 0xB821 is ONLY an identity
 * source -- it has no cell-observation leg, because the bridge's
 * result_to_observations has no Diag0xB821 branch. A caller that routed on
 * has_leg() alone would never learn a single NR identity. */
bool diag_native_is_identity_source(uint16_t log_code);

/* 0xB197 serving-cell config. The bridge caches it in the sib1_map under
 * ("lte_cfg", pci, earfcn & 0xFFFF) and attaches it to every LTE observation of
 * that cell. It is a separate entry point, not an identity source: it seeds no
 * (pci, earfcn) identity, so it must not move the identity_* counters.
 *
 * diag_native_cell_config_learn returns 1 when the record was cached, 0 when a
 * 0xB197 carried nothing cacheable (or map is NULL), and -1 for any other code.
 * Call it on every record before decoding, as the bridge runs update_sib1_map
 * before result_to_observations.
 *
 * diag_native_lte_cell_config fills obs's has_bandwidth_rb/has_tx_antennas
 * fields and returns true when it attached anything. It MUTATES the map (the
 * alias guard records every full EARFCN seen under a truncated key), so it must
 * run on EVERY LTE observation, in emit order, after diag_native_sib1_enrich --
 * whether or not the observation is rendered. NR rows and rows with no earfcn
 * are left alone, as the bridge's NR branch never calls it. */
int diag_native_cell_config_learn(diag_native_sib1_map_t *map, uint16_t log_code,
        const uint8_t *payload, size_t payload_len);
bool diag_native_lte_cell_config(diag_native_sib1_map_t *map, diag_native_obs_t *obs);

/* Learn identity from one SIB1-bearing record.
 *
 * Returns 1 when a NEW key was inserted, 0 when the leg ran and inserted
 * nothing (no SIB1 in this record, or the key was already known -- first-write
 * -wins, mirroring update_sib1_map), and -1 when log_code is not an identity
 * source. Call this BEFORE decoding the same record's observations, matching
 * `feed`'s ordering: a serving cell seen after its own SIB1 in the same record
 * stream must be emitted WITH identity. */
int diag_native_sib1_learn(diag_native_sib1_map_t *map, uint16_t log_code,
        const uint8_t *payload, size_t payload_len);

/* Merge cached identity onto one observation, keyed (obs->pci, obs->earfcn).
 * Returns true iff obs was enriched (i.e. the key was known and obs did not
 * already carry identity).
 *
 * An observation that ALREADY carries identity is left alone. That is not a
 * safety belt, it is the bridge's rule: result_to_observations calls _enrich on
 * the four MEASUREMENT branches and deliberately not on the 0xB0C0 branch,
 * whose identity is the record's own freshly-decoded SIB1. Since 0xB0C0 is the
 * only leg that sets has_identity, `!has_identity` is exactly that condition. */
bool diag_native_sib1_enrich(const diag_native_sib1_map_t *map,
        diag_native_obs_t *obs);

/* ---- LTE MeasurementReport neighbours -----------------------------------
 *
 * A second piece of cross-record state, and the reason it lives in this map.
 * A MeasurementReport names its neighbours by PCI only; the carrier comes from
 * measId -> measObjectId -> EARFCN, configured by an EARLIER
 * RRCConnectionReconfiguration and invalidated by release / setup / handover /
 * re-establishment. diaggrok keeps that state in the same sib1_map the SIB1
 * identities live in (key ("lte_meas",)) and updates it from EVERY 0xB0C0
 * record inside update_sib1_map, before the SIB1 gate. So
 * diag_native_sib1_learn(0xB0C0) updates it too, and this reads it: the
 * lifetime and the learn-before-decode ordering are exactly the bridge's.
 *
 * The join itself (the state machine and the emit rules) is the diagspec leg
 * diagspec/enrichment/cpp/lte_meas_join.*, pinned field-for-field against
 * diaggrok. Nothing here decides.
 *
 * Writes the neighbour rows of a 0xB0C0 UL-DCCH MeasurementReport into
 * out_obs, as meas_neighbour observations: LTE, not serving, rsrp/rsrq as
 * reported, identity set only for a cgi-Info neighbour (so SIB1 enrichment
 * reaches exactly the PCI-only rows, as _enrich does in the bridge), and
 * earfcn_absent for a CGI row whose measId did not map. Returns the count
 * written (0 is normal: most 0xB0C0 records are not reports, and an unmapped
 * PCI-only neighbour is omitted), or -1 when log_code is not 0xB0C0. A NULL
 * map knows no carriers, so only CGI neighbours can be returned. */
int diag_native_lte_meas_neighbours(const diag_native_sib1_map_t *map,
        uint16_t log_code, const uint8_t *payload, size_t payload_len,
        diag_native_obs_t *out_obs, size_t max_obs);

/* ---- Serialize an observation to the bridge's exact JSON line --------------
 *
 * The render step nativedecode=on needs: everything above DECODES a record into
 * a diag_native_obs_t, and this turns that struct back into the
 * cell_observation line Kismet consumes.
 *
 * BYTE-PARITY IS A PER-CODE OBLIGATION, NOT A STRUCT-WIDE ONE. The bridge
 * (kismet_diag_decode.py result_to_observations) builds an
 * insertion-ordered dict DIFFERENTLY per log code -- 0xB193 places prov before
 * rsrp/rsrq, 0xB192/0xB195/0xB97F place it after is_serving, 0xB0C0 carries its
 * identity inline (not via _enrich) and puts prov last -- then json.dumps with
 * separators=(",",":"). Matching it means reproducing key order per code, which
 * is why this switches on obs->log_code rather than emitting a uniform field
 * list. The offline test pins every branch against strings captured from the
 * bridge itself.
 *
 * `imei`, `captured_at` (Unix-epoch seconds, as _prov stamps) and `log_tick`
 * (the raw DIAG ts64 -- the bridge renders it as float(ts64), i.e. "<n>.0") are
 * taken as PARAMETERS, not read from a clock, so the rendered line is a pure
 * function of its inputs and can be pinned byte-for-byte offline. In the live
 * on-path the caller passes gettimeofday() and rec->ts64; log_tick byte-matches
 * the bridge (same underlying integer), captured_at need only be valid JSON (a
 * different process stamps a different wall-clock -- it never byte-matched).
 *
 * Returns the number of bytes the full line occupies excluding the NUL,
 * snprintf-style: a value >= out_len means the line was truncated and the caller
 * MUST NOT emit it. Never writes the trailing newline -- the caller frames it.
 * Returns -1 if log_code has no cell-observation serialization (e.g. a GNSS code,
 * or 0xB821, which seeds identity but emits no observation of its own). */
int diag_native_obs_to_json(const diag_native_obs_t *obs, const char *imei,
        double captured_at, uint64_t log_tick, char *out, size_t out_len);

#ifdef __cplusplus
}
#endif

#endif /* __DIAG_NATIVE_DECODE_H__ */
