// SPDX-License-Identifier: GPL-2.0-or-later
// 0xB0C0 C++ enrichment leg.
//
// Consumes the ksc-generated Kaitai C++ raw fields (generated/cpp/diag_0xb0c0.*)
// and produces the enriched fields that need no C++ ASN.1 decoder:
//   * channel_name   -- version-keyed enum lookup      (shared table data)
//   * sibs_present    -- sib_mask bit walk              (trivial derived logic)
//
// It stops at the opaque msg_data PER blob. The SIB / NAS / measConfig /
// MeasurementReport decode lives in the separate LTE RRC decoders
// (lte_rrc_sib, lte_rrc_reconfig, lte_rrc_meas_report).
//
// LIBRARY TU: the two helpers are declared in diag_0xb0c0_enrich.h and reused
// by both the standalone runner (diag_0xb0c0_enrich_verify.cpp) and the
// datasource bridge (diag_0xb0c0_gsmtap.cpp). This TU carries no main(), so it
// can be linked into another program.
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>
#include "diag_0xb0c0_enrich.h"

// The v20 / non-v20 channel-name enums are generated into diagspec::lte_rrc from shared_data/enrichment_tables.yaml (mirroring
// diaggrok's _V20_CHANNEL_NAMES / _NONV20_CHANNEL_NAMES_BY_VERSION), with drift
// caught by the shared-table drift check.
#include "../../generated/cpp/enrichment_tables.h"

namespace diag_0xb0c0_enrich {

// Version-keyed channel_type → channel_name via the generated tables. v20 uses
// its own byte[12] enum; every other version has its own oracle-pinned enum
// (there is NO single non-v20 map). An unmapped value falls back to an honest
// hex string, matching diaggrok's f"0x{ct:02X}".
std::string channel_name(uint8_t version, uint8_t ct) {
    if (version == 20) {
        const auto& v20 = diagspec::lte_rrc::v20_channel_names();
        auto it = v20.find(ct);
        if (it != v20.end()) return it->second;
    } else {
        const auto& byver = diagspec::lte_rrc::nonv20_channel_names();
        auto vit = byver.find(version);
        if (vit != byver.end()) {
            auto it = vit->second.find(ct);
            if (it != vit->second.end()) return it->second;
        }
    }
    // honest hex fallback (matches diaggrok's f"0x{ct:02X}")
    char buf[8];
    std::snprintf(buf, sizeof(buf), "0x%02X", ct);
    return std::string(buf);
}

// derived logic: sib_mask bit N (1..31) set <=> SIB N present. bit0 unused.
std::vector<int> sibs_present(uint32_t sib_mask) {
    std::vector<int> out;
    for (int n = 1; n < 32; ++n)
        if (sib_mask & (1u << n)) out.push_back(n);
    return out;
}

// ---------------------------------------------------------------------------
// The ASN.1/UPER PER decode of the msg_data blob is not done in this TU. In
// Python it is diaggrok's uper + asn1_helpers + lte_rrc_sib* + lte_rrc_reconfig
// + lte_rrc_meas_report, the part that churns with firmware. The C++ ports live
// in lte_rrc_sib.{h,cpp} (SIB1 cellAccessRelatedInfo: PLMN / TAC / CellID,
// byte-for-byte identical to diaggrok's decode_sib1_cell_access) and the
// sibling lte_rrc_* files.
//
// Do not add a stub here that returns placeholder RRC fields: an unpopulated
// result struct is a dead symbol that looks like a decoder. The GSMTAP bridge
// (diag_0xb0c0_gsmtap.cpp) does not need an RRC decode at all: it wraps the RAW
// msg_data PDU and lets Wireshark's ASN.1 dissector decode it, so
// channel_name() above is the only enrichment on that path.

} // namespace diag_0xb0c0_enrich
