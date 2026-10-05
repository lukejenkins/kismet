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

    The Python-decode baseline's stand-in for diag_native_decode.cpp.

    WHAT THIS IS FOR. The reference configuration of this helper is
    `kismet_cap_cell_diag` built with ZERO native decode objects, decoding every
    DIAG payload through the Python bridge (kismet_diag_decode.py). `make`
    builds that configuration by default; `make NATIVE=1` opts into the
    diagspec C++ legs for shadow measurement.

    WHY IT IS NEEDED AT ALL, i.e. why the build switch is not one Makefile line.
    capture_cell_diag.c calls diag_native_decode(), diag_native_decode_gps_fix(),
    diag_native_sib1_learn() and friends at LINK scope -- the `nativedecode=off`
    default is a runtime branch, not a compile-time one, so the references exist
    in the object whether or not the mode is ever armed. Dropping NATIVE_OBJS
    from the link line therefore does not produce a smaller binary; it produces
    undefined references. Something must define the extern "C" surface.

    WHY A STUB RATHER THAN #ifdef AROUND THE CALL SITES. Conditionals in
    capture_cell_diag.c would make the two configurations differ in the shape of
    the capture path itself: eight or so #ifdef arms, each one code that only one
    build compiles, and a difference an operator could only discover by running
    nm. With a stub, capture_cell_diag.c is byte-identical in both builds and the
    entire difference is which object satisfies one header. That is also what
    lets diagspec/check_binary_symbols.sh assert something real about the
    baseline binary (role `baseline`): the extern "C" surface PRESENT, every C++
    leg ABSENT -- the same 21 symbols the `helper` role derives from the same two
    files, at the opposite polarity.

    THIS IS NOT A FALLBACK DECODER AND MUST NEVER BECOME ONE. Every entry
    point below returns the contract's "no native leg" answer (-1, false, 0,
    NULL). The Python bridge is the decoder in this configuration and there is
    nothing here for it to race. If a future change gives one of these a real
    implementation, the file has stopped being a stub and the `baseline` role's
    absence assertion has stopped meaning what it says.

    THE ONE BEHAVIOUR DIFFERENCE, and it is deliberate: diag_native_available()
    returns false, and capture_cell_diag.c uses that to REJECT
    `nativedecode=shadow` at source-open time. Silently accepting it would arm a
    tap whose every counter reports bridge-only -- a reading indistinguishable
    from a live stream that genuinely carries nothing the legs handle, which is
    precisely the measurement the shadow tap exists to produce. A fabricated
    zero from the wrong build is worse than a refusal.
*/

#include "diag_native_decode.h"

bool diag_native_available(void) {
    return false;
}

/* ---- cell observation surface --------------------------------------------
 *
 * has_leg false for every code, so a caller routing on it sends everything to
 * the bridge -- which in this configuration is the whole design, not a
 * degradation. decode() still returns -1 ("no leg") rather than 0 ("leg ran,
 * found nothing"): the header makes that distinction load-bearing, and
 * collapsing it here would teach a caller that the codes were tried. */

bool diag_native_has_leg(uint16_t log_code) {
    (void)log_code;
    return false;
}

int diag_native_decode(uint16_t log_code, const uint8_t *payload, size_t payload_len,
        diag_native_obs_t *out_obs, size_t max_obs) {
    (void)log_code; (void)payload; (void)payload_len; (void)out_obs; (void)max_obs;
    return -1;
}

size_t diag_native_codes(uint16_t *out_codes, size_t max_codes) {
    (void)out_codes; (void)max_codes;
    return 0;
}

/* ---- GNSS position surface ----------------------------------------------- */

bool diag_native_has_gps_leg(uint16_t log_code) {
    (void)log_code;
    return false;
}

int diag_native_decode_gps_fix(uint16_t log_code, const uint8_t *payload,
        size_t payload_len, diag_native_gps_fix_t *out_fix) {
    (void)log_code; (void)payload; (void)payload_len;
    /* The header promises out_fix is fully written when the return is >= 0.
     * This returns -1, so it owes nothing -- but a caller that reads it anyway
     * gets an explicit "no fix" rather than a stack coordinate. `valid` false is
     * the one field that matters: a geo-tag taken from an uninitialised lat/lon
     * anchors every cell in the run at a fabricated position. */
    if (out_fix != NULL) {
        out_fix->valid = false;
        out_fix->lat_deg = 0.0;
        out_fix->lon_deg = 0.0;
        out_fix->alt_m = 0.0;
        out_fix->decline_reason = DIAG_NATIVE_GPS_DECLINE_NONE;
    }
    return -1;
}

size_t diag_native_gps_codes(uint16_t *out_codes, size_t max_codes) {
    (void)out_codes; (void)max_codes;
    return 0;
}

/* ---- cross-record SIB1 enrichment ----------------------------------------
 *
 * map_new() returns NULL, which the header already documents as a legal state
 * ("enrichment is unavailable"): capture_cell_diag.c treats a NULL map as
 * non-fatal and keeps capturing. Nothing here can be reached in the baseline
 * anyway -- the tap that would call it cannot be armed -- but returning NULL
 * rather than a heap sentinel means the stub allocates nothing at all. */

diag_native_sib1_map_t *diag_native_sib1_map_new(void) {
    return NULL;
}

void diag_native_sib1_map_free(diag_native_sib1_map_t *map) {
    (void)map;
}

size_t diag_native_sib1_map_size(const diag_native_sib1_map_t *map) {
    (void)map;
    return 0;
}

bool diag_native_is_identity_source(uint16_t log_code) {
    (void)log_code;
    return false;
}

int diag_native_sib1_learn(diag_native_sib1_map_t *map, uint16_t log_code,
        const uint8_t *payload, size_t payload_len) {
    (void)map; (void)log_code; (void)payload; (void)payload_len;
    return -1;
}

bool diag_native_sib1_enrich(const diag_native_sib1_map_t *map,
        diag_native_obs_t *obs) {
    (void)map; (void)obs;
    return false;
}

/* 0xB197 cell config: -1 / false, like every other baseline entry. */
int diag_native_cell_config_learn(diag_native_sib1_map_t *map, uint16_t log_code,
        const uint8_t *payload, size_t payload_len) {
    (void)map; (void)log_code; (void)payload; (void)payload_len;
    return -1;
}

bool diag_native_lte_cell_config(diag_native_sib1_map_t *map, diag_native_obs_t *obs) {
    (void)map; (void)obs;
    return false;
}

/* -1, "not a code this entry point handles", for every code: the baseline has
 * no join leg, and the bridge renders every neighbour row it emits. */
int diag_native_lte_meas_neighbours(const diag_native_sib1_map_t *map,
        uint16_t log_code, const uint8_t *payload, size_t payload_len,
        diag_native_obs_t *out_obs, size_t max_obs) {
    (void)map; (void)log_code; (void)payload; (void)payload_len;
    (void)out_obs; (void)max_obs;
    return -1;
}

/* ---- observation -> bridge JSON ------------------------------------------
 *
 * -1 is the header's "no cell-observation serialization for this log code".
 * In the baseline the bridge renders every line it emits, so this producer has
 * no caller; returning -1 keeps a future one from emitting a truncated line. */

int diag_native_obs_to_json(const diag_native_obs_t *obs, const char *imei,
        double captured_at, uint64_t log_tick, char *out, size_t out_len) {
    (void)obs; (void)imei; (void)captured_at; (void)log_tick;
    if (out != NULL && out_len > 0)
        out[0] = '\0';
    return -1;
}
