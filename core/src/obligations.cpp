// Native obligation kernel — mirrors pipeline/refinement/obligations.py
// pass-for-pass: the same three obligation sweeps in the same order (all
// valuations through O1, then O2, then O3), the same domain enumeration
// (lexicographic cartesian product over sorted input names; the same sampled
// battery: edge cross-product + the same LCG spread, deduplicated in order),
// the same read-before-write body step, and byte-identical counterexamples.
//
// See obligations.hpp for the backend-parity contract.

#include "rtlcore/obligations.hpp"

#include <algorithm>
#include <set>
#include <stdexcept>

namespace rtlcore {

namespace {

// ---------------------------------------------------------------------------
// Compiled derivation: every expression parsed once against one symbol table.
// ---------------------------------------------------------------------------

struct Compiled {
    SymTab syms;
    // Slots are interned in Python env-dict insertion order: sorted input names
    // first, then init keys, then body keys, then mapping keys. Free variables
    // referenced only inside expressions intern AFTER these, and are never
    // marked present, so counterexample snapshots match Python's dict order.
    std::vector<std::pair<int, std::uint64_t>> inputs;  // slot, domain size
    std::vector<std::string> input_names;               // sorted
    std::vector<std::pair<int, Expr>> init;             // sequential, in order
    std::vector<std::pair<int, Expr>> body;             // simultaneous
    std::vector<std::pair<int, Expr>> mapping;          // bound at loop exit
    Expr post, invariant, variant, guard;
    int nslots = 0;
};

Compiled compile(const LoopParams& p) {
    Compiled c;

    // names = sorted(input_widths)  — deterministic order
    std::vector<std::pair<std::string, int>> widths = p.input_widths;
    std::sort(widths.begin(), widths.end(),
              [](const auto& a, const auto& b) { return a.first < b.first; });
    for (const auto& [name, w] : widths) {
        if (w < 0 || w > 63)
            throw std::invalid_argument("input width out of range: " + name);
        c.inputs.emplace_back(c.syms.intern(name), 1ull << w);
        c.input_names.push_back(name);
    }
    // Reserve slots in env insertion order BEFORE compiling any expression.
    for (const auto& [var, expr] : p.init) {
        (void)expr;
        c.syms.intern(var);
    }
    for (const auto& [var, expr] : p.body) {
        (void)expr;
        c.syms.intern(var);
    }
    for (const auto& [var, expr] : p.mapping) {
        (void)expr;
        c.syms.intern(var);
    }

    for (const auto& [var, expr] : p.init)
        c.init.emplace_back(c.syms.intern(var), compile_expr(expr, c.syms));
    for (const auto& [var, expr] : p.body)
        c.body.emplace_back(c.syms.intern(var), compile_expr(expr, c.syms));
    for (const auto& [var, expr] : p.mapping)
        c.mapping.emplace_back(c.syms.intern(var), compile_expr(expr, c.syms));
    c.post = compile_expr(p.post, c.syms);
    c.invariant = compile_expr(p.invariant, c.syms);
    c.variant = compile_expr(p.variant, c.syms);
    c.guard = compile_expr(p.guard, c.syms);

    c.nslots = c.syms.size();
    return c;
}

// ---------------------------------------------------------------------------
// Input domain — mirrors _input_domain.
// ---------------------------------------------------------------------------

// Saturating product of the domain sizes; exact while <= threshold (which is
// all that is compared — Python computes the unbounded product).
std::uint64_t total_cases(const Compiled& c, std::uint64_t threshold) {
    std::uint64_t total = 1;
    for (const auto& [slot, size] : c.inputs) {
        (void)slot;
        if (size != 0 && total > threshold / size + 1) return UINT64_MAX;
        total *= size;
        if (total > threshold) return total;  // already over; exactness moot
    }
    return total;
}

// The sampled battery: edge cross-product (0/1/max per input, nested-build
// order) plus the deterministic LCG spread, deduplicated keeping first
// occurrence — value-for-value identical to the Python battery.
std::vector<std::vector<std::uint64_t>> sampled_battery(const Compiled& c) {
    const size_t n = c.inputs.size();

    std::vector<std::vector<std::uint64_t>> edge_sets;
    for (const auto& [slot, size] : c.inputs) {
        (void)slot;
        const std::uint64_t mx = size - 1;
        std::set<std::uint64_t> edges{0};
        if (1 < size) edges.insert(1);
        edges.insert(mx);
        edge_sets.emplace_back(edges.begin(), edges.end());  // sorted
    }
    // Cross-product in Python's nested list-build order (first input slowest).
    std::vector<std::vector<std::uint64_t>> sampled{{}};
    for (const auto& edges : edge_sets) {
        std::vector<std::vector<std::uint64_t>> next;
        next.reserve(sampled.size() * edges.size());
        for (const auto& prev : sampled)
            for (const auto& e : edges) {
                auto row = prev;
                row.push_back(e);
                next.push_back(std::move(row));
            }
        sampled = std::move(next);
    }
    // Deterministic pseudo-random spread (the exact Python LCG mix).
    constexpr std::uint64_t SAMPLES = 256;
    for (std::uint64_t i = 0; i < SAMPLES; ++i) {
        std::vector<std::uint64_t> row(n);
        for (size_t j = 0; j < n; ++j) {
            const std::uint64_t mixed =
                (i * (37 + 7 * j) + 11 + 3 * j) * (1 + j);
            row[j] = mixed % c.inputs[j].second;
        }
        sampled.push_back(std::move(row));
    }
    // De-duplicate, keeping first occurrence (deterministic order).
    std::set<std::vector<std::uint64_t>> seen;
    std::vector<std::vector<std::uint64_t>> uniq;
    uniq.reserve(sampled.size());
    for (auto& row : sampled)
        if (seen.insert(row).second) uniq.push_back(std::move(row));
    return uniq;
}

// ---------------------------------------------------------------------------
// Walker state: an Env plus per-slot "present in the Python env dict" flags so
// counterexample snapshots list exactly the keys Python's dict would hold.
// ---------------------------------------------------------------------------

struct Walker {
    const Compiled& c;
    Env env;
    std::vector<bool> present;
    // step_body scratch (no allocation in the hot loop)
    std::vector<Value> scratch;
    // O3 mapping-binding scratch
    std::vector<Value> map_scratch;
    // pre-state buffers: a flat copy per O2 iteration (cheap), expanded into a
    // (name, value) snapshot only on failure
    std::vector<Value> pre_scalars;
    std::vector<bool> pre_present;

