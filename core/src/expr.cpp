// Tokenizer + recursive-descent parser + evaluator, mirroring
// pipeline/cocotb/spec_sim.py (_tokenize / _Interp / _eval) exactly.
// See include/rtlcore/expr.hpp for the mirror contract and the documented
// divergences. tests/test_native_kernel.py holds this file to the Python
// reference with a differential fuzz.

#include "rtlcore/expr.hpp"

#include <cctype>

namespace rtlcore {

namespace {

constexpr std::uint64_t U32 = 0xFFFFFFFFull;

// ---------------------------------------------------------------------------
// Tokenizer — mirrors _TOKEN_RE:
//   \s*(/\\|\\/|/=|<=|>=|[=<>]|[-+*/%]|~|!|\[|\]|\(|\)|\d+|[A-Za-z_]\w*|')
// Unmatched characters are SKIPPED (spec_sim keeps the sim robust rather than
// crashing); the prime ' is matched then DROPPED.
// ---------------------------------------------------------------------------

bool is_ident_start(char ch) {
    return (ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') || ch == '_';
}
bool is_ident_char(char ch) {
    return is_ident_start(ch) || (ch >= '0' && ch <= '9');
}
bool is_digit(char ch) { return ch >= '0' && ch <= '9'; }

std::vector<std::string> tokenize(const std::string& s) {
    std::vector<std::string> toks;
    size_t pos = 0;
    const size_t n = s.size();
    while (pos < n) {
        // \s* prefix
        if (std::isspace(static_cast<unsigned char>(s[pos]))) {
            ++pos;
            continue;
        }
        const char ch = s[pos];
        const char nx = pos + 1 < n ? s[pos + 1] : '\0';
        // two-char operators first (regex alternation order): /\  \/  /=  <=  >=
        if (ch == '/' && nx == '\\') { toks.emplace_back("/\\"); pos += 2; continue; }
        if (ch == '\\' && nx == '/') { toks.emplace_back("\\/"); pos += 2; continue; }
        if (ch == '/' && nx == '=') { toks.emplace_back("/="); pos += 2; continue; }
        if (ch == '<' && nx == '=') { toks.emplace_back("<="); pos += 2; continue; }
        if (ch == '>' && nx == '=') { toks.emplace_back(">="); pos += 2; continue; }
        // single-char operators / punctuation
        if (ch == '=' || ch == '<' || ch == '>' || ch == '+' || ch == '-' ||
            ch == '*' || ch == '/' || ch == '%' || ch == '~' || ch == '!' ||
            ch == '[' || ch == ']' || ch == '(' || ch == ')') {
            toks.emplace_back(1, ch);
            ++pos;
            continue;
        }
        if (is_digit(ch)) {
            size_t end = pos;
            while (end < n && is_digit(s[end])) ++end;
            toks.emplace_back(s.substr(pos, end - pos));
            pos = end;
            continue;
        }
        if (is_ident_start(ch)) {
            size_t end = pos;
            while (end < n && is_ident_char(s[end])) ++end;
            toks.emplace_back(s.substr(pos, end - pos));
            pos = end;
            continue;
        }
        // ' is matched by the regex but dropped; everything else is unmatched.
        // Either way: advance one character and emit nothing.
        ++pos;
    }
    return toks;
}

bool all_digits(const std::string& t) {
    if (t.empty()) return false;
    for (char ch : t)
        if (!is_digit(ch)) return false;
    return true;
}

// Parse a digit run into 64 bits, saturating on overflow (documented
// divergence: Python ints are unbounded; no real spec carries such literals).
std::uint64_t parse_literal(const std::string& t) {
    std::uint64_t v = 0;
    for (char ch : t) {
        const std::uint64_t d = static_cast<std::uint64_t>(ch - '0');
        if (v > (UINT64_MAX - d) / 10) return UINT64_MAX;
        v = v * 10 + d;
    }
    return v;
}

// ---------------------------------------------------------------------------
// Parser — mirrors _Interp's grammar method-for-method. Python PARSES WHILE
// EVALUATING; we parse once into an Expr. The token consumption order is
// identical, so tolerances and the truncated-expression IndexError carry over.
// ---------------------------------------------------------------------------

class Parser {
public:
    Parser(std::vector<std::string> toks, SymTab& syms, Expr& out)
        : toks_(std::move(toks)), syms_(syms), out_(out) {}

    int expr() {
        if (peek() == "IF") {
            take();
            const int c = expr();
            if (peek() == "THEN") take();
            const int a = expr();
            if (peek() == "ELSE") take();
            const int b = expr();
            return add(Node{Op::If, 0, -1, c, a, b});
        }
        return or_();
    }

private:
    std::vector<std::string> toks_;
    size_t i_ = 0;
    SymTab& syms_;
    Expr& out_;

    static const std::string& none_tok() {
        static const std::string sentinel = "\x01<eof>";  // unmatchable
        return sentinel;
    }
    const std::string& peek() const {
        return i_ < toks_.size() ? toks_[i_] : none_tok();
    }
    const std::string& take() {
        if (i_ >= toks_.size())
            throw std::out_of_range("truncated expression");  // Python: IndexError
        return toks_[i_++];
    }
    int add(Node nd) {
        out_.nodes.push_back(nd);
        return static_cast<int>(out_.nodes.size() - 1);
    }
    int binary(Op op, int a, int b) { return add(Node{op, 0, -1, a, b, -1}); }
    int unary(Op op, int a) { return add(Node{op, 0, -1, a, -1, -1}); }

    int or_() {
        int v = and_();
        while (peek() == "\\/" || peek() == "OR") {
            take();
            v = binary(Op::Or, v, and_());
        }
        return v;
    }
    int and_() {
        int v = cmp();
        while (peek() == "/\\" || peek() == "AND") {
            take();
            v = binary(Op::And, v, cmp());
        }
        return v;
    }
    int cmp() {
        int v = addsub();
        const std::string& op = peek();
        Op k;
        if (op == "=") k = Op::Eq;
        else if (op == "/=") k = Op::Ne;
        else if (op == "<=") k = Op::Le;
        else if (op == ">=") k = Op::Ge;
        else if (op == "<") k = Op::Lt;
        else if (op == ">") k = Op::Gt;
        else return v;  // at most ONE optional comparison, like Python
        take();
        return binary(k, v, addsub());
    }
    int addsub() {
        int v = muldiv();
        while (peek() == "+" || peek() == "-") {
            const Op k = take() == "+" ? Op::Add : Op::Sub;
            v = binary(k, v, muldiv());
        }
        return v;
    }
    int muldiv() {
        int v = unry();
        while (peek() == "*" || peek() == "/" || peek() == "%" || peek() == "mod") {
            const std::string op = take();
            const Op k = op == "*" ? Op::Mul : op == "/" ? Op::Div : Op::Mod;
            v = binary(k, v, unry());
        }
        return v;
    }
    int unry() {
        const std::string& t = peek();
        if (t == "~" || t == "!" || t == "NOT") {
            take();
            return unary(Op::Not, unry());
        }
        if (t == "-") {
            take();
            return unary(Op::Neg, unry());
        }
        return primary();
    }
    int primary() {
        const std::string t = take();
        if (t == "(") {
            const int v = expr();
            if (peek() == ")") take();
            return v;
        }
        if (all_digits(t)) {
            Node nd{Op::Const, parse_literal(t), -1, -1, -1, -1};
            return add(nd);
        }
        if (t == "TRUE" || t == "FALSE") {
            Node nd{Op::Const, t == "TRUE" ? 1ull : 0ull, -1, -1, -1, -1};
            return add(nd);
        }
        // identifier, optionally indexed (memory read). Anything else — a stray
        // ) ] THEN ELSE — also lands here and is looked up as a name, exactly
        // like Python's `self.state.get(t)` (absent -> X).
        if (peek() == "[") {
            take();
            const int idx = expr();
            if (peek() == "]") take();
            Node nd{Op::Index, 0, syms_.intern(t), idx, -1, -1};
            return add(nd);
        }
        Node nd{Op::Var, 0, syms_.intern(t), -1, -1, -1};
        return add(nd);
    }
};

// ---------------------------------------------------------------------------
// Evaluator — X-pessimistic, U32-masked, mirrors _Interp's value semantics.
// ---------------------------------------------------------------------------

Value eval_node(const std::vector<Node>& nodes, int i, const Env& env) {
    const Node& nd = nodes[static_cast<size_t>(i)];
    switch (nd.op) {
        case Op::Const:
            return Value::of(nd.lit);
        case Op::Var:
            return env.scalars[static_cast<size_t>(nd.slot)];
        case Op::Index: {
            // Mirror: arr None -> X;  idx X -> X;  out of range -> X;
            //         arr bound but SCALAR -> TypeError (Python: len(int)).
            const auto* arr = env.arrays[static_cast<size_t>(nd.slot)];
            const Value scalar = env.scalars[static_cast<size_t>(nd.slot)];
            if (arr == nullptr && scalar.x) return Value::X();
            const Value idx = eval_node(nodes, nd.a, env);
            if (idx.x) return Value::X();
            if (arr == nullptr)
                throw ScalarIndexError("indexing a scalar value");
            if (idx.v >= arr->size()) return Value::X();
            return (*arr)[static_cast<size_t>(idx.v)];
        }
        case Op::If: {
            const Value c = eval_node(nodes, nd.a, env);
            if (c.x) return Value::X();
            // Lazy branch selection (documented divergence: Python evaluates
            // both; evaluation is total, so only the taken value is observable).
            return c.v != 0 ? eval_node(nodes, nd.b, env)
                            : eval_node(nodes, nd.c, env);
        }
        case Op::Or: {
            const Value l = eval_node(nodes, nd.a, env);
            const Value r = eval_node(nodes, nd.b, env);  // no short-circuit
            if (l.x || r.x) return Value::X();
            return Value::of((l.v != 0 || r.v != 0) ? 1 : 0);
        }
        case Op::And: {
            const Value l = eval_node(nodes, nd.a, env);
            const Value r = eval_node(nodes, nd.b, env);
            if (l.x || r.x) return Value::X();
            return Value::of((l.v != 0 && r.v != 0) ? 1 : 0);
        }
        case Op::Eq: case Op::Ne: case Op::Le:
        case Op::Ge: case Op::Lt: case Op::Gt: {
            const Value l = eval_node(nodes, nd.a, env);
            const Value r = eval_node(nodes, nd.b, env);
            if (l.x || r.x) return Value::X();
            bool t = false;
            switch (nd.op) {
                case Op::Eq: t = l.v == r.v; break;
                case Op::Ne: t = l.v != r.v; break;
                case Op::Le: t = l.v <= r.v; break;
                case Op::Ge: t = l.v >= r.v; break;
                case Op::Lt: t = l.v < r.v; break;
                default:     t = l.v > r.v; break;
            }
            return Value::of(t ? 1 : 0);
        }
        case Op::Add: case Op::Sub: case Op::Mul: {
            const Value l = eval_node(nodes, nd.a, env);
            const Value r = eval_node(nodes, nd.b, env);
            if (l.x || r.x) return Value::X();
            // uint64 wraparound then & U32 == Python's unbounded-int & U32,
            // because 2^32 divides 2^64.
            std::uint64_t out;
            if (nd.op == Op::Add) out = l.v + r.v;
            else if (nd.op == Op::Sub) out = l.v - r.v;
            else out = l.v * r.v;
            return Value::of(out & U32);
        }
        case Op::Div: case Op::Mod: {
            const Value l = eval_node(nodes, nd.a, env);
            const Value r = eval_node(nodes, nd.b, env);
            if (l.x || r.x) return Value::X();
            if (r.v == 0) return Value::X();  // div/mod by zero -> X
            const std::uint64_t out = nd.op == Op::Div ? l.v / r.v : l.v % r.v;
            return Value::of(out & U32);
        }
        case Op::Not: {
            const Value v = eval_node(nodes, nd.a, env);
            if (v.x) return Value::X();
            return Value::of(v.v == 0 ? 1 : 0);
        }
        case Op::Neg: {
            const Value v = eval_node(nodes, nd.a, env);
            if (v.x) return Value::X();
            return Value::of((0 - v.v) & U32);
        }
    }
    return Value::X();  // unreachable
}

}  // namespace

Expr compile_expr(const std::string& expr, SymTab& syms) {
    Expr e;
    Parser p(tokenize(expr), syms, e);
    e.root = p.expr();
    // Trailing tokens after a complete expression are ignored, like Python.
    return e;
}

Value eval(const Expr& e, const Env& env) {
    return eval_node(e.nodes, e.root, env);
}

}  // namespace rtlcore
