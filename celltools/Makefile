# celltools/Makefile -- the entry point for the Python harnesses in this
# directory. See README.md.
#
# Deliberately a plain makefile, not generated from a Makefile.in by
# ./configure. Everything here is stdlib + pytest and compiles nothing, so
# requiring a configured tree would make these checks unrunnable in exactly the
# state where they are most worth running: a fresh clone nobody has built yet.
# `make -C celltools check` works on a clone that has never seen ./configure.
#
# PYTHON overrides the interpreter; any Python 3 with pytest installed will do.
# There is no project virtualenv and there must not be one -- see README.md.

PYTHON ?= python3
PYTEST_ARGS ?= -q

.PHONY: check
check:
	$(PYTHON) -m pytest $(PYTEST_ARGS) $(dir $(lastword $(MAKEFILE_LIST)))tests

.PHONY: preflight
preflight:
	$(dir $(lastword $(MAKEFILE_LIST)))kismet-host-preflight.sh

# ---------------------------------------------------------------------------
# replay-gate -- build BOTH helper configurations, then compare their output
# ---------------------------------------------------------------------------
# The Python-decode baseline and the NATIVE=1 build must decode a capture into
# identical observations. Both run nativedecode=off, so both decode through the
# Python bridge: what this gauges is that LINKING the C++ closure does not
# perturb the bridge's output. It keeps either mode from rotting while the other
# is the one everybody happens to build.
#
# ⚠️ The two builds cannot coexist in the tree. NATIVE= is a make VARIABLE, so
# switching it relinks the SAME output path -- .native_mode exists precisely to
# force that relink. Each binary is therefore copied out to $(GATE_DIR) as soon
# as it is produced, and the tests are handed the two copies by absolute path.
# Building into the tree and comparing in place would compare one binary to
# itself.
#
# A measurement target must not mutate its measurement subject. Leaving the
# tree in a fixed mode (say, always baseline) means a tree built NATIVE=1 comes
# back baseline and the next measurement measures the wrong artifact: the
# staleness guard and `_native_binary_or_skip` fail with nothing wrong in the
# code under test. So the mode the tree was in is captured before the first
# $(MAKE) and restored after the last, and `replay-gate` is non-mutating for
# every starting state, including an UNBUILT tree, which must not be handed a
# binary that guards were correctly skipping for want of. Restoring is a
# relink, not a rebuild: the pure-C objects are shared by both modes, so it
# costs about a second.
#
# The build half is its own target so the restore can be exercised against a
# synthetic CELLDIAG_DIR -- the starting state is then a fixture rather than a
# comment. Hence CELLDIAG_DIR is ?=, and hence the entry mode is recorded in a
# FILE under $(GATE_DIR) rather than a recipe-local shell variable: each recipe
# line is its own shell, and a mid-target interrupt must still leave a later
# bare `make replay-gate-restore` able to put the tree back.
CELLDIAG_DIR ?= $(dir $(lastword $(MAKEFILE_LIST)))../capture_cell_diag
GATE_DIR ?= $(dir $(lastword $(MAKEFILE_LIST)))build-replay-gate
ENTRY_MODE = $(GATE_DIR)/.entry_mode

# The A/B is the gate, and its fixtures live outside the tree, so without
# CELL_CAPTURES it would run 1 test of 60 and exit 0, and skips like that can
# hide red A/B tests indefinitely. So: refuse at once when the corpus is absent (replay-gate-corpus runs BEFORE the build; this
# makefile is .NOTPARALLEL so -j cannot start the build beside it and leave the
# tree mid-relink), and run the suite with CELL_CAPTURES_REQUIRED=1 so a single
# missing fixture fails the session (tests/corpus_skip_banner.py). CELL_CAPTURES
# may be exported or given as a make variable; make exports the latter.
.PHONY: replay-gate
replay-gate: replay-gate-corpus replay-gate-build
	CELL_CAPTURES_REQUIRED=1 \
	CELLDIAG_BASELINE_BIN=$(abspath $(GATE_DIR)/kismet_cap_cell_diag.baseline) \
	CELLDIAG_NATIVE_BIN=$(abspath $(GATE_DIR)/kismet_cap_cell_diag.native) \
	    $(PYTHON) -m pytest $(PYTEST_ARGS) \
	        $(dir $(lastword $(MAKEFILE_LIST)))tests/test_celldiag_replay_ab.py

