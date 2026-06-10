// Native spec-simulator cycle engine — mirrors SpecSimulator._edge/_recompute_
// comb/run step-for-step. See spec_sim.hpp for the division of labour and the
// mirror contract.

#include "rtlcore/spec_sim.hpp"

namespace rtlcore {

namespace {

struct CompiledSim {
    SymTab syms;

    // widths: per-slot commit mask (-1 = none, mirrors _mask's width<=0 skip)
    std::vector<int> width;

    // memory storage; arrays[] pointers into `mem` are installed after all
    // compilation (no reallocation hazard).
    std::vector<std::vector<Value>> mem;
    std::vector<int> mem_slot;  // slot of each memory, aligned with `mem`

    struct ScalarUpd { int slot; int w; Expr rhs; };
    // mem_index resolves the base into `mem` (-1: base is not a declared
    // memory — Python then either skips (state None) or TypeErrors (scalar)).
    struct MemUpd { int slot; int mem_index; int w; Expr idx; Expr rhs; };
    std::vector<ScalarUpd> clocked_scalars;  // composition order
    std::vector<MemUpd> clocked_mems;        // composition order
    std::vector<ScalarUpd> comb;             // definition order
    std::vector<ScalarUpd> reset;            // pre-filtered scalar reg targets

    std::vector<int> out_slots;              // aligned with output_ports
};

int width_of(const SimSpec& spec, const std::string& name) {
    for (const auto& [n, w] : spec.widths)
        if (n == name) return w;
    return -1;  // undeclared (e.g. a free input): no mask
}

CompiledSim compile_sim(const SimSpec& spec,
                        const std::vector<std::string>& output_ports) {
    CompiledSim c;

    for (const auto& u : spec.clocked) {
        const int slot = c.syms.intern(u.base);
        const int w = width_of(spec, u.base);
        if (u.idx_expr.empty()) {
            c.clocked_scalars.push_back({slot, w, compile_expr(u.rhs, c.syms)});
        } else {
            int mem_index = -1;
            for (size_t k = 0; k < spec.depths.size(); ++k)
                if (spec.depths[k].first == u.base)
                    mem_index = static_cast<int>(k);
            c.clocked_mems.push_back({slot, mem_index, w,
                                      compile_expr(u.idx_expr, c.syms),
                                      compile_expr(u.rhs, c.syms)});
        }
    }
    for (const auto& [var, expr] : spec.comb)
        c.comb.push_back({c.syms.intern(var), width_of(spec, var),
                          compile_expr(expr, c.syms)});
    for (const auto& [var, expr] : spec.reset)
        c.reset.push_back({c.syms.intern(var), width_of(spec, var),
                           compile_expr(expr, c.syms)});

    for (const auto& [name, depth] : spec.depths) {
        c.mem_slot.push_back(c.syms.intern(name));
        c.mem.emplace_back(static_cast<size_t>(depth > 0 ? depth : 0),
                           Value::X());
    }
    for (const auto& port : output_ports)
        c.out_slots.push_back(c.syms.intern(port));

    // per-slot width table (slots interned above; later env lookups only)
    c.width.assign(static_cast<size_t>(c.syms.size()), -1);
    for (const auto& [n, w] : spec.widths) {
        // Only slots actually referenced exist; an unreferenced declared
        // variable never participates, exactly like the Python dict state.
        auto it = c.syms.index.find(n);
        if (it != c.syms.index.end())
            c.width[static_cast<size_t>(it->second)] = w;
    }
    return c;
}

Value masked(Value v, int w) {
    // Mirrors _mask: X passes through; width<=0 means no mask.
    if (v.x || w <= 0) return v;
    if (w >= 64) return v;
    v.v &= (1ull << w) - 1;
    return v;
}

class Runner {
public:
    Runner(CompiledSim&& compiled)
        : c(std::move(compiled)), env(c.syms.size()) {
        for (size_t k = 0; k < c.mem.size(); ++k)
            env.arrays[static_cast<size_t>(c.mem_slot[k])] = &c.mem[k];
        scalar_buf.resize(c.clocked_scalars.size(), Value::X());
        mem_idx_buf.resize(c.clocked_mems.size(), Value::X());
        mem_val_buf.resize(c.clocked_mems.size(), Value::X());
        reset_buf.resize(c.reset.size(), Value::X());
    }

