// SPDX-License-Identifier: GPL-2.0-or-later
// LTE EARFCN -> band and band -> duplex, for the native cell_observation path.
//
// The C++ mirror of diaggrok `parsers/lte_tables.py`'s earfcn_to_band() and
// band_to_duplex(), which the Python bridge applies to EVERY LTE observation via
// observation.py's centralised add_band_duplex(). Without it every
// nativedecode=on LTE row would lack two keys the bridge's row carries.
//
// One table, pinned. This is not a second opinion on TS 36.101 -- it is a port,
// held to the Python table by harness/lte_band_verify.py, which sweeps EVERY
// EARFCN 0..kSweepMax on both sides and compares (band, duplex) pairwise. A
// correction belongs in lte_tables.py first; the sweep then fails here until this
// file follows. Two writers disagreeing on a pure function of the EARFCN is the
// failure add_band_duplex was centralised to prevent.
//
// Both functions answer the Python "unknown" the same way: band 0 for an EARFCN in
// no range, and nullptr (Python None) for a band with no TS 36.101 5.5-1 class --
// so a caller omits the key rather than emitting a plausible-but-wrong value.
#ifndef DIAGSPEC_LTE_BAND_H
#define DIAGSPEC_LTE_BAND_H

#include <cstdint>

namespace lte_band {

// EARFCN -> E-UTRA band number; 0 when the EARFCN is in no table range.
int earfcn_to_band(uint32_t earfcn);

// Band -> "TDD" / "FDD" / "SDL"; nullptr for band 0 or any unclassified band.
// The returned string is a static literal and is never freed.
const char* band_to_duplex(int band);

}  // namespace lte_band

#endif  // DIAGSPEC_LTE_BAND_H
