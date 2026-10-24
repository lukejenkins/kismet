#!/bin/sh
# celldiag_binary_mode.sh -- which decode mode a kismet_cap_cell_diag was LINKED in.
#
#   sh celldiag_binary_mode.sh <binary>
#
# Prints `native` (NATIVE=1: the C++ decode legs are linked in) or `baseline`
# (the Python-decode baseline: no C++ at all) on stdout and exits 0. When it
# cannot tell, it prints `unknown: <why>` on stderr and exits 1: no such file,
# no `nm`, no symbol table (a stripped copy), or not a celldiag helper at all.
# Usage errors exit 64.
#
# Used by standalone.mk's install-mode-guard (`make install` must
# ship the configured mode, not whichever one the tree was last linked in) and by
# standalone.mk's check-native (the native build must read as native).
#
# WHY THE BINARY, NOT .native_mode
# --------------------------------
# .native_mode records the mode of the last link the generated Makefile STARTED:
# it is a prerequisite of the link, rewritten before the linker runs, so a link
# that then fails leaves it naming a mode the binary on disk is not in. A copy
# of the binary anywhere else (build-replay-gate/, an installed copy) carries no
# .native_mode at all. The symbol table travels with the binary.
#
# THE RULE
# --------
# Both modes define the extern "C" decode surface capture_cell_diag.c calls
# (diag_native_decode: from the C++ shim under NATIVE=1, from
# diag_native_decode_stub.c in the baseline), so its presence says only "this is
# a celldiag helper". What differs is the Kaitai runtime. Every native decode leg
# parses through kaitai::kstream, so a NATIVE=1 link defines it. The baseline is
# pure C by construction (Makefile.in: "does this binary need libstdc++ is a
# real, checkable difference"), so it cannot.
#
# WHY NOT check_binary_symbols.sh
# -------------------------------
# That guard asserts the full per-leg polarity at LINK time, and it fails any
# binary older than the shim and leg headers it derives its expectations from.
# An install guard must not inherit that second rule: a baseline link is
# legitimately older than the C++ shim it does not link (editing
# diag_native_decode.cpp rebuilds nothing in the baseline), so the guard would
# refuse an install that no `make` could fix. This script answers only "which
# mode". check-native runs both on the same binary, so the two readings are
# held to each other.

bin="${1:-}"
if [ -z "$bin" ] || [ $# -ne 1 ]; then
    echo "usage: $0 <kismet_cap_cell_diag>" >&2
    exit 64
fi
if [ ! -f "$bin" ]; then
    echo "unknown: there is no file at $bin" >&2
    exit 1
fi
if ! command -v nm >/dev/null 2>&1; then
    echo "unknown: nm (binutils) is not installed, so $bin cannot be read" >&2
    exit 1
fi

syms=$(nm -C --defined-only "$bin" 2>/dev/null)
if [ -z "$syms" ]; then
    echo "unknown: $bin has no symbol table nm can read (a stripped copy, or not an object file)" >&2
    exit 1
fi

# Mach-O prefixes every C-level symbol with '_'; ELF does not. The C surface is
# a plain name, so the last field of nm's line is the whole of it.
if ! printf '%s\n' "$syms" | awk '{ n = $NF; sub(/^_/, "", n); if (n == "diag_native_decode") f = 1 } END { exit !f }'; then
    echo "unknown: $bin defines no diag_native_decode, so it is not a kismet_cap_cell_diag" >&2
    exit 1
fi

if printf '%s\n' "$syms" | grep -q 'kaitai::kstream::'; then
    echo native
else
    echo baseline
fi
