#!/bin/sh
# check_binary_symbols.sh -- assert that each shipping binary carries EXACTLY the
# decode legs it can actually call, derived from the SOURCE TREE.
#
# The three roles assert OPPOSITE polarities, and that is the point:
#
#   helper  PRESENCE -- every leg diag_native_decode.cpp forwards to must be
#                       DEFINED IN kismet_cap_cell_diag.  That process holds the
#                       raw DIAG bytes; a leg missing from it cannot run.
#   baseline BOTH    -- the same binary built the DEFAULT way (`make`, no
#                       NATIVE=1): the Python-decode baseline.  Every leg must be
#                       ABSENT -- the Python bridge is the sole decoder and a leg
#                       linked here is the very thing the mode exists to omit --
#                       WHILE the extern "C" surface capture_cell_diag.c calls
#                       stays PRESENT, supplied by diag_native_decode_stub.c.
#                       Both halves derive from the same two files the `helper`
#                       role reads, so the two roles cannot disagree about what
#                       a leg is.
#
#                       The absence half is what makes the baseline provable
#                       rather than merely asserted.  Do not instead "accept an
#                       empty expected set as a pass in no-native mode": the set
#                       is derived by sed-ing the SOURCE TEXT of
#                       diag_native_decode.cpp, and a build switch changes what
#                       the LINK consumes without editing that file.  The set
#                       stays at its full size in every mode, so the empty-set
#                       branch would be unreachable code AND a hole in the
#                       invariant the block at "An empty derived set is a
#                       failure" calls load-bearing.
#   server  ABSENCE  -- NO leg may be defined in `kismet`.  The server never sees
#                       a raw DIAG payload (the helper's only upstream call is
#                       cf_send_json(), with already-decoded JSON), so a leg
#                       linked there is byte-correct, self-tested, and
#                       STRUCTURALLY UNREACHABLE.  The check asserts absence
#                       rather than being omitted, so a misplaced leg cannot
#                       quietly return.
#
# THE FAILURE MODE THIS EXISTS FOR: a decode leg that compiles, passes its
# selftest, and is not in the binary that needs it.  Examples of the shape:
#
#   - a stale generated top-level Makefile silently omitting DIAGSPEC_O legs
#     from the `kismet` link;
#   - a leg whose only references anywhere are its own selftest, because the
#     shim entry point that should call it does not exist;
#   - a bootstrap-stale capture_cell_diag/Makefile running fewer `make check`
#     suites than Makefile.in declares, and reading GREEN;
#   - MONITOR_OBJS not listing diag_native_decode.cpp.o, so kismet_cap_cell_diag,
#     the process that actually holds the raw DIAG bytes, ships with ZERO decode
#     legs in it.
#
# The build succeeds every time, so nothing complains; that is the argument for
# a guard rather than more vigilance.
#
# WHY THE EXISTING GUARDS DON'T CATCH IT
#
#   - `make check` proves a leg COMPILES and DECODES CORRECTLY. It says nothing
#     about which shipping binary links it -- a missing MONITOR_OBJS entry leaves
#     every selftest passing.
#   - check_diagspec_o.sh compares the generated Makefile's DIAGSPEC_O against
#     Makefile.in's. That is list-vs-list, for the SERVER only; it cannot see a
#     missing entry in a DIFFERENT binary's list.
#   - The helper-side A/B parity harnesses pin BYTE CORRECTNESS, not reachability.
#
# The common shape: every existing guard checks an artifact against its
# DECLARATION, and the bug is always a MISSING declaration. So this guard derives
# what it expects from the SOURCE TREE -- never from a hand-maintained list, or it
# would acquire the exact failure mode it exists to catch.
#
#   server role: every diagspec/enrichment/cpp/diag_0x*_{observation,gsmtap,
#                exported_pdu}.h in the tree.  Adding a leg header extends the
#                checked set with no edit here -- and under the ABSENCE polarity
#                that means a NEW leg is barred from the server automatically,
#                which is the property an exclusion list could never have.
#   helper role: the leg headers diag_native_decode.cpp actually #includes, plus
#                the extern "C" surface diag_native_decode.h declares.  The shim's
#                own include list IS the declaration of which legs the helper
#                forwards to, so it cannot drift from it.
#
# In both cases the fully-qualified symbol name is read out of the header
# (namespace stack + declared free functions) rather than assumed, because this
# tree already uses two different conventions -- diag_0xbNNN_observation::
# observations() and diagspec::celldiag::diag_0xbNNN_to_gsmtap().  Hardcoding
# either would make the guard blind to a leg written in the other.
#
# Note the server glob deliberately does NOT match diag_0x1476_gpsfix.h.  That is
# the GNSS leg, helper-only by construction; naming it outside the three
# server-side suffixes is what keeps it out of the server absence set.  A leg
# named *_observation / *_gsmtap / *_exported_pdu is one that a server-side sink
# was once imagined for, and those are exactly the ones this role must keep out.
#
# Usage: diagspec/check_binary_symbols.sh <server|helper|baseline> <binary> [repo-root]
#
# Exits 0 when the binary satisfies its role's polarity; 1 otherwise.
# ABSENT or STALE binary is a FAILURE, never a skip: a skip recreates exactly
# the silence this guard exists to break.

