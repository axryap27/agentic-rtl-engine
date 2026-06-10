// rtlcore — native verification core for the agentic-rtl-engine.
//
// This header defines the compiled expression evaluator. It is an EXACT-SEMANTICS
// mirror of pipeline/cocotb/spec_sim.py's `_tokenize` + `_Interp` + `_eval` — the
// reference evaluator the obligation kernel discharges proofs against. The mirror
// contract (held by tests/test_native_kernel.py's differential fuzz) is:
//
//   * values are unsigned integers or X (Python: int or None); X propagates
//     PESSIMISTICALLY through every operator (X \/ TRUE = X, unlike Verilog)
//   * every arithmetic RESULT is masked to 32 bits unsigned (Verilog integer
//     context): count - 1 at count==0 wraps to 4294967295, never goes negative
//   * div/mod by zero yield X (no exception)
//   * comparisons yield 1/0; TRUE/FALSE are 1/0; logical not is (v == 0)
//   * word operators AND / OR / NOT / mod are accepted alongside /\ \/ ~ %
//   * the parser is TOLERANT exactly where Python's is: unrecognised characters
//     are skipped, a stray prime ' is dropped, THEN/ELSE/)/] are optional,
//     trailing tokens after a complete expression are ignored, and a TRUNCATED
//     expression throws std::out_of_range (pybind11 maps it to IndexError, the
//     same exception Python's _take() raises)
//
// Documented divergences (never exercised by the pipeline; see core/README.md):
//   * digits/identifiers are ASCII (Python \d / \w also match unicode)
//   * integer literals are parsed into 64 bits (saturating); Python is unbounded
//   * division is exact integer division; Python computes int(v / r) through a
//     double, which only differs for operand magnitudes >= 2^53 (only reachable
//     via absurd literals — all masked values are < 2^32)
//   * IF evaluates only the taken branch (Python evaluates both); observable only
//     through the indexed-scalar TypeError below, since evaluation is otherwise
//     total
//   * indexing a BOUND SCALAR throws TypeError (Python: len(int) TypeError) —
//     mirrored in kind, not message

#pragma once

#include <cstdint>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

namespace rtlcore {

// ---------------------------------------------------------------------------
// Values: an unsigned integer or X (undefined), mirroring "int or None".
// ---------------------------------------------------------------------------

struct Value {
    std::uint64_t v = 0;
    bool x = true;  // X (undefined)

    static Value X() { return Value{0, true}; }
    static Value of(std::uint64_t n) { return Value{n, false}; }
    bool is_one() const { return !x && v == 1; }
    bool operator==(const Value& o) const {
        return x == o.x && (x || v == o.v);
    }
};

// Mirrors Python's TypeError on `scalar[idx]` (len(int) raises). Translated to
// TypeError at the pybind11 boundary.
struct ScalarIndexError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

// ---------------------------------------------------------------------------
// Symbol table: identifier -> environment slot, interned in INSERTION order.
// The kernel relies on insertion order to reproduce Python's env-dict key order
// in counterexample snapshots.
// ---------------------------------------------------------------------------

struct SymTab {
    std::unordered_map<std::string, int> index;
    std::vector<std::string> names;  // slot -> name

    int intern(const std::string& s) {
        auto it = index.find(s);
        if (it != index.end()) return it->second;
        const int slot = static_cast<int>(names.size());
        index.emplace(s, slot);
        names.push_back(s);
        return slot;
    }
    int size() const { return static_cast<int>(names.size()); }
};

// ---------------------------------------------------------------------------
// Compiled expressions: parse ONCE into a flat node pool, evaluate many times.
// (Python re-tokenizes and re-parses on every _eval call — that is the single
// hot-loop cost this core removes.)
// ---------------------------------------------------------------------------

enum class Op : std::uint8_t {
    Const, Var, Index,            // literal / identifier / identifier[expr]
    If,                            // IF c THEN a ELSE b   (children c, a, b)
    Or, And,                       // \/ OR    /\ AND      (X-pessimistic)
    Eq, Ne, Le, Ge, Lt, Gt,        // = /= <= >= < >
    Add, Sub, Mul, Div, Mod,       // U32-masked; div/mod-by-zero -> X
    Not, Neg,                      // ~ ! NOT    unary -
};

struct Node {
    Op op;
    std::uint64_t lit = 0;  // Const
    int slot = -1;          // Var / Index base
    int a = -1, b = -1, c = -1;  // child node indices
};

struct Expr {
    std::vector<Node> nodes;
    int root = -1;
};

// ---------------------------------------------------------------------------
// Environment: one Value per slot; optional array (memory) per slot for
// eval_expr parity with spec_sim's list-valued state. The obligation kernel
// never binds arrays.
// ---------------------------------------------------------------------------

struct Env {
    std::vector<Value> scalars;
    std::vector<const std::vector<Value>*> arrays;  // non-owning; null = scalar

    explicit Env(int nslots)
        : scalars(static_cast<size_t>(nslots), Value::X()),
          arrays(static_cast<size_t>(nslots), nullptr) {}
};

// Tokenize + parse `expr`, interning identifiers into `syms`. Throws
// std::out_of_range on a truncated expression (mirrors Python's IndexError).
Expr compile_expr(const std::string& expr, SymTab& syms);

// Evaluate a compiled expression. Total except ScalarIndexError (see header
// comment); X propagates.
Value eval(const Expr& e, const Env& env);

}  // namespace rtlcore
