// SPDX-License-Identifier: GPL-2.0-or-later
// TS 24.301 structural-completeness check for a plain LTE NAS PDU -- the C++
// port of diaggrok's nas_pdu_structurally_complete()
// (diaggrok's parsers/_lte_nas_ota_helpers.py), table for
// table and in the same walk order.
//
// The DIAG NAS-OTA wrapper carries no length field: the NAS PDU is "the rest
// of the record", so a truncated record is only detectable from the message's
// own structure. diaggrok's shared NAS-OTA framer refuses a record that
// provably cannot hold what it declares; the GSMTAP bridges for
// 0xB0E2/E3/EC/ED call this so they decline exactly the records diaggrok
// refuses instead of framing a truncated PDU.
#ifndef DIAGSPEC_LTE_NAS_STRUCTURE_H
#define DIAGSPEC_LTE_NAS_STRUCTURE_H

#include <cstddef>
#include <cstdint>

namespace diagspec {
namespace lte_nas {

// Python's three-valued answer.
enum class Complete {
  kNo,           // False: a mandatory field is missing, or a declared length
                 //        runs past the end -- the record is truncated
  kYes,          // True: every declared octet is present
  kNotCheckable, // None: unknown message type, unknown protocol
                 //       discriminator, or a security-protected header
};

// Does `pdu` hold every octet its own structure declares? `is_uplink`
// selects the direction-dependent Detach request layout (TS 24.301 §8.2.11).
Complete structurally_complete(const uint8_t* pdu, std::size_t n, bool is_uplink);

}  // namespace lte_nas
}  // namespace diagspec

#endif  // DIAGSPEC_LTE_NAS_STRUCTURE_H
