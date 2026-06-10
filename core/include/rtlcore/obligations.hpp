// rtlcore — native obligation kernel.
//
// An EXACT-VERDICT mirror of pipeline/refinement/obligations.py: the three
// Morgan/Back loop-introduction obligations (O1/O2/O3), discharged over the
// same input domain, in the same enumeration order, with the same honest
// `mode` ("exhaustive-proof" vs "sampled"), the same cases_checked count, the
// same per-obligation verdict envelope, and byte-identical counterexample
// detail strings. The verdicts MUST NOT depend on which backend ran — chain
// replay hashes the refinement audit, so any divergence would break replay.
// tests/test_native_kernel.py pins backend parity.

#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "rtlcore/expr.hpp"

namespace rtlcore {

// First failing case, mirroring the Python counterexample dict. `inputs` is in
// sorted-name order; `state` is in env-dict insertion order (inputs first, then
// init/body/mapping vars as first written). An X value renders as Python None.
struct CounterExample {
    std::string obligation;  // "O1" | "O2" | "O3"
    std::string detail;      // byte-identical to the Python detail string
    std::vector<std::pair<std::string, std::uint64_t>> inputs;
    std::vector<std::pair<std::string, Value>> state;
};

struct DischargeResult {
    bool ok = false;
    std::string mode;            // "exhaustive-proof" | "sampled"
    std::uint64_t cases_checked = 0;
    bool o1 = false, o2 = false, o3 = false;
    std::optional<CounterExample> cex;
};

// Inputs to a discharge. init/body/mapping preserve the caller's (Python dict)
// insertion order — init is applied SEQUENTIALLY in that order.
struct LoopParams {
    std::string post;
    std::string invariant;
    std::string variant;
    std::string guard;
    std::vector<std::pair<std::string, std::string>> init;
    std::vector<std::pair<std::string, std::string>> body;
    std::vector<std::pair<std::string, std::string>> mapping;
    std::vector<std::pair<std::string, int>> input_widths;
    std::uint64_t exhaustive_threshold = 65536;
    int max_iters = 64;
};

DischargeResult discharge_loop_obligations(const LoopParams& params);

}  // namespace rtlcore
