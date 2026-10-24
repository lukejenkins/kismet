// SPDX-License-Identifier: GPL-2.0-or-later
// 0xB0C0 cell_observation identity enrichment leg — see the header.
//
// Turns a SIB1-bearing 0xB0C0 LTE RRC OTA record into the FULL-IDENTITY
// (pci, earfcn, mcc, mnc, tac, cell_id) cell the celldiag datasource emits, pure
// C++ instead of the Python bridge (kismet_diag_decode.py). It crosses the ASN.1
// boundary by composing the already-verified generated Kaitai parse +
// channel_name enrich + SIB1 decode.
//
// Byte-for-byte contract vs diaggrok parse_0xb0c0's SIB1 auto-decode (grounded by
// the A/B pytest over the committed lte_rrc_sib1_ident_b0c0_*.bin fixtures):
//   * gate    == the bridge's `if result.sib1_tac is not None`, which parse_0xb0c0
//                sets iff channel_name == "BCCH-DL-SCH" AND decode_sib1_cell_access
//                returned a SIB1 with a non-empty plmn_list (_build_result). This
//                leg reproduces that exact two-part gate.
//   * pci     == rec.pci()      (the generated parser's version-keyed pci)
//   * earfcn  == rec.earfcn()   (the generated parser's version-keyed earfcn width)
//   * mcc/mnc == sib1.plmn_list[0].{mcc,mnc}   (bridge: sib1_mcc / sib1_mnc)
//   * tac     == sib1.tracking_area_code       (bridge: sib1_tac)
//   * cell_id == sib1.cell_identity            (bridge: sib1_cell_id)
//
// No rsrp: SIB1 is OTA identity, not a signal measurement; the bridge's 0xB0C0
// branch attaches no rsrp.

#include "diag_0xb0c0_observation.h"

#include <exception>
#include <string>
#include <vector>

#include "kaitai/kaitaistream.h"
#include "diag_0xb0c0.h"
#include "diag_0xb0c0_enrich.h"
#include "lte_rrc_sib.h"

