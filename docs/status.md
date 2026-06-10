# Status & Known Issues

A snapshot of the current state. The per-run history and resolved-bug detail live in the
git log and in the tests that pin each fix — this file is **not** the archive.

_Last updated: 2026-06-09._

---

## What's built

| Component | State |
|---|---|
| Stage 1 — prompt → `SpecSummary` (Agent 1) | built |
| Stage 2 — testbench generation (deterministic) | built |
| Stage 3 — spec authoring, refinement, codegen (Agent 3 + engine + Compiler 2) | built |
| Stage 4 — cocotb simulation | built; **spec-derived golden-vector cross-check** (removes Agent-1 false reds; flags Agent-1/spec disagreements) |
| Diagnoser — failure classification + routing | built |
| Refinement engine + eight rules (six Tier-1 + `LoopIntroduction` + `ScheduleHandshakeFSM`) | built; catch-all is the sole, replayable driver; robust to a throwing/cycling live picker; **verified derivation**: abstract spec statement → obligation-checked loop → scheduled FSMD |
| Obligation kernel (`pipeline/refinement/obligations.py`) | built; discharges Morgan/Back O1/O2/O3 against the real expression semantics; honest `mode` (exhaustive-proof vs sampled) |
| Native verification core (`core/`, optional) | built; C++ exact-verdict mirror of the evaluator + obligation kernel (~300× on the live-loop proof, 23 s → 74 ms at 8-bit) **and the spec-sim cycle engine** (~0.8M edges/s, 20k-cycle FIFO 1.5 s → 25 ms); auto-dispatch with pure-Python fallback |
| Compiler 1 / Compiler 2 + bridge | built; Verilog-2001, width-correct, banlist-enforced; **memory arrays** + **combinational outputs** + **FSM control / multi-cycle datapath** |
| LangGraph orchestration + status routing | built |
| Usage ledger + Agent-3 budget guard | built |
| Deterministic test suite | **457 passed, 0 xfailed** (+22 opt-in live-LLM tests, deselected by default) |

The deterministic spine runs **NL → RTL → cocotb PASS offline** on every design class below,
with all LLM boundaries mocked, exercising the real engine, both compilers, and cocotb.

---

## Design classes

Seven classes, all offline-proven (NL → RTL → **real cocotb PASS**) and exercised on a live
LLM run (Agent 1 + Agent 3). The FSMD multiplier's first live run additionally surfaced (via
the spec-derived cross-check) and fixed a handshake bug — see its row:

| Design | Live | Notes |
|---|---|---|
| 2-bit counter | ✅ `885b9fc0` | bounded-action-space proven live (3 clean, replayable picks) |
| traffic-light FSM | ✅ `15d3dd35` | active-low reset (RC1), clock contract (RC2), critic carve-out (RC3) |
| multi-op ALU | ✅ | free-input width inference (D2) |
| 8-bit accumulator | ✅ `121027-760bd3` | clean after a 3-run arc (RC4–RC8); active-low `rst_n` confirmed live |
| 8×8 register file | ✅ `155212-38cc17` | first **memory array**; clean on the first try |
| 4-deep FIFO | ✅ `190407` | first **combinational output**; clean live cocotb PASS via the spec-derived bench. The cross-check caught **two** Agent-1 false reds (v10 `empty`, v19 `rd_data`) and surfaced them — no false green. (`181016` was the codegen-validated false-red run that motivated the cross-check.) |
| 8×8 sequential multiplier | ✅ `195118` (+fix) | first **FSMD** — control FSM (IDLE/BUSY/DONE) sequencing a multi-cycle shift-add datapath behind a start/done handshake. Reuses ONLY Init + Iteration (no new rule); shifts via `*2`/`/2`/`%2`. Live run proved the multiplier correct across the full 16-bit range **and** the cross-check exposed a handshake bug — a `start` landing in the 1-cycle DONE was dropped (the 3rd multiply never ran). **Fixed:** the load accepts `start` in IDLE *or* DONE (true back-to-back), with a regression test. Hardened design offline-proven; a confirming re-run is optional. |

---

## Key capabilities (load-bearing, with tests as the record)

- **Memory arrays** — `reg [w-1:0] mem [0:K-1]`, indexed read/write. Indexed write rides in
  the FormalSpec `updates` key (`{"mem[waddr]": "wdata"}`); `Variable.depth` marks a memory;
  `is_rtl_style` carves it out of the reset requirement. Reuses only Init + Iteration.
- **Combinational outputs** — `Transition.combinational: true` → born-concrete `assign`-driven
  wires (e.g. FIFO `full`/`empty`), never clocked or reset. Symmetric engine carve-out to memory.
- **FSM control + multi-cycle datapath (FSMD)** — a control FSM (integer-encoded `state`) plus
  an iteration `count` sequence a datapath over many clocks behind a `start`/`done` handshake,
  all inside flat else-if guard chains on one clocked transition + a combinational `done`. No
  new rule — Init + Iteration only. Shift/bit operators are absent, so shifts are arithmetic:
  left = `*2`, right = `/2`, low bit = `%2`. Proven by the sequential shift-add multiplier; the
  spec-derived golden vectors make multi-cycle latency verifiable one-edge-per-vector.