    explicit Walker(const Compiled& comp)
        : c(comp),
          env(comp.nslots),
          present(static_cast<size_t>(comp.nslots), false),
          scratch(comp.body.size(), Value::X()),
          map_scratch(comp.mapping.size(), Value::X()),
          pre_scalars(static_cast<size_t>(comp.nslots), Value::X()),
          pre_present(static_cast<size_t>(comp.nslots), false) {}

    void save_pre() {
        pre_scalars = env.scalars;   // same capacity: element-wise copy, no alloc
        pre_present = present;
    }

    void load_case(const std::vector<std::uint64_t>& vals) {
        std::fill(env.scalars.begin(), env.scalars.end(), Value::X());
        std::fill(present.begin(), present.end(), false);
        for (size_t k = 0; k < c.inputs.size(); ++k) {
            env.scalars[static_cast<size_t>(c.inputs[k].first)] =
                Value::of(vals[k]);
            present[static_cast<size_t>(c.inputs[k].first)] = true;
        }
        // init is SEQUENTIAL: each expression sees the inputs and every prior
        // init assignment (mirrors _apply_init).
        for (const auto& [slot, expr] : c.init) {
            env.scalars[static_cast<size_t>(slot)] = eval(expr, env);
            present[static_cast<size_t>(slot)] = true;
        }
    }

    // One simultaneous loop step: every RHS reads the PRE-state, all commits
    // land together (mirrors _step_body's read-before-write).
    void step_body() {
        for (size_t k = 0; k < c.body.size(); ++k)
            scratch[k] = eval(c.body[k].second, env);
        for (size_t k = 0; k < c.body.size(); ++k) {
            env.scalars[static_cast<size_t>(c.body[k].first)] = scratch[k];
            present[static_cast<size_t>(c.body[k].first)] = true;
        }
    }

