// Native-core unit tests: evaluator semantics (the quirks that MUST mirror
// spec_sim._eval) and the obligation kernel on the multiplier derivation,
// including the two non-vacuous negative controls. Zero-dependency assert
// runner; the cross-LANGUAGE parity (C++ vs the Python reference) is pinned
// separately by tests/test_native_kernel.py's differential fuzz.

#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include "rtlcore/expr.hpp"
#include "rtlcore/obligations.hpp"

using namespace rtlcore;

static int failures = 0;

#define CHECK(cond)                                                      \
    do {                                                                 \
        if (!(cond)) {                                                   \
            std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, \
                         #cond);                                         \
            ++failures;                                                  \
        }                                                                \
    } while (0)

// --- helpers ---------------------------------------------------------------

static Value ev(const std::string& expr,
                const std::vector<std::pair<std::string, std::uint64_t>>& binds =
                    {}) {
    SymTab syms;
    const Expr e = compile_expr(expr, syms);
    Env env(syms.size());
    for (const auto& [name, v] : binds) {
        const auto it = syms.index.find(name);
        if (it != syms.index.end())
            env.scalars[static_cast<size_t>(it->second)] = Value::of(v);
    }
    return eval(e, env);
}

static bool is_int(const Value& v, std::uint64_t n) { return !v.x && v.v == n; }
static bool is_x(const Value& v) { return v.x; }

// --- evaluator semantics ----------------------------------------------------

static void test_eval_basics() {
    CHECK(is_int(ev("1 + 2"), 3));
    CHECK(is_int(ev("TRUE"), 1));
    CHECK(is_int(ev("FALSE"), 0));
    CHECK(is_int(ev("2 * 3 + 4"), 10));
    CHECK(is_int(ev("2 + 3 * 4"), 14));
    CHECK(is_int(ev("(2 + 3) * 4"), 20));
    CHECK(is_int(ev("7 / 2"), 3));
    CHECK(is_int(ev("7 % 2"), 1));
    CHECK(is_int(ev("7 mod 2"), 1));  // word op
}

static void test_eval_u32_wraparound() {
    // count - 1 at count==0 wraps to all-ones (Verilog integer context).
    CHECK(is_int(ev("count - 1", {{"count", 0}}), 4294967295ull));
    CHECK(is_int(ev("0 - 1"), 4294967295ull));
    CHECK(is_int(ev("0 - 1 < 5"), 0));  // wrapped, so NOT less-than
    CHECK(is_int(ev("-1"), 4294967295ull));
    CHECK(is_int(ev("65535 * 65537"), 4294967295ull));
    CHECK(is_int(ev("65536 * 65536"), 0));  // 2^32 masked
}

static void test_eval_x_propagation() {
    CHECK(is_x(ev("missing + 1")));
    CHECK(is_x(ev("missing")));
    // PESSIMISTIC X: X \/ TRUE is X here (unlike Verilog's x|1 = 1).
    CHECK(is_x(ev("missing \\/ TRUE")));
    CHECK(is_x(ev("missing /\\ FALSE")));
    CHECK(is_x(ev("IF missing THEN 1 ELSE 2")));
    CHECK(is_x(ev("5 / 0")));   // div by zero -> X
    CHECK(is_x(ev("5 % 0")));   // mod by zero -> X
    CHECK(is_x(ev("~missing")));
    CHECK(is_int(ev("IF TRUE THEN 1 ELSE missing"), 1));  // lazy taken branch
}

static void test_eval_logic_and_cmp() {
    CHECK(is_int(ev("1 = 1"), 1));
    CHECK(is_int(ev("1 /= 1"), 0));
    CHECK(is_int(ev("2 <= 2"), 1));
    CHECK(is_int(ev("2 >= 3"), 0));
    CHECK(is_int(ev("a AND b", {{"a", 1}, {"b", 0}}), 0));  // word ops
    CHECK(is_int(ev("a OR b", {{"a", 1}, {"b", 0}}), 1));
    CHECK(is_int(ev("NOT a", {{"a", 0}}), 1));
    CHECK(is_int(ev("~0"), 1));
    CHECK(is_int(ev("!5"), 0));
    CHECK(is_int(ev("5 \\/ 0"), 1));  // nonzero is truthy
}