set -e

ROLE="$1"
BIN="$2"
ROOT="${3:-$(dirname "$0")/..}"

if [ -z "$ROLE" ] || [ -z "$BIN" ]; then
    echo "usage: $0 <server|helper|baseline> <binary> [repo-root]" >&2
    exit 2
fi

ENRICH="$ROOT/diagspec/enrichment/cpp"
SHIM_C="$ROOT/capture_cell_diag/diag_native_decode.cpp"
SHIM_H="$ROOT/capture_cell_diag/diag_native_decode.h"

# A tree can carry this guard without the decode definitions: no
# diagspec/enrichment directory at all.  Then no decode leg can be linked, by
# construction, so the absence roles (server, baseline) have no legs to assert
# absent and say so; baseline still asserts the extern "C" surface.  The helper
# role needs the legs and fails.  An enrichment directory that EXISTS but holds
# no leg headers is still the empty-set error below: that is a broken tree, not
# one built without definitions.
NO_DEFS=0
[ -d "$ENRICH" ] || NO_DEFS=1
if [ "$NO_DEFS" = 1 ] && [ "$ROLE" = "helper" ]; then
    echo "*** ERROR: role 'helper' needs the decode definitions, and $ENRICH does not exist ***" >&2
    echo "This tree does not include the C++ decode sources; build the Python-decode" >&2
    echo "baseline (NATIVE= unset) instead." >&2
    exit 1
fi

# --- expected symbols, derived from the source tree -------------------------