namespace diag_0xb0c0_observation {

// SIB1 auto-decode fires only on the broadcast BCCH-DL-SCH channel; every other
// channel (PCCH / DL-DCCH / UL-DCCH / …) never populates sib1_tac in the bridge.
static const char* kSib1Channel = "BCCH-DL-SCH";

bool is_refusal(Decline d) {
    switch (d) {
        case Decline::kThrew:
        case Decline::kTruncated:
        case Decline::kVersion:
            return true;
        default:
            return false;
    }
}

const char* decline_name(Decline d) {
    switch (d) {
        case Decline::kNone:           return "none";
        case Decline::kNotSib1Channel: return "not_sib1_channel";
        case Decline::kEmptyPdu:       return "empty_pdu";
        case Decline::kNotSib1:        return "not_sib1";
        case Decline::kEmptyPlmn:      return "empty_plmn";
        case Decline::kThrew:          return "threw";
        case Decline::kTruncated:      return "truncated";
        case Decline::kVersion:        return "version";
    }
    return "unknown";
}

// Record the path taken, when the caller asked for it. A no-op when `why` is
// null, which is every shipping call site — the parameter changes no behaviour,
// it only makes an existing distinction visible.
static inline void note(Decline* why, Decline d) {
    if (why != nullptr)
        *why = d;
}

std::vector<CellObservation> observations(const std::string& payload,
                                          Decline* why) {
    std::vector<CellObservation> out;
    note(why, Decline::kNone);

    // Kaitai (and the downstream UPER reader) throw std::exception subclasses on a
    // truncated / malformed buffer; a capture source must decline the record, not
    // abort the stream — so a parse failure yields zero observations.
    // parse_0xb0c0's first gate: an empty payload or a version byte outside
    // _VERSIONS_B0C0_4918 is None, before any header is read.
    static const uint8_t kVersions[] = {0x02, 0x07, 0x09, 0x0C, 0x0D, 0x0E,
                                        0x0F, 0x14, 0x18, 0x1A, 0x1B, 0x1E};
    bool known = false;
    if (!payload.empty())
        for (uint8_t v : kVersions)
            known = known || static_cast<uint8_t>(payload[0]) == v;
    if (!known) {
        note(why, Decline::kVersion);
        return out;
    }

    try {
        kaitai::kstream ks(payload);
        diag_0xb0c0_t rec(&ks);

        // A declared PDU that overruns the record is a truncated payload, which
        // parse_0xb0c0 declines (None). The .ksy clamps msg_data to empty there,
        // so without this test the record would read as a parsed non-SIB1.
        if (static_cast<size_t>(rec.header_size()) + rec.msg_length() > payload.size()) {
            note(why, Decline::kTruncated);
            return out;
        }

        // Gate part 1: channel NAME must be BCCH-DL-SCH (resolved off the
        // per-version channel_type enum, not the raw integer, since v20 and the
        // non-v20 path encode BCCH-DL-SCH under different values).
        const std::string chan =
            diag_0xb0c0_enrich::channel_name(rec.version(), rec.channel_type());
        if (chan != kSib1Channel) {
            note(why, Decline::kNotSib1Channel);
            return out;
        }

        // msg_data() is the raw RRC PDU bytes (KS_STR_ENCODING_NONE); an empty PDU
        // means parse_0xb0c0 never ran the SIB1 decode (its `and msg_data` guard).
        const std::string& pdu = rec.msg_data();
        if (pdu.empty()) {
            note(why, Decline::kEmptyPdu);
            return out;
        }

        // Gate part 2: the PDU must decode as a SIB1 with a non-empty PLMN list —
        // exactly when parse_0xb0c0 sets sib1_tac (bridge emit condition). A
        // BCCH-DL-SCH carrying a SystemInformation (SIB2+) container returns false
        // here (not SIB1), so no identity cell is emitted — matching the bridge.
        std::vector<uint8_t> msg(pdu.begin(), pdu.end());
        diagspec::lte_rrc::Sib1CellIdentity sib1;
        if (!diagspec::lte_rrc::decode_sib1_cell_access(msg, sib1)) {
            note(why, Decline::kNotSib1);
            return out;
        }
        // Unreachable by construction, and it stays.
        // decode_sib1_cell_access() returns true only when plmn_list is
        // non-empty (lte_rrc_sib.cpp's own sanity block), and ASN.1 types the
        // field SEQUENCE (SIZE (1..6)) — enforced by read_length(1, 6) — so an
        // empty-PLMN SIB1 does not exist even as a forgery.
        //
        // Not dead weight: mcc/mnc are read off plmn_list[0] four lines
        // below, so this is a live bounds guard on a postcondition that lives
        // in ANOTHER FILE. Deleting it would make the leg's memory safety
        // depend on an undocumented invariant of a function it merely calls.
        // The postcondition is pinned instead, by
        // test_decode_sib1_cell_access_NEVER_returns_true_with_an_empty_plmn_list.
        if (sib1.plmn_list.empty()) {
            note(why, Decline::kEmptyPlmn);
            return out;
        }

        CellObservation obs;
        obs.pci = static_cast<int32_t>(rec.pci());
        obs.earfcn = static_cast<int32_t>(rec.earfcn());
        obs.mcc = sib1.plmn_list[0].mcc;
        obs.mnc = sib1.plmn_list[0].mnc;
        obs.tac = sib1.tracking_area_code;
        obs.cell_id = sib1.cell_identity;
        obs.cell_barred = sib1.cell_barred;
        obs.csg_indication = sib1.csg_indication;
        for (const auto& plmn : sib1.plmn_list)
            obs.plmns.emplace_back(plmn.mcc, plmn.mnc);
        out.push_back(obs);
    } catch (const std::exception&) {
        note(why, Decline::kThrew);
        return out;
    }
    return out;
}

}  // namespace diag_0xb0c0_observation