static void test_eval_if_chains() {
    // The multiplier's hardened handshake chain shape.
    const std::string chain =
        "IF (state = 0 OR state = 2) AND start = 1 THEN 1 "
        "ELSE IF state = 1 AND count = 1 THEN 2 "
        "ELSE IF state = 1 THEN 1 "
        "ELSE IF state = 2 THEN 0 ELSE 0";
    CHECK(is_int(ev(chain, {{"state", 0}, {"start", 1}, {"count", 5}}), 1));
    CHECK(is_int(ev(chain, {{"state", 2}, {"start", 1}, {"count", 0}}), 1));
    CHECK(is_int(ev(chain, {{"state", 1}, {"start", 0}, {"count", 1}}), 2));
    CHECK(is_int(ev(chain, {{"state", 1}, {"start", 0}, {"count", 4}}), 1));
    CHECK(is_int(ev(chain, {{"state", 2}, {"start", 0}, {"count", 0}}), 0));
    // Shift-add body conditional.
    CHECK(is_int(ev("IF (mplier % 2) = 1 THEN product + mcand ELSE product",
                    {{"mplier", 3}, {"product", 10}, {"mcand", 7}}),
                 17));
}

static void test_eval_tolerances() {
    CHECK(is_int(ev("1 + 2 zzz"), 3));      // trailing tokens ignored
    CHECK(is_int(ev("a {,} + 1", {{"a", 2}}), 3));  // unknown chars skipped
    CHECK(is_int(ev("count' + 1", {{"count", 4}}), 5));  // prime dropped
    bool threw = false;
    try {
        ev("1 +");  // truncated -> out_of_range (Python: IndexError)
    } catch (const std::out_of_range&) {
        threw = true;
    }
    CHECK(threw);
}

static void test_eval_indexed() {
    SymTab syms;
    const Expr e = compile_expr("m[i]", syms);
    Env env(syms.size());
    std::vector<Value> mem{Value::of(11), Value::X(), Value::of(33)};
    env.arrays[static_cast<size_t>(syms.index.at("m"))] = &mem;
    env.scalars[static_cast<size_t>(syms.index.at("i"))] = Value::of(2);
    CHECK(is_int(eval(e, env), 33));
    env.scalars[static_cast<size_t>(syms.index.at("i"))] = Value::of(1);
    CHECK(is_x(eval(e, env)));  // X cell
    env.scalars[static_cast<size_t>(syms.index.at("i"))] = Value::of(9);
    CHECK(is_x(eval(e, env)));  // out of range
    CHECK(is_x(ev("m[0]")));    // absent array -> X
    // bound SCALAR indexed -> TypeError-equivalent
    bool threw = false;
    try {
        ev("s[0]", {{"s", 5}});
    } catch (const ScalarIndexError&) {
        threw = true;
    }
    CHECK(threw);
}

// --- obligation kernel -------------------------------------------------------

static LoopParams multiplier_params(int width) {
    LoopParams p;
    p.post = "product = a * b";
    p.invariant = "product + mplier * mcand = a * b";
    p.variant = "count";
    p.guard = "count > 0";
    p.init = {{"product", "0"}, {"mcand", "a"}, {"mplier", "b"}, {"count", "8"}};
    p.body = {{"product",
               "IF (mplier % 2) = 1 THEN product + mcand ELSE product"},
              {"mcand", "mcand * 2"},
              {"mplier", "mplier / 2"},
              {"count", "count - 1"}};
    p.mapping = {{"product", "product"}};
    p.input_widths = {{"a", width}, {"b", width}};
    return p;
}

static void test_kernel_multiplier_exhaustive() {
    const DischargeResult r = discharge_loop_obligations(multiplier_params(6));
    CHECK(r.ok);
    CHECK(r.mode == "exhaustive-proof");
    CHECK(r.cases_checked == 4096);
    CHECK(r.o1 && r.o2 && r.o3);
    CHECK(!r.cex.has_value());
}