    void edge(const EdgeIn& in) {
        // 1. drive inputs (no masking — mirrors _set_inputs); undriven names
        //    hold their previous value.
        for (const auto& [name, v] : in.inputs) {
            auto it = c.syms.index.find(name);
            if (it != c.syms.index.end())
                env.scalars[static_cast<size_t>(it->second)] = v;
            // An input never referenced by any expression/output cannot affect
            // anything — Python stores it in the state dict; we can drop it.
        }
        // 2. settle combinational logic seen by the edge (OLD registers)
        recompute_comb();

        if (in.is_reset) {
            // Reset branch: only the (pre-filtered) scalar register targets
            // are driven; everything else holds. Evaluate all against the
            // pre-state, then commit (Python builds new_scalars, then update).
            for (size_t k = 0; k < c.reset.size(); ++k)
                reset_buf[k] = masked(eval(c.reset[k].rhs, env), c.reset[k].w);
            for (size_t k = 0; k < c.reset.size(); ++k)
                env.scalars[static_cast<size_t>(c.reset[k].slot)] = reset_buf[k];
        } else {
            // Normal branch: every RHS/idx reads the PRE-edge state
            // (nonblocking read-before-write); scalars commit first (in
            // order, duplicates last-wins), then memory writes (in order).
            for (size_t k = 0; k < c.clocked_scalars.size(); ++k)
                scalar_buf[k] =
                    masked(eval(c.clocked_scalars[k].rhs, env),
                           c.clocked_scalars[k].w);
            for (size_t k = 0; k < c.clocked_mems.size(); ++k) {
                mem_idx_buf[k] = eval(c.clocked_mems[k].idx, env);
                mem_val_buf[k] =
                    masked(eval(c.clocked_mems[k].rhs, env),
                           c.clocked_mems[k].w);
            }
            for (size_t k = 0; k < c.clocked_scalars.size(); ++k)
                env.scalars[static_cast<size_t>(c.clocked_scalars[k].slot)] =
                    scalar_buf[k];
            for (size_t k = 0; k < c.clocked_mems.size(); ++k) {
                const Value idx = mem_idx_buf[k];
                if (idx.x) continue;  // X index: write skipped
                const CompiledSim::MemUpd& u = c.clocked_mems[k];
                if (u.mem_index < 0) {
                    // Mirrors Python: an undeclared base (state None) is
                    // silently skipped; a bound SCALAR base is len(int) ->
                    // TypeError.
                    if (env.scalars[static_cast<size_t>(u.slot)].x) continue;
                    throw ScalarIndexError("indexed write to a scalar value");
                }
                auto& arr = c.mem[static_cast<size_t>(u.mem_index)];
                if (idx.v >= arr.size()) continue;  // out of range: dropped
                arr[static_cast<size_t>(idx.v)] = mem_val_buf[k];
            }
        }
        // 3. settle combinational outputs for observation
        recompute_comb();
    }

    Row observe() const {
        Row row;
        for (size_t k = 0; k < c.out_slots.size(); ++k) {
            const Value v = env.scalars[static_cast<size_t>(c.out_slots[k])];
            if (!v.x) row.emplace_back(out_name(k), v);
        }
        return row;
    }

    const std::string& out_name(size_t k) const {
        return c.syms.names[static_cast<size_t>(c.out_slots[k])];
    }

private:
    CompiledSim c;
    Env env;
    // hot-loop scratch (no allocation per edge)
    std::vector<Value> scalar_buf, mem_idx_buf, mem_val_buf, reset_buf;

    void recompute_comb() {
        // Bounded fixpoint, in-place sequential commits — exactly
        // _recompute_comb: len(comb)+1 sweeps, stop when a sweep changes
        // nothing (a cyclic comb loop simply stops without converging).
        const size_t sweeps = c.comb.size() + 1;
        for (size_t s = 0; s < sweeps; ++s) {
            bool changed = false;
            for (const auto& u : c.comb) {
                const Value nv = masked(eval(u.rhs, env), u.w);
                Value& cur = env.scalars[static_cast<size_t>(u.slot)];
                if (!(nv == cur)) {
                    cur = nv;
                    changed = true;
                }
            }
            if (!changed) break;
        }
    }
};

}  // namespace

std::vector<Row> run_spec_sim(const SimSpec& spec,
                              const std::vector<EdgeIn>& edges,
                              const std::vector<std::string>& output_ports) {
    Runner runner(compile_sim(spec, output_ports));
    std::vector<Row> rows;
    for (const auto& e : edges) {
        runner.edge(e);
        if (e.observe) rows.push_back(runner.observe());
    }
    return rows;
}

}  // namespace rtlcore