# Read a C/C++ header and emit one fully-qualified free-function name per line.
#
# Only declarations at namespace scope count: a `struct`/`class`/`enum` body
# opens a non-namespace level and everything inside it is skipped, so struct
# members are never mistaken for entry points. `extern "C" {` is transparent
# (it contributes no name qualifier) -- that is what lets the helper's un-mangled
# C surface come out of the same parser as the mangled C++ legs.
decls() {
    awk '
        # Preprocessor lines carry no declarations and their braces are unbalanced
        # across #ifdef arms -- skipping them keeps the depth tracking honest.
        /^[ \t]*#/ { next }
        {
            line = $0
            sub(/\/\/.*/, "", line)          # strip line comments
            sub(/\/\*.*\*\//, "", line)      # strip single-line block comments
            if (in_comment) {
                if (line ~ /\*\//) { sub(/.*\*\//, "", line); in_comment = 0 }
                else next
            }
            if (line ~ /\/\*/) { sub(/\/\*.*/, "", line); in_comment = 1 }
            if (line ~ /^[ \t]*$/) next

            # namespace open: pushes a name qualifier
            if (match(line, /^[ \t]*namespace[ \t]+[A-Za-z_][A-Za-z0-9_]*[ \t]*\{/)) {
                n = line
                sub(/^[ \t]*namespace[ \t]+/, "", n)
                sub(/[ \t]*\{.*/, "", n)
                depth++; kind[depth] = "ns"; nsname[depth] = n
                next
            }
            # extern "C" { : a scope with no qualifier
            if (line ~ /^[ \t]*extern[ \t]+"C"[ \t]*\{/) {
                depth++; kind[depth] = "ns"; nsname[depth] = ""
                next
            }

            # A declaration is only an entry point if every open scope is a
            # namespace. Capture it BEFORE the braces on this very line are
            # counted, so `struct X {` is excluded but a one-line decl is not.
            at_ns_scope = 1
            for (d = 1; d <= depth; d++) if (kind[d] != "ns") at_ns_scope = 0

            if (at_ns_scope && line !~ /^[ \t]*(struct|class|union|enum|typedef|using|return)\b/ &&
                line ~ /[A-Za-z_][A-Za-z0-9_]*[ \t]*\(/) {
                # The identifier immediately preceding the first "(" is the name;
                # require something (a return type) before it, so a macro
                # invocation or a bare call is not mistaken for a declaration.
                head = line
                sub(/\(.*/, "", head)
                if (match(head, /[A-Za-z_][A-Za-z0-9_]*[ \t]*$/)) {
                    nm = substr(head, RSTART, RLENGTH)
                    gsub(/[ \t]/, "", nm)
                    pre = substr(head, 1, RSTART - 1)
                    if (pre ~ /[A-Za-z_>&*]/) {
                        q = ""
                        for (d = 1; d <= depth; d++)
                            if (nsname[d] != "") q = q nsname[d] "::"
                        print q nm
                    }
                }
            }

            # Now account for the braces on this line.
            n_open = gsub(/\{/, "{", line)
            n_close = gsub(/\}/, "}", line)
            for (i = 0; i < n_open; i++) { depth++; kind[depth] = "other"; nsname[depth] = "" }
            for (i = 0; i < n_close; i++) if (depth > 0) depth--
        }
    ' "$1"
}

# EXPECT is the set this role asserts its POLARITY over: presence for `helper`,
# absence for `server` and `baseline`.  EXPECT_PRESENT is a second, opposite
# assertion only `baseline` uses -- the extern "C" surface that must survive the
# legs' removal, because capture_cell_diag.c calls it at link scope in every
# mode.  Kept separate rather than folded into a "role polarity" flag so each set
# names what it is FOR: one is decode legs, the other is the caller's ABI.
EXPECT=""
EXPECT_PRESENT=""
SOURCES=""

case "$ROLE" in
server)
    for h in "$ENRICH"/diag_0x*_observation.h "$ENRICH"/diag_0x*_gsmtap.h \
             "$ENRICH"/diag_0x*_exported_pdu.h; do
        [ -f "$h" ] || continue
        SOURCES="$SOURCES $h"
        EXPECT="$EXPECT$(decls "$h")
"
    done
    ;;
helper|baseline)
    if [ ! -f "$SHIM_C" ] || [ ! -f "$SHIM_H" ]; then
        echo "*** ERROR: $SHIM_C / $SHIM_H missing -- cannot derive the expected symbol set ***" >&2
        exit 1
    fi
    SOURCES="$SHIM_C $SHIM_H"
    # The shim's own extern "C" surface: the symbols capture_cell_diag.c calls.
    SURFACE="$(decls "$SHIM_H")"
    # ...and every leg it forwards to, named by its own #include list.
    #
    # A leg is any shim #include that resolves into $ENRICH, not only the
    # diag_0x*_*.h names. lte_meas_join.h is a leg that is not named
    # after a log code; matching diag_0x* alone would leave its symbols
    # unasserted in BOTH polarities -- never required in the native helper,
    # never forbidden in the baseline. A diag_0x* include that does not resolve is still an
    # error; any other non-resolving include (diag_native_decode.h, system
    # headers) is simply not a leg.
    LEGS=""
    for inc in $(sed -n 's/^#include[ \t]*"\([A-Za-z0-9_]*\.h\)".*/\1/p' "$SHIM_C"); do
        h="$ENRICH/$inc"
        if [ ! -f "$h" ]; then
            case "$inc" in
            diag_0x*)
                [ "$NO_DEFS" = 1 ] && continue
                echo "*** ERROR: $SHIM_C includes $inc, which is not in $ENRICH ***" >&2
                exit 1 ;;
            *) continue ;;
            esac
        fi
        SOURCES="$SOURCES $h"
        LEGS="$LEGS$(decls "$h")
"
    done
    if [ "$ROLE" = "baseline" ]; then
        # Legs absent, caller ABI present.  Note $SHIM_C is still read (it names
        # the legs) and still a staleness input, even though the object it
        # compiles to is NOT in this binary: the question the role answers is
        # "does the tree's declared leg set appear here", and the tree declares
        # it there regardless of what got linked.
        EXPECT="$LEGS"
        EXPECT_PRESENT="$SURFACE"
    else
        EXPECT="$SURFACE
$LEGS"
    fi
    ;;
*)
    echo "usage: $0 <server|helper|baseline> <binary> [repo-root]" >&2
    exit 2
    ;;
esac

EXPECT=$(printf '%s\n' "$EXPECT" | grep -v '^[[:space:]]*$' | sort -u)
EXPECT_PRESENT=$(printf '%s\n' "$EXPECT_PRESENT" | grep -v '^[[:space:]]*$' | sort -u)

# An empty derived set is a failure under BOTH polarities, for the same reason:
# the guard would be asserting nothing at all.  Under PRESENCE it passes against
# a binary containing nothing; under ABSENCE it passes against a binary
# containing everything.  Either way the tree lost the headers the set comes
# from, which is the bug and not the baseline.
if [ -z "$EXPECT" ] && [ "$NO_DEFS" = 1 ]; then
    echo "  (no decode definitions in this tree: $ENRICH is absent, so no decode leg can be linked)"
elif [ -z "$EXPECT" ]; then
    echo "*** ERROR: derived an EMPTY decode-leg symbol set for role '$ROLE' ***" >&2
    echo "That is itself the bug this guard exists to catch -- a guard with an" >&2
    echo "empty set asserts nothing, whichever direction it asserts it in." >&2
    echo "Check that $ENRICH still holds the leg headers." >&2
    exit 1
fi

# Same argument, applied to the baseline's second set.  An empty caller ABI would
# make the presence half vacuous, and it is the half that proves the stub is
# actually in the binary -- without it "no legs" is satisfied by a helper that
# links nothing at all.
if [ "$ROLE" = "baseline" ] && [ -z "$EXPECT_PRESENT" ]; then
    echo "*** ERROR: derived an EMPTY extern \"C\" surface from $SHIM_H ***" >&2
    echo "The baseline role would then assert only that legs are absent, which a" >&2
    echo "binary with no decode surface at all also satisfies." >&2
    exit 1
fi

# --- the binary must exist, and must not predate its sources ----------------

if [ ! -f "$BIN" ]; then
    echo "*** ERROR: $BIN does not exist ***" >&2
    echo "" >&2
    echo "Build it first.  This is a FAILURE and not a skip on purpose: a skip" >&2
    echo "recreates the silence this guard exists to break." >&2
    exit 1
fi

# No sources (server role, no definitions) means nothing to be newer than;
# find with no path would search the working directory instead.
NEWER=""
[ -n "$SOURCES" ] && NEWER=$(find $SOURCES -newer "$BIN" 2>/dev/null | head -5)
if [ -n "$NEWER" ]; then
    echo "*** ERROR: $BIN is STALE -- these sources are newer than it: ***" >&2
    printf '%s\n' "$NEWER" | sed 's/^/    /' >&2
    echo "" >&2
    echo "A symbol check against a stale binary reports on code that is not in it." >&2
    echo "Rebuild, then re-run." >&2
    exit 1
fi

# --- assert ----------------------------------------------------------------

HAVE=$(nm -C -g --defined-only "$BIN" 2>/dev/null | sed -n 's/^[0-9a-fA-F]* [TWiI] //p')

# Mach-O prefixes every C-level symbol with '_' (`_diag_native_decode`); ELF does
# not. Demangled C++ names arrive without it either way, so only the extern "C"
# surface is affected -- in BOTH directions: without stripping it, a Darwin
# baseline fails "MISSING the extern C decode surface" with every symbol
# present, and an extern "C" leg linked where it must be absent is invisible.
# Decided by the BINARY's magic, not by uname: an ELF cross-built on a Mac must
# keep its names as they are.
case "$(od -An -tx1 -N4 "$BIN" 2>/dev/null | tr -d ' \n')" in
    cffaedfe|cefaedfe|feedfacf|feedface|cafebabe)
        HAVE=$(printf '%s\n' "$HAVE" | sed 's/^_//') ;;
esac

# defined_in <symbol> -- true when the binary defines it.  A C++ leg demangles to
# "ns::fn(args...)"; an extern "C" symbol to bare "fn".  Shared by both
# polarities so "found" means the same thing in each direction.
defined_in() {
    printf '%s\n' "$HAVE" | grep -qxF "$1" && return 0
    printf '%s\n' "$HAVE" | grep -qF "$1(" && return 0
    return 1
}

FOUND=""
NOTFOUND=""
for sym in $EXPECT; do
    if defined_in "$sym"; then FOUND="$FOUND $sym"; else NOTFOUND="$NOTFOUND $sym"; fi
done

FOUND_P=""
NOTFOUND_P=""
for sym in $EXPECT_PRESENT; do
    if defined_in "$sym"; then FOUND_P="$FOUND_P $sym"; else NOTFOUND_P="$NOTFOUND_P $sym"; fi
done

case "$ROLE" in
baseline)
    if [ -n "$FOUND" ]; then
        echo "*** ERROR: $BIN is the PYTHON-DECODE BASELINE and LINKS decode legs ***" >&2
        echo "" >&2
        for sym in $FOUND; do echo "    $sym" >&2; done
        echo "" >&2
        echo "The baseline configuration decodes every DIAG payload through the" >&2
        echo "Python bridge; its whole definition is a helper built with zero" >&2
        echo "native decode objects.  A leg linked here is not a spare tyre --" >&2
        echo "it is the C++ dependency (and the ../diagspec/ tree behind it)" >&2
        echo "that this configuration exists to prove it does not need." >&2
        echo "" >&2
        echo "You are probably looking at a stale binary from a NATIVE=1 build:" >&2
        echo "run 'make clean && make' in capture_cell_diag/, or 'make NATIVE=1'" >&2
        echo "if the native configuration was what you meant to build." >&2
        exit 1
    fi
    if [ -n "$NOTFOUND_P" ]; then
        echo "*** ERROR: $BIN is MISSING the extern \"C\" decode surface ***" >&2
        echo "" >&2
        for sym in $NOTFOUND_P; do echo "    $sym" >&2; done
        echo "" >&2
        echo "capture_cell_diag.c calls these at link scope in EVERY mode -- the" >&2
        echo "nativedecode=off default is a runtime branch, not a compile-time" >&2
        echo "one.  In the baseline build they come from" >&2
        echo "capture_cell_diag/diag_native_decode_stub.c; check that it is in" >&2
        echo "NATIVE_OBJS_STUB and that it still implements all of" >&2
        echo "diag_native_decode.h.  A symbol declared in that header and" >&2
        echo "implemented only in the C++ shim makes the baseline unlinkable," >&2
        echo "which is the failure this whole configuration exists to prevent." >&2
        exit 1
    fi
    echo "OK: $BIN carries all $(printf '%s\n' "$EXPECT_PRESENT" | wc -l | tr -d ' ') extern \"C\" entry points and none of the $(printf '%s\n' "$EXPECT" | wc -l | tr -d ' ') decode legs (baseline)"
    ;;
