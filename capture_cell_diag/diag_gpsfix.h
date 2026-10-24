/* diag_gpsfix.h - the gps_fix status line, as the bridge renders it.
 *
 * The bridge's _write_gps_fix relays every accepted fix to the message bus as
 *
 *   celldiag GPS fix {lat:.6f},{lon:.6f} alt={alt:.0f}m src=0x{code:04X}
 *
 * and under nativedecode=on this process renders the same line itself
 * (capture_cell_diag.c, native_tap_cb). Matching it byte for byte rests on one
 * fact, which this module exists to pin: Python's fixed-point float format
 * and glibc's printf both round the EXACT binary value, half to even. So a
 * half-way altitude renders the same way in both (12.5 -> "12", 13.5 -> "14"),
 * and so does negative zero ("-0.000000", "alt=-0m"). test_diag_gpsfix holds
 * goldens pasted from Python, and tests/test_native_gps_fix_format.py runs a
 * differential over random values and the bit-neighbours of every tie.
 *
 * Pure libc.
 */
#ifndef DIAG_GPSFIX_H
#define DIAG_GPSFIX_H

#include <stddef.h>
#include <stdint.h>

/* snprintf contract: returns the full length; >= out_len means truncated. */
int diag_gps_fix_status(char *out, size_t out_len, double lat_deg,
                        double lon_deg, double alt_m, uint16_t log_code);

#endif
