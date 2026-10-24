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
check: pytest-available
	$(PYTHON) -m pytest $(PYTEST_ARGS) $(dir $(lastword $(MAKEFILE_LIST)))tests

# An interpreter without pytest otherwise fails with a bare "No module named
# pytest" and exit 2, which reads like a failing suite. Say what to change
# instead. On a development workstation the system python3 often has no
# pytest while the checkout's venv does.
.PHONY: pytest-available
pytest-available:
	@$(PYTHON) -c 'import pytest' 2>/dev/null || { \
	    echo "*** celltools: '$(PYTHON)' has no pytest, so no test can run." >&2; \
	    echo "*** Point PYTHON at an interpreter that has it, e.g. a venv's:" >&2; \
	    echo "***   make -C celltools check PYTHON=/path/to/venv/bin/python" >&2; \
	    exit 2; \
	 }

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
# The native copy is built by a PLAIN `make`
# -- the default build, the one that ships -- so every test handed
# CELLDIAG_NATIVE_BIN measures exactly what the default produces, and a default
# that stopped being native fails the pair's own polarity check. The baseline,
# now the dev/test oracle's build, is the explicit NATIVE=0.
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
# The copies live ONE level below the helper's own directory, like
# build-baseline/. The helper finds its decoder by walking up from its own
# location to the first .deps/diaggrok/src, at most HELPER_SCRIPT_MAX_UP levels,
# and only then falls back to the INSTALLED decoder. A copy under celltools/
# never reaches capture_cell_diag/.deps, so it would quietly decode with
# whatever `make install` left in the data dir rather than this tree's
# decoder, and the A/B would compare native against the wrong reference.
GATE_DIR ?= $(CELLDIAG_DIR)/build-replay-gate
ENTRY_MODE = $(GATE_DIR)/.entry_mode

# The A/B is the gate, and its fixtures live outside the tree, so without
# CELL_CAPTURES it would run 1 test of 60 and exit 0, and skips like that can
# hide red A/B tests indefinitely. So: refuse at once when the corpus is absent (replay-gate-corpus runs BEFORE the build; this
# makefile is .NOTPARALLEL so -j cannot start the build beside it and leave the
# tree mid-relink), and run the suite with CELL_CAPTURES_REQUIRED=1 so a single
# missing fixture fails the session (tests/corpus_skip_banner.py). CELL_CAPTURES
# may be exported or given as a make variable; make exports the latter.
.PHONY: replay-gate
replay-gate: replay-gate-corpus pytest-available replay-gate-build
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
#
# Two failure modes this is built against:
#
# * A failure mid-pair (a broken NATIVE=1 compile, a full disk) must still put
#   the tree back. The pair is its own sub-make, and its failure runs the
#   restore before the target exits non-zero.
# * A RERUN after an interrupted gate must not record the mode the interrupted
#   run left behind as the "entry" mode, or the restore faithfully puts the
#   tree into the wrong state. $(GATE_MARK) exists from the moment the entry
#   mode is recorded until a restore succeeds. While it exists, the recorded
#   entry mode is the true one and is kept.
#
# Each copy lands under a temporary name and is RENAMED over the old one. A
# copy written in place fails with "Text file busy" while a replay is
# executing the previous copy; a rename replaces the directory entry and
# leaves the running inode alone.
GATE_MARK = $(GATE_DIR)/.restore_pending
SUBMAKE = $(MAKE) --no-print-directory -f $(lastword $(MAKEFILE_LIST)) \
	CELLDIAG_DIR=$(CELLDIAG_DIR) GATE_DIR=$(GATE_DIR)

.PHONY: replay-gate-build
replay-gate-build:
	@mkdir -p $(GATE_DIR)
	@if [ -f $(GATE_MARK) ] && [ -f $(ENTRY_MODE) ]; then \
	    echo "  replay-gate: an earlier gate did not restore; keeping its entry mode"; \
	 elif [ -f $(CELLDIAG_DIR)/.native_mode ]; then \
	    cat $(CELLDIAG_DIR)/.native_mode > $(ENTRY_MODE); \
	 else \
	    echo unbuilt > $(ENTRY_MODE); \
	 fi
	@touch $(GATE_MARK)
	@echo "  replay-gate: tree entered in mode '$$(cat $(ENTRY_MODE))'"
	@$(SUBMAKE) replay-gate-pair || { \
	    echo "*** replay-gate: building the pair FAILED -- restoring the tree" >&2; \
	    $(SUBMAKE) replay-gate-restore; \
	    exit 1; \
	 }
	@$(SUBMAKE) replay-gate-restore
	@echo
	@echo "built both configurations into $(GATE_DIR):"
	@echo "  .native    = make           (the default: carved C++ legs linked)"
	@echo "  .baseline  = make NATIVE=0  (pure C, Python bridge only; dev/test)"
	@echo

# The two builds and their copies, nothing else: replay-gate-build owns the
# entry mode and the restore around it. The native copy names no NATIVE= on
# purpose: it is whatever the default build is. If that is ever not native (an
# exported NATIVE=0, or a default set back to the baseline), the replay harness's polarity
# check fails on the copy rather than comparing two baselines.
.PHONY: replay-gate-pair
replay-gate-pair:
	$(MAKE) -C $(CELLDIAG_DIR)
	cp $(CELLDIAG_DIR)/kismet_cap_cell_diag $(GATE_DIR)/.kismet_cap_cell_diag.native.tmp
	mv -f $(GATE_DIR)/.kismet_cap_cell_diag.native.tmp $(GATE_DIR)/kismet_cap_cell_diag.native
	$(MAKE) -C $(CELLDIAG_DIR) NATIVE=0
	cp $(CELLDIAG_DIR)/kismet_cap_cell_diag $(GATE_DIR)/.kismet_cap_cell_diag.baseline.tmp
	mv -f $(GATE_DIR)/.kismet_cap_cell_diag.baseline.tmp $(GATE_DIR)/kismet_cap_cell_diag.baseline

# Puts $(CELLDIAG_DIR) back into the mode recorded in $(ENTRY_MODE). Safe to run
# by hand after an interrupted gate. Only a successful restore clears
# $(GATE_MARK); a failed one leaves it, so the next gate keeps the true entry
# mode. `unbuilt` REMOVES the artifacts rather than
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
	     echo "  replay-gate: restoring tree to baseline (NATIVE=0)"; \
	     $(MAKE) --no-print-directory -C $(CELLDIAG_DIR) NATIVE=0 ;; \
	   unbuilt) \
	     echo "  replay-gate: tree had no build on entry -- removing artifacts"; \
	     rm -f $(CELLDIAG_DIR)/kismet_cap_cell_diag $(CELLDIAG_DIR)/.native_mode ;; \
	   *) \
	     echo "*** ERROR: unrecognised entry mode '$$mode' in $(ENTRY_MODE);" >&2; \
	     echo "*** refusing to guess -- put the tree back by hand ***" >&2; \
	     exit 1 ;; \
	 esac
	@rm -f $(GATE_MARK)

.PHONY: replay-gate-clean
replay-gate-clean:
	rm -rf $(GATE_DIR)