    std::vector<std::pair<std::string, Value>> snapshot() const {
        return snapshot_of(env.scalars, present);
    }
    std::vector<std::pair<std::string, Value>> snapshot_pre() const {
        return snapshot_of(pre_scalars, pre_present);
    }

private:
    std::vector<std::pair<std::string, Value>> snapshot_of(
        const std::vector<Value>& scalars, const std::vector<bool>& pres) const {
        std::vector<std::pair<std::string, Value>> out;
        for (int s = 0; s < c.nslots; ++s)
            if (pres[static_cast<size_t>(s)])
                out.emplace_back(c.syms.names[static_cast<size_t>(s)],
                                 scalars[static_cast<size_t>(s)]);
        return out;
    }
};

std::vector<std::pair<std::string, std::uint64_t>> case_inputs(
    const Compiled& c, const std::vector<std::uint64_t>& vals) {
    std::vector<std::pair<std::string, std::uint64_t>> out;
    out.reserve(vals.size());
    for (size_t k = 0; k < vals.size(); ++k)
        out.emplace_back(c.input_names[k], vals[k]);
    return out;
}

std::string fmt_value(const Value& v) {
    return v.x ? "None" : std::to_string(v.v);  // Python f-string rendering
}

// ---------------------------------------------------------------------------
// Domain iteration: lexicographic odometer (first sorted name slowest), the
// exact order of Python's materialized cartesian product. `fn` returns false
// to stop early (a counterexample was recorded).
// ---------------------------------------------------------------------------

template <typename Fn>
void for_each_valuation(const Compiled& c,
                        const std::vector<std::vector<std::uint64_t>>* sampled,
                        Fn&& fn) {
    if (sampled != nullptr) {
        for (const auto& row : *sampled)
            if (!fn(row)) return;
        return;
    }
    const size_t n = c.inputs.size();
    std::vector<std::uint64_t> cur(n, 0);
    while (true) {
        if (!fn(cur)) return;
        // increment, last input fastest
        size_t k = n;
        while (k > 0) {
            --k;
            if (++cur[k] < c.inputs[k].second) break;
            cur[k] = 0;
            if (k == 0) return;  // wrapped the slowest digit: done
        }
        if (n == 0) return;  // single empty valuation
    }
}

// ---------------------------------------------------------------------------
// The three obligations — control flow mirrors _check_O1/_check_O2/_check_O3.
// ---------------------------------------------------------------------------

bool check_O1(const Compiled& c,
              const std::vector<std::vector<std::uint64_t>>* sampled,
              Walker& w, CounterExample& cex) {
    bool ok = true;
    for_each_valuation(c, sampled, [&](const std::vector<std::uint64_t>& vals) {
        w.load_case(vals);
        if (!eval(c.invariant, w.env).is_one()) {
            cex = CounterExample{"O1", "invariant does not hold after init",
                                 case_inputs(c, vals), w.snapshot()};
            ok = false;
            return false;
        }
        return true;
    });
    return ok;
}

bool check_O2(const Compiled& c,
              const std::vector<std::vector<std::uint64_t>>* sampled,
              Walker& w, int max_iters, CounterExample& cex) {
    bool ok = true;
    for_each_valuation(c, sampled, [&](const std::vector<std::uint64_t>& vals) {
        w.load_case(vals);
        if (!eval(c.invariant, w.env).is_one()) {
            cex = CounterExample{"O2", "invariant fails at loop entry",
                                 case_inputs(c, vals), w.snapshot()};
            ok = false;
            return false;
        }
        int iters = 0;
        while (eval(c.guard, w.env).is_one() && iters < max_iters) {
            const Value variant_before = eval(c.variant, w.env);
            // Python builds nxt, checks it, and reports failures with the PRE
            // state (`_portable(env)`, not nxt). We step in place, so save a
            // flat copy of the pre-state first; it becomes a (name, value)
            // snapshot only on failure.
            w.save_pre();
            w.step_body();
            if (!eval(c.invariant, w.env).is_one()) {
                cex = CounterExample{"O2", "body does not maintain the invariant",
                                     case_inputs(c, vals), w.snapshot_pre()};
                ok = false;
                return false;
            }
            const Value variant_after = eval(c.variant, w.env);
            if (variant_before.x || variant_after.x ||
                !(variant_after.v < variant_before.v)) {
                cex = CounterExample{
                    "O2",
                    "variant did not strictly decrease (" +
                        fmt_value(variant_before) + " -> " +
                        fmt_value(variant_after) + ")",
                    case_inputs(c, vals), w.snapshot_pre()};
                ok = false;
                return false;
            }
            ++iters;
        }
        if (iters >= max_iters && eval(c.guard, w.env).is_one()) {
            cex = CounterExample{
                "O2",
                "guard still holds after max_iters=" + std::to_string(max_iters),
                case_inputs(c, vals), w.snapshot()};
            ok = false;
            return false;
        }
        return true;
    });
    return ok;
}

bool check_O3(const Compiled& c,
              const std::vector<std::vector<std::uint64_t>>* sampled,
              Walker& w, int max_iters, CounterExample& cex) {
    bool ok = true;
    for_each_valuation(c, sampled, [&](const std::vector<std::uint64_t>& vals) {
        w.load_case(vals);
        int iters = 0;
        while (eval(c.guard, w.env).is_one() && iters < max_iters) {
            w.step_body();
            ++iters;
        }
        // At exit: bind abstract variables via the mapping — every mapping
        // expression evaluates against the EXIT env (not against earlier
        // bindings), mirroring `post_env[a] = _eval(expr, env)`.
        for (size_t k = 0; k < c.mapping.size(); ++k)
            w.map_scratch[k] = eval(c.mapping[k].second, w.env);
        for (size_t k = 0; k < c.mapping.size(); ++k) {
            w.env.scalars[static_cast<size_t>(c.mapping[k].first)] =
                w.map_scratch[k];
            w.present[static_cast<size_t>(c.mapping[k].first)] = true;
        }
        if (!eval(c.post, w.env).is_one()) {
            cex = CounterExample{"O3", "postcondition does not hold at loop exit",
                                 case_inputs(c, vals), w.snapshot()};
            ok = false;
            return false;
        }
        return true;
    });
    return ok;
}

}  // namespace

// ---------------------------------------------------------------------------
// Public entry point — mirrors discharge_loop_obligations' result envelope:
// O1 fail -> {F,F,F}; O2 fail -> {T,F,F}; O3 fail -> {T,T,F}; ok -> {T,T,T};
// cases_checked is always the full domain size.
// ---------------------------------------------------------------------------

DischargeResult discharge_loop_obligations(const LoopParams& params) {
    const Compiled c = compile(params);

    const std::uint64_t total = total_cases(c, params.exhaustive_threshold);
    const bool exhaustive = total <= params.exhaustive_threshold;

    std::vector<std::vector<std::uint64_t>> battery;
    const std::vector<std::vector<std::uint64_t>>* sampled = nullptr;
    DischargeResult r;
    if (exhaustive) {
        r.mode = "exhaustive-proof";
        r.cases_checked = total;
    } else {
        battery = sampled_battery(c);
        sampled = &battery;
        r.mode = "sampled";
        r.cases_checked = battery.size();
    }

    Walker w(c);
    CounterExample cex;

    if (!check_O1(c, sampled, w, cex)) {
        r.ok = false;
        r.o1 = r.o2 = r.o3 = false;
        r.cex = std::move(cex);
        return r;
    }
    if (!check_O2(c, sampled, w, params.max_iters, cex)) {
        r.ok = false;
        r.o1 = true;
        r.o2 = r.o3 = false;
        r.cex = std::move(cex);
        return r;
    }
    if (!check_O3(c, sampled, w, params.max_iters, cex)) {
        r.ok = false;
        r.o1 = r.o2 = true;
        r.o3 = false;
        r.cex = std::move(cex);
        return r;
    }
    r.ok = true;
    r.o1 = r.o2 = r.o3 = true;
    return r;
}

}  // namespace rtlcore
