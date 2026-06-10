// rtlcore — native spec-simulator cycle engine.
//
// An EXACT-ROW mirror of pipeline/cocotb/spec_sim.py's SpecSimulator cycle
// loop. The DIVISION OF LABOUR is load-bearing:
//
//   * Python keeps the ONE-TIME composition — SpecSimulator.__init__ reuses
//     the bridge's _compose_clocked_actions/_action_update_exprs (the same
//     functions that feed Compiler 2), pre-parses indexed LHSes, pre-filters
//     reset targets, and pre-coerces stimulus values. That composition is the
//     shared semantics and STAYS the reference.
//   * C++ takes the PER-EDGE loop — compile every composed expression once,
//     then run the reset pulse + one rising edge per vector natively:
//     input drive (hold when not re-driven), combinational fixpoint settle,
//     reset-branch vs read-before-write clocked commits, memory writes
//     (X index skipped, out-of-range silently dropped, value masked to the
//     base width), width-masked register commits, and per-vector output rows
//     that OMIT X (a cocotb don't-assert).
//
// Mirror contract (pinned by tests/test_native_specsim.py): for identical
// composed inputs the native rows are EXACTLY the Python rows — every fixture
// design's proven trace and a randomized-stimulus differential fuzz. Stage 4
// artifacts must not depend on which backend derived the golden vectors.

#pragma once

#include <string>
#include <utility>
#include <vector>

#include "rtlcore/expr.hpp"

namespace rtlcore {

// One composed clocked update, pre-parsed by Python (_INDEXED_LHS_RE):
// a scalar register commit when idx_expr is empty, else base[idx] <= rhs.
struct UpdateSpec {
    std::string base;
    std::string idx_expr;  // "" = scalar target
    std::string rhs;
};

struct SimSpec {
    // Declared variable widths (commit masks); inputs are NOT masked, like the
    // Python simulator. width <= 0 means no mask.
    std::vector<std::pair<std::string, int>> widths;
    // Memory name -> depth (cells start X; X-until-written).
    std::vector<std::pair<std::string, int>> depths;
    // Composed clocked next-state, in composition order. All RHS/idx evaluate
    // against the PRE-edge state; scalars commit first (in order, last wins,
    // like Python's dict update), then memory writes (in order).
    std::vector<UpdateSpec> clocked;
    // Combinational definitions in order; settled to a bounded fixpoint
    // (len+1 sweeps, in-place sequential commits, like _recompute_comb).
    std::vector<std::pair<std::string, std::string>> comb;
    // Reset-branch updates, PRE-FILTERED by Python to scalar register targets
    // (memory and unlisted registers hold across reset).
    std::vector<std::pair<std::string, std::string>> reset;
};

// One rising edge, pre-coerced by Python. `inputs` carries only driveable
// names (clk and the reset port are stripped); an input absent from this edge
// HOLDS its previous value.
struct EdgeIn {
    std::vector<std::pair<std::string, Value>> inputs;
    bool is_reset = false;
    bool observe = false;  // emit an output row after this edge settles
};

// (port, value) pairs for the observed non-X outputs of one vector.
using Row = std::vector<std::pair<std::string, Value>>;

// Run the cycle loop. Throws ScalarIndexError if an indexed write targets a
// bound scalar (Python: len(int) TypeError — mirrored in kind).
std::vector<Row> run_spec_sim(const SimSpec& spec,
                              const std::vector<EdgeIn>& edges,
                              const std::vector<std::string>& output_ports);

}  // namespace rtlcore