static void test_kernel_negative_dropped_shift() {
    // Non-vacuous control: dropping the mcand shift breaks O2 with a concrete
    // counterexample.
    LoopParams p = multiplier_params(6);
    p.body[1] = {"mcand", "mcand"};  // dropped shift
    const DischargeResult r = discharge_loop_obligations(p);
    CHECK(!r.ok);
    CHECK(r.o1);
    CHECK(!r.o2);
    CHECK(!r.o3);
    CHECK(r.cex.has_value());
    CHECK(r.cex->obligation == "O2");
    CHECK(r.cex->detail == "body does not maintain the invariant");
}

static void test_kernel_negative_wrong_invariant() {
    LoopParams p = multiplier_params(6);
    p.invariant = "product + mplier * mcand = a + b";  // wrong
    const DischargeResult r = discharge_loop_obligations(p);
    CHECK(!r.ok);
    CHECK(!r.o1 && !r.o2 && !r.o3);
    CHECK(r.cex.has_value());
    CHECK(r.cex->obligation == "O1");
    CHECK(r.cex->detail == "invariant does not hold after init");
}

static void test_kernel_termination_failure() {
    LoopParams p = multiplier_params(4);
    p.init = {{"product", "0"}, {"mcand", "a"}, {"mplier", "b"}, {"count", "100"}};
    p.invariant = "TRUE";
    p.post = "TRUE";
    const DischargeResult r = discharge_loop_obligations(p);
    CHECK(!r.ok);
    CHECK(r.cex.has_value());
    CHECK(r.cex->obligation == "O2");
    CHECK(r.cex->detail == "guard still holds after max_iters=64");
}

static void test_kernel_sampled_mode() {
    LoopParams p = multiplier_params(16);  // 2^32 inputs >> threshold
    p.init[3] = {"count", "16"};  // 16-bit operands need 16 shift-add steps
    const DischargeResult r = discharge_loop_obligations(p);
    CHECK(r.ok);
    CHECK(r.mode == "sampled");
    // edge cross-product (3x3) + 256 LCG samples, deduplicated
    CHECK(r.cases_checked > 9 && r.cases_checked <= 265);
}

static void test_kernel_sampled_underiterated() {
    // An 8-step loop CANNOT multiply 16-bit operands: mplier is not exhausted
    // at exit, so O3 fails (first on a=1, b=65535 in the edge battery). The
    // sampled battery falsifying this is the kernel's non-vacuity at work.
    const DischargeResult r = discharge_loop_obligations(multiplier_params(16));
    CHECK(!r.ok);
    CHECK(r.mode == "sampled");
    CHECK(r.o1 && r.o2 && !r.o3);
    CHECK(r.cex.has_value());
    CHECK(r.cex->obligation == "O3");
    CHECK(r.cex->detail == "postcondition does not hold at loop exit");
}

static void test_kernel_empty_inputs() {
    LoopParams p;
    p.post = "x = 8";
    p.invariant = "x + count = 8";
    p.variant = "count";
    p.guard = "count > 0";
    p.init = {{"x", "0"}, {"count", "8"}};
    p.body = {{"x", "x + 1"}, {"count", "count - 1"}};
    p.mapping = {{"x", "x"}};
    const DischargeResult r = discharge_loop_obligations(p);
    CHECK(r.ok);
    CHECK(r.mode == "exhaustive-proof");
    CHECK(r.cases_checked == 1);
}

int main() {
    test_eval_basics();
    test_eval_u32_wraparound();
    test_eval_x_propagation();
    test_eval_logic_and_cmp();
    test_eval_if_chains();
    test_eval_tolerances();
    test_eval_indexed();
    test_kernel_multiplier_exhaustive();
    test_kernel_negative_dropped_shift();
    test_kernel_negative_wrong_invariant();
    test_kernel_termination_failure();
    test_kernel_sampled_mode();
    test_kernel_sampled_underiterated();
    test_kernel_empty_inputs();

    if (failures) {
        std::fprintf(stderr, "%d check(s) FAILED\n", failures);
        return 1;
    }
    std::printf("all native-core checks passed\n");
    return 0;
}