- **Spec-derived golden vectors** — `pipeline/cocotb/spec_sim.py` is an independent
  cycle-accurate interpreter of the refined spec (reset pulse, nonblocking read-before-write,
  combinational fixpoint, memory X-until-written, unsigned 32-bit arithmetic). `vector_check.py`
  derives correct expecteds from Agent 1's **input stimulus**; Stage 4 runs cocotb against them
  (no false red) and records Agent-1/spec disagreements in `02_vector_check.json`. It reproduces
  all five offline traces exactly. **Guardrail:** because cocotb now checks RTL against a
  spec-derived reference, a disagreement is recorded on `04_evaluation.json` and surfaced by
  `main.py` ("PASSED WITH UNRESOLVED AGENT-1/SPEC DISAGREEMENT") — a passing run is never a
  *silent* green when Agent 1 and the spec differ.
- **Refinement robustness** — the catch-all (base prompt, all rules) is the sole driver, so the
  on-disk chain is self-contained and replayable; a throwing/no-op `pick_rule` is a
  strike→backtrack, not a crash; the **RC4 port-direction gate** turns a false-green interface
  into a loud `partial` halt; the revise path clears the chain (RC6) for a replayable rebuild.
- **Verified refinement (abstract → derived)** — an ABSTRACT Morgan spec statement
  (`Transition.spec_statement`, e.g. `product' = a*b`) is refined into a concrete clocked loop
  only after the obligation kernel discharges the iteration-rule obligations (O1 init⇒inv,
  O2 inv∧guard⇒inv'∧variant↓, O3 inv∧¬guard⇒post) against the **real** expression semantics
  (`spec_sim._eval`) — soundness from the CHECK, not the proposer. `LoopIntroduction` installs
  the verified loop (failure = engine no-op → backtrack, with a counterexample);
  `ScheduleHandshakeFSM` then mechanically schedules it onto the hardened IDLE/BUSY/DONE
  start/done FSMD (body conditionals FLATTENED into the else-if chains). The discharged
  obligations are recorded on the chain (`action["refinement"]`) as the derivation certificate.
  E2E: abstract multiplier → exhaustive proof (4,096 cases) → derived RTL → real cocotb PASS;
  a wrong invariant stalls the chain (`tests/test_verified_derivation.py`).
- **Native verification core** (`core/`, optional) — the obligation kernel runs on every
  `LoopIntroduction` proposal (including failed ones while backtracking), and the pure-Python
  evaluator re-parses each expression per call. The C++ core compiles expressions once and
  enumerates natively: ~205× at 6-bit, ~311× at 8-bit (23 s → 74 ms). EXACT-verdict mirror —
  same mode/cases_checked/counterexamples, pinned by a 12,000-case differential fuzz + full
  result-equality tests (`tests/test_native_kernel.py`) — so chain replay is backend-independent.
  Auto-dispatch in `obligations.py` (`OBLIGATIONS_BACKEND` to force); pure-Python fallback when
  not built (`core/build.sh`). The same core also hosts the **spec-sim cycle engine**: Python
  keeps the one-time composition (`SpecSimulator.__init__`, the bridge functions Compiler 2
  shares); C++ runs the per-edge loop (reset pulse, comb fixpoint, read-before-write commits,
  memory writes, width masks) at ~0.8M edges/s — exact-ROW mirror (`derive_expected(backend=)`,
  `SPECSIM_BACKEND`), pinned by every fixture trace + a randomized-stimulus differential fuzz
  (`tests/test_native_specsim.py`). Today's ~20-vector Stage-4 derivation was never slow; this
  is what makes a future MASS spec-vs-RTL cross-check (thousands of random cycles per run)
  affordable.

---

## Open / deferred

- **Disagreement → diagnoser** — an Agent-1/spec disagreement is surfaced but not yet *routed*
  to the diagnoser as a candidate spec bug. The FIFO live re-run (`190407`) showed the value:
  two genuine disagreements, both Agent-1 errors (false reds avoided), zero spec bugs — a
  router would classify and close them automatically instead of leaving a flagged-pass banner.
- **Tier-2 rules** — `ParallelComposition`, `ExpandFrame`/`ContractFrame`,
  `WeakenPrecondition`/`StrengthenPostcondition` are designed (see [background.md](background.md)),
  not implemented — needed beyond FSM+datapath.
- **Verilator width nits (cosmetic)** — iverilog `-Wall` (the lint gate) is clean on every
  design, but verilator `-Wall` flags width-expansion classes: `count <= (count + 1) % 2^k`
  (`WIDTHTRUNC`) and the multiplier's 8→16-bit operand load `mcand <= a` (`WIDTHEXPAND`, an
  implicit zero-extension). Both are functionally correct; the clean fix is a Compiler-2
  width-extension pass (concat is unsupported), deferred as polish.

Live runs are metered on the Agent-3 Anthropic key ([budget cap](agents.md#budget-guard)) and
need the two credential sets in [running.md](running.md#credentials).

---

## How issues are tracked

Current items only. Resolved work — the BUG-* sweep, the G01–G16 comprehensiveness audit, the
D1–D5 medium-design fixes, and the RC1–RC8 live-debugging arc — lives in the git history and in
the tests that pin each fix. When an item here is resolved, remove it and let its test stand as
the record.
