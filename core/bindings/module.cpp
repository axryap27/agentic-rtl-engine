// pybind11 bindings for the rtlcore native verification core.
//
// Exposes exactly two functions:
//   eval_expr(expr, env)                  — differential-testing surface for the
//                                           compiled evaluator vs spec_sim._eval
//   discharge_loop_obligations(...)       — the native obligation kernel; the
//                                           Python wrapper in
//                                           pipeline/refinement/obligations.py
//                                           converts the returned dict into an
//                                           ObligationResult
//
// Exception parity: a truncated expression raises IndexError (std::out_of_range,
// pybind11's default mapping — the same exception Python's _take() raises);
// indexing a bound scalar raises TypeError (Python: len(int)).

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <utility>
#include <vector>

#include "rtlcore/expr.hpp"
#include "rtlcore/obligations.hpp"

namespace py = pybind11;
using namespace rtlcore;

namespace {

// ---------------------------------------------------------------------------
// Conversions
// ---------------------------------------------------------------------------

Value to_value(const py::handle& h) {
    if (h.is_none()) return Value::X();
    if (py::isinstance<py::bool_>(h)) return Value::of(h.cast<bool>() ? 1 : 0);
    if (py::isinstance<py::int_>(h)) {
        // The evaluator's domain is unsigned (every masked value is < 2^32 and
        // comparisons assume non-negative operands). Reject negatives loudly
        // rather than silently diverging from Python's signed comparison.
        const long long sv = h.cast<long long>();
        if (sv < 0)
            throw py::value_error(
                "rtlcore: negative env values are unsupported (the engine-spec "
                "domain is unsigned)");
        return Value::of(static_cast<std::uint64_t>(sv));
    }
    throw py::type_error("rtlcore: env values must be int, bool, None, or list");
}

py::object from_value(const Value& v) {
    if (v.x) return py::none();
    return py::int_(v.v);
}

std::vector<std::pair<std::string, std::string>> str_items(const py::dict& d) {
    // py::dict iteration follows Python dict insertion order — load-bearing:
    // init is applied sequentially in this order.
    std::vector<std::pair<std::string, std::string>> out;
    out.reserve(d.size());
    for (const auto& item : d)
        out.emplace_back(py::cast<std::string>(item.first),
                         py::cast<std::string>(py::str(item.second)));
    return out;
}

py::dict cex_to_dict(const CounterExample& cex) {
    py::dict inputs;
    for (const auto& [name, v] : cex.inputs) inputs[py::str(name)] = py::int_(v);
    py::dict state;
    for (const auto& [name, v] : cex.state) state[py::str(name)] = from_value(v);
    py::dict out;
    out["obligation"] = cex.obligation;
    out["inputs"] = inputs;
    out["state"] = state;
    out["detail"] = cex.detail;
    return out;
}

// ---------------------------------------------------------------------------
// eval_expr — parity surface
// ---------------------------------------------------------------------------

py::object eval_expr(const std::string& expr, const py::dict& env_dict) {
    SymTab syms;
    const Expr e = compile_expr(expr, syms);

    Env env(syms.size());
    // Owned storage for list-valued (memory) entries; Env::arrays is non-owning.
    std::vector<std::vector<Value>> owned_arrays;
    owned_arrays.reserve(env_dict.size());

    for (const auto& item : env_dict) {
        const auto name = py::cast<std::string>(item.first);
        const auto it = syms.index.find(name);
        if (it == syms.index.end()) continue;  // not referenced by this expr
        const py::handle val = item.second;
        if (py::isinstance<py::list>(val) || py::isinstance<py::tuple>(val)) {
            std::vector<Value> arr;
            for (const auto& el : py::cast<py::sequence>(val))
                arr.push_back(to_value(el));
            owned_arrays.push_back(std::move(arr));
            env.arrays[static_cast<size_t>(it->second)] = &owned_arrays.back();
            // NOTE: owned_arrays is reserve()d to env_dict.size() above, so
            // push_back never reallocates and the stored pointers stay valid.
        } else {
            env.scalars[static_cast<size_t>(it->second)] = to_value(val);
        }
    }
    return from_value(eval(e, env));
}

// ---------------------------------------------------------------------------
// discharge_loop_obligations — the kernel
// ---------------------------------------------------------------------------

py::dict discharge(const std::string& post, const std::string& invariant,
                   const std::string& variant, const std::string& guard,
                   const py::dict& init, const py::dict& body,
                   const py::dict& mapping, const py::dict& input_widths,
                   std::uint64_t exhaustive_threshold, int max_iters) {
    LoopParams p;
    p.post = post;
    p.invariant = invariant;
    p.variant = variant;
    p.guard = guard;
    p.init = str_items(init);
    p.body = str_items(body);
    p.mapping = str_items(mapping);
    for (const auto& item : input_widths)
        p.input_widths.emplace_back(py::cast<std::string>(item.first),
                                    py::cast<int>(item.second));
    p.exhaustive_threshold = exhaustive_threshold;
    p.max_iters = max_iters;

    DischargeResult r;
    {
        // Pure C++ from here on — release the GIL for the enumeration.
        py::gil_scoped_release release;
        r = discharge_loop_obligations(p);
    }

    py::dict obligations;
    obligations["O1"] = r.o1;
    obligations["O2"] = r.o2;
    obligations["O3"] = r.o3;
    py::dict out;
    out["ok"] = r.ok;
    out["mode"] = r.mode;
    out["cases_checked"] = py::int_(r.cases_checked);
    out["obligations"] = obligations;
    out["counterexample"] = r.cex ? py::object(cex_to_dict(*r.cex))
                                  : py::object(py::none());
    return out;
}

}  // namespace

PYBIND11_MODULE(_rtlcore, m) {
    m.doc() =
        "Native verification core: a compiled exact-semantics mirror of "
        "spec_sim._eval and the obligation kernel. Built from core/ via "
        "core/build.sh; pipeline/refinement/obligations.py dispatches to it "
        "when present and falls back to pure Python otherwise.";

    py::register_exception<ScalarIndexError>(m, "ScalarIndexError",
                                             PyExc_TypeError);

    m.def("eval_expr", &eval_expr, py::arg("expr"), py::arg("env"),
          "Evaluate one engine-spec expression against an env dict "
          "(int/bool/None/list values); returns int or None (X). Exact mirror "
          "of pipeline.cocotb.spec_sim._eval.");

    m.def("discharge_loop_obligations", &discharge, py::arg("post"),
          py::arg("invariant"), py::arg("variant"), py::arg("guard"),
          py::arg("init"), py::arg("body"), py::arg("mapping"),
          py::arg("input_widths"), py::arg("exhaustive_threshold") = 65536,
          py::arg("max_iters") = 64,
          "Discharge the three loop-introduction obligations natively. "
          "Returns a dict with the exact ObligationResult fields; verdicts are "
          "backend-identical to the pure-Python kernel.");

    m.attr("__version__") = "0.1.0";
}
