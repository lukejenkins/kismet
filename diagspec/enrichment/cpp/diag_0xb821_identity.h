// SPDX-License-Identifier: GPL-2.0-or-later
// 0xB821 (NR5G RRC OTA) → SIB1 cell IDENTITY leg — the NR analogue of
// diag_0xb0c0_observation, moving NR identity off the Python bridge.
//
// This is not an observation leg, and the difference is deliberate.
// diag_0xb0c0_observation emits a CellObservation because the Python bridge's
// result_to_observations has a `Diag0xB0C0` branch that self-emits an identity
// cell. It has NO `Diag0xB821` branch: a 0xB821 record contributes to the
// sib1_map and nothing else — it never produces a cell_observation of its own.
// Giving this leg an `observations()` surface would therefore ADD rows the
// bridge does not emit, breaking parity in the direction that is hardest to
// notice (an observation count that grew, on a code nobody was counting).
//
// So the surface is `identity()`: zero-or-one map entry, matching diaggrok's
// update_sib1_map(0xB821) exactly — key (pci, arfcn), value MCC/MNC/TAC/NCI —
// plus the same SIB1's cell config, the bridge's ("nr_cfg", pci, arfcn) entry.
#ifndef DIAGSPEC_DIAG_0XB821_IDENTITY_H
#define DIAGSPEC_DIAG_0XB821_IDENTITY_H

#include <climits>
#include <cstdint>
#include <string>

namespace diag_0xb821_identity {

// One (pci, arfcn) → identity entry recovered from a SIB1-bearing NR RRC OTA
// record. `valid` false means the record contributes nothing to the map — not
// BCCH-DL-SCH, empty PDU, or the SIB1 decode declined.
// Why observations() returned what it did. kRefused is exactly a record
// diaggrok's parser returns None for: the census counts it DROPPED. kNone is a
// completed parse, whatever its row count, and is counted decoded. The out-param
// is additive: `observations(payload)` behaves byte-for-byte as before.
enum class Decline {
    kNone = 0,
    kRefused,
};
bool is_refusal(Decline d);

// The C++ spelling of the parser's `None` for the CellConfig ints below. It is
// INT_MIN, the same value as nr_rrc::kNrAbsent the SIB1 decoder fills them from.
constexpr int kAbsent = INT_MIN;

// The SIB1 servingCellConfigCommon headline: the bridge's ("nr_cfg", pci, arfcn)
// cache entry, which observation.add_nr_cell_config attaches to every NR
// observation of the cell as band / carrier_scs / bandwidth_rb / bandwidth.
// Each field is kAbsent where Diag0xB821's sib1_* field is None: the walk did
// not reach FrequencyInfoDL-SIB (scc.walk_stopped_at), SIB1 has no
// servingCellConfigCommon, or, for bandwidth_mhz alone, the (SCS, N_RB) pair is
// not a TS 38.101 Table 5.3.2-1 entry. `valid` is "at least one field is set",
// observation._cache_nr_cell_config's `if cfg:`. An invalid config is not cached.
struct CellConfig {
    bool valid = false;
    int band = kAbsent;             // sib1_band: first DL freqBandIndicatorNR
    int carrier_scs_khz = kAbsent;  // sib1_scs_khz: first scs-SpecificCarrier
    int bandwidth_rb = kAbsent;     // sib1_carrier_bandwidth_prb
    int bandwidth_mhz = kAbsent;    // sib1_channel_bandwidth_mhz
};

struct CellIdentity {
    bool valid = false;
    int32_t pci = 0;
    int32_t arfcn = 0;       // NR-ARFCN; the sib1_map key's second component
    std::string mcc;
    std::string mnc;
    uint32_t tac = 0;        // 24-bit; 0 is LEGITIMATE (OPTIONAL in 38.331)
    uint64_t cell_id = 0;    // 36-bit NCI
    // From the same SIB1 decode. Meaningful only when `valid`: the bridge
    // writes its cache behind the same SIB1 gate as the identity.
    CellConfig config;
};

// Decode one log-code-stripped 0xB821 DIAG payload into a map entry.
//
// Gate, mirroring diaggrok parse_0xb821 + update_sib1_map:
//   channel_name(version, pdu_id) == "BCCH-DL-SCH"   (resolved off the
//     per-version pdu_id enum, never the raw integer)
//   AND msg_data non-empty
//   AND decode_nr_sib1 succeeds  (== the bridge's `sib1_tac is not None`)
// Any other outcome returns valid == false. Never throws: a malformed record
// declines, because a capture source must not abort its stream over one frame.
// A valid entry also carries `config`, filled from the decode's `scc`.
CellIdentity identity(const std::string& payload, Decline* why = nullptr);

// One (pci, arfcn) -> NR SSB subcarrier spacing entry, the bridge's SECOND
// 0xB821 cache: update_sib1_map's `("scs", pci, arfcn)` key, read by
// observation.add_nr_scs onto every NR observation of that cell.
// `valid` false means the record carries no MIB SCS and contributes nothing.
struct CellScs {
    bool valid = false;
    int32_t pci = 0;
    int32_t arfcn = 0;
    int scs_khz = 0;         // 15 / 30 (FR1) or 60 / 120 (FR2)
};

// Decode one log-code-stripped 0xB821 payload's MIB subCarrierSpacingCommon.
//
// Gate, mirroring parse_0xb821's MIB auto-decode + observation._mib_scs_khz:
//   channel_name(version, pdu_id) == "BCCH-BCH"
//   AND msg_data non-empty
//   AND decode_mib succeeds (its outer CHOICE is `mib`, as parse_0xb821's
//     msg_class_name == "mib" gate requires)
// The ENUMERATED value is resolved to kHz by the record's own NR-ARFCN:
// scs15or60 -> 15 (FR1) / 60 (FR2), scs30or120 -> 30 / 120, FR2 meaning
// arfcn >= 2016667 (TS 38.104). It is independent of identity(): the MIB rides
// on records that carry no SIB1, and the bridge writes this cache regardless of
// the SIB1 gate. Never throws.
CellScs mib_scs(const std::string& payload, Decline* why = nullptr);

}  // namespace diag_0xb821_identity

#endif  // DIAGSPEC_DIAG_0XB821_IDENTITY_H