helper)
    if [ -n "$NOTFOUND" ]; then
        echo "*** ERROR: $BIN is MISSING decode legs the source tree declares ***" >&2
        echo "" >&2
        for sym in $NOTFOUND; do echo "    $sym" >&2; done
        echo "" >&2
        echo "Add the leg's source to DIAGSPEC_SRCS (and diag_native_decode.cpp.o to" >&2
        echo "NATIVE_OBJS) in capture_cell_diag/Makefile.in, re-run ./configure" >&2
        echo "so the generated Makefile links it, then rebuild." >&2
        echo "" >&2
        echo "A leg that is compiled, self-tested, and in no shipping binary cannot run." >&2
        exit 1
    fi
    echo "OK: $BIN defines all $(printf '%s\n' "$EXPECT" | wc -l | tr -d ' ') declared decode entry points (helper)"
    ;;
server)
    if [ -n "$FOUND" ]; then
        echo "*** ERROR: $BIN LINKS decode legs it can never call ***" >&2
        echo "" >&2
        for sym in $FOUND; do echo "    $sym" >&2; done
        echo "" >&2
        echo "The kismet server never decodes DIAG.  Observations arrive from the" >&2
        echo "capture helper as already-decoded JSON, and raw DIAG slices are" >&2
        echo "only logged, as opaque packets.  There is no path by which a" >&2
        echo "caller for these could exist, so a leg linked" >&2
        echo "here is dead weight in a 400+ MB binary and, worse, reads as though" >&2
        echo "the server decodes DIAG." >&2
        echo "" >&2
        echo "Remove the leg's object from DIAGSPEC_O in Makefile.in, re-run" >&2
        echo "./configure, and rebuild.  Decode belongs in kismet_cap_cell_diag" >&2
        echo "(DIAGSPEC_SRCS), which is where the raw bytes are." >&2
        echo "" >&2
        echo "If server-side DIAG decode is ever DESIGNED, flip" >&2
        echo "this role back to a presence assertion -- do not just delete it." >&2
        exit 1
    fi
    echo "OK: $BIN defines none of the $(printf '%s\n' "$EXPECT" | wc -l | tr -d ' ') decode entry points it could not call (server)"
    ;;
esac

exit 0