.PHONY: replay-gate-corpus
replay-gate-corpus:
	@if [ -z "$(CELL_CAPTURES)" ]; then \
	    echo "*** replay-gate: CELL_CAPTURES is unset. The replay A/B fixtures live" >&2; \
	    echo "*** outside the tree; without them the gate measures 1 test of 60." >&2; \
	    echo "*** Run: make -C celltools replay-gate CELL_CAPTURES=<capture dir>" >&2; \
	    exit 2; \
	 fi
	@if [ ! -d "$(CELL_CAPTURES)" ]; then \
	    echo "*** replay-gate: CELL_CAPTURES='$(CELL_CAPTURES)' is not a directory" >&2; \
	    exit 2; \
	 fi

# Nothing here compiles; the sub-makes it runs still take -j from MAKEFLAGS.
.NOTPARALLEL:

# Builds both configurations into $(GATE_DIR) and puts the tree back. Split out
# of replay-gate so the restore is testable without paying for the A/B suite,
# and so the tree is restored BEFORE the tests run -- a failing gate must not be
# the reason the tree is left in the wrong mode.
.PHONY: replay-gate-build
replay-gate-build:
	@mkdir -p $(GATE_DIR)
	@if [ -f $(CELLDIAG_DIR)/.native_mode ]; then \
	    cat $(CELLDIAG_DIR)/.native_mode > $(ENTRY_MODE); \
	 else \
	    echo unbuilt > $(ENTRY_MODE); \
	 fi
	@echo "  replay-gate: tree entered in mode '$$(cat $(ENTRY_MODE))'"
	$(MAKE) -C $(CELLDIAG_DIR) NATIVE=1
	cp $(CELLDIAG_DIR)/kismet_cap_cell_diag $(GATE_DIR)/kismet_cap_cell_diag.native
	$(MAKE) -C $(CELLDIAG_DIR)
	cp $(CELLDIAG_DIR)/kismet_cap_cell_diag $(GATE_DIR)/kismet_cap_cell_diag.baseline
	@$(MAKE) --no-print-directory -f $(lastword $(MAKEFILE_LIST)) \
	    CELLDIAG_DIR=$(CELLDIAG_DIR) GATE_DIR=$(GATE_DIR) replay-gate-restore
	@echo
	@echo "built both configurations into $(GATE_DIR):"
	@echo "  .native    = make NATIVE=1  (carved C++ legs linked)"
	@echo "  .baseline  = make           (pure C, Python bridge only)"
	@echo

# Puts $(CELLDIAG_DIR) back into the mode recorded in $(ENTRY_MODE). Safe to run
# by hand after an interrupted gate. `unbuilt` REMOVES the artifacts rather than
# leaving a baseline build: a tree that had no binary must come back with no
# binary, or every guard that skips for want of one silently starts running
# against a build nobody asked for.
.PHONY: replay-gate-restore
replay-gate-restore:
	@mode=$$(cat $(ENTRY_MODE) 2>/dev/null || echo unbuilt); \
	 case "$$mode" in \
	   native) \
	     echo "  replay-gate: restoring tree to NATIVE=1"; \
	     $(MAKE) --no-print-directory -C $(CELLDIAG_DIR) NATIVE=1 ;; \
	   baseline) \
	     echo "  replay-gate: restoring tree to baseline"; \
	     $(MAKE) --no-print-directory -C $(CELLDIAG_DIR) ;; \
	   unbuilt) \
	     echo "  replay-gate: tree had no build on entry -- removing artifacts"; \
	     rm -f $(CELLDIAG_DIR)/kismet_cap_cell_diag $(CELLDIAG_DIR)/.native_mode ;; \
	   *) \
	     echo "*** ERROR: unrecognised entry mode '$$mode' in $(ENTRY_MODE);" >&2; \
	     echo "*** refusing to guess -- put the tree back by hand ***" >&2; \
	     exit 1 ;; \
	 esac

.PHONY: replay-gate-clean
replay-gate-clean:
	rm -rf $(GATE_DIR)
