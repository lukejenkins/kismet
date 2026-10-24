// SPDX-License-Identifier: GPL-2.0-or-later
// Linkable surface of the 0xB0C0 cell_observation identity enrichment leg.
//
// Unlike the 0xB192 and 0xB195 legs, this one emits a full-identity cell, not a
// bare (pci, earfcn) signal observation. A SIB1-bearing 0xB0C0 (LTE RRC OTA,
// BCCH-DL-SCH) self-emits its own identity cell (PLMN / TAC / CellID + serving pci/earfcn) — mirroring dlf_to_wigle
// scanning 0xB0C0 as a Pass-2 measurement, which is what lets a DIAG-only wardrive
// match its full WiGLE cell set with no AT source. It exists so the celldiag capture
// helper can decode 0xB0C0 identity in-process (pure C++) instead of forking the
// Python bridge (kismet_diag_decode.py), behind the identical
// (log_code, payload) -> cell_observation contract.
//
// This leg crosses the ASN.1 boundary: it composes three already-verified C++
// units rather than reimplementing any decode —
//   * generated Kaitai raw parse (generated/cpp/diag_0xb0c0.*) for the version-keyed
//     header: version, channel_type, pci, earfcn, msg_data;
//   * diag_0xb0c0_enrich::channel_name() to resolve the canonical channel NAME off
//     the per-version channel_type enum (the SIB1 gate keys on the NAME);
//   * diagspec::lte_rrc::decode_sib1_cell_access() (enrichment/cpp/lte_rrc_sib.*,
//     byte-verified vs diaggrok) for the SIB1 cellAccessRelatedInfo UPER
//     decode → PLMN / TAC / CellID.
//
// EQUIVALENCE CONTRACT: the identity observation this leg emits must equal the
// Python bridge's 0xB0C0 emit (kismet_diag_decode.py -> diaggrok parse_0xb0c0's
// SIB1 auto-decode) over every committed lte_rrc_sib1_ident_b0c0_*.bin fixture. The
// bridge emits an identity cell iff parse_0xb0c0 populated sib1_tac — i.e. the
// record is BCCH-DL-SCH AND decode_sib1_cell_access returned a SIB1 with a
// non-empty PLMN list. This leg gates identically. No rsrp: SIB1 is OTA identity,
// not a signal measurement, so the bridge's 0xB0C0 branch never attaches one.
#ifndef DIAGSPEC_DIAG_0XB0C0_OBSERVATION_H
#define DIAGSPEC_DIAG_0XB0C0_OBSERVATION_H

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace diag_0xb0c0_observation {

// One full-identity cell observation self-emitted by a SIB1-bearing 0xB0C0 record.
// rat is always "LTE"; is_serving is always false (the bridge sets is_serving=False
// on the 0xB0C0 identity cell — SIB1 is a broadcast identity, not a serving-cell
// signal measurement). mcc/mnc are the SIB1 PLMN[0] as decimal strings (mcc may be
// "" only if the OPTIONAL MCC field was absent, matching diaggrok/asn1); tac and
// cell_id are the SIB1 TrackingAreaCode / CellIdentity.
struct CellObservation {
    int32_t pci;
    int32_t earfcn;
    std::string mcc;
    std::string mnc;
    uint32_t tac;
    uint32_t cell_id;
    // SIB1 cellAccessRelatedInfo flags, which the bridge emits on every identity
    // cell (`cell_barred`, `csg`): parse_0xb0c0 sets both whenever the SIB1
    // decoded, which is exactly when this leg emits. cell_barred is true when
    // cellBarred == barred.
    bool cell_barred = false;
    bool csg_indication = false;
    // The full SIB1 PLMN list as (mcc, mnc), [0] being mcc/mnc above. The bridge
    // emits it as `plmns` only for a MOCN / RAN-shared cell (more than one
    // entry); the leg carries the list and leaves that rule to the renderer.
    std::vector<std::pair<std::string, std::string>> plmns;
};

// The decline vocabulary.
//
// This leg declines five structurally different ways — wrong channel, empty PDU,
// the PDU not decoding as a SIB1, a SIB1 with an empty PLMN list, and a throw —
// and all five return the same empty vector. Under an observation-list oracle
// they are one observable, so a mutation test cannot tell the gates apart
// (removing the channel gate or the empty-PLMN gate looks like a no-op unless a
// forged input reaches it).
//
// The gates do not mask each other in the code: each runs at a different point
// and refuses for a different reason. They only mask in the measurement. So
// rather than forging fixtures, the leg reports which path ran, as the
// 0xB192 / 0xB195 legs do. The gates exist to reproduce the bridge's two-part
// gate exactly, which is a claim about which branch runs; reporting the branch
// is what lets a test pin it.
//
// Strictly additive: `why` defaults to nullptr, every existing call site
// (`observations(payload)`) compiles and behaves byte-identically, and the
// returned vector is untouched.
enum class Decline {
    kNone = 0,        // the walk completed — `out` is the answer
    kNotSib1Channel,  // channel_name() != "BCCH-DL-SCH"
    kEmptyPdu,        // msg_data() was empty (the bridge's `and msg_data` guard)
    kNotSib1,         // decode_sib1_cell_access returned false (SIB2+, or not a SIB)
    kEmptyPlmn,       // a SIB1 decoded, with no PLMN in cellAccessRelatedInfo
    kThrew,           // the Kaitai / UPER walk threw; the catch answered
    kTruncated,       // header_size + msg_length > the payload (parse_0xb0c0 -> None)
    kVersion,         // byte 0 outside parse_0xb0c0's version allowlist (-> None)
};

// Stable lowercase name for a Decline, for drivers and diagnostics. Never null.
const char* decline_name(Decline d);

// True exactly when the decline is a record diaggrok's parser returns None for
//: the native census then counts the record DROPPED. Every other
// decline is a completed parse that emitted nothing, and is counted decoded.
bool is_refusal(Decline d);

// Decode a raw 0xB0C0 LOG_F payload (log-code-stripped, as diaggrok/the bridge see
// it) into zero or one identity observations. Returns an empty vector unless the
// record is BCCH-DL-SCH carrying a SIB1 with a non-empty PLMN list — exactly the
// condition under which the Python bridge emits a 0xB0C0 identity cell (its
// `if result.sib1_tac is not None` guard). A malformed / truncated payload is
// declined (empty vector), never propagated as an exception — a capture source must
// not let one bad record abort the live stream.
//
// When `why` is non-null it receives the path taken. Unlike 0xB192/0xB195,
// this leg emits at most ONE observation, so an empty vector with `*why == kNone`
// is unreachable here — every empty return is a decline. That makes the parameter
// *more* load-bearing rather than less: there is no other signal at all.
std::vector<CellObservation> observations(const std::string& payload,
                                          Decline* why = nullptr);

}  // namespace diag_0xb0c0_observation

#endif  // DIAGSPEC_DIAG_0XB0C0_OBSERVATION_H
