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
| Refinement engine + six Tier-1 rules | built; catch-all is the sole, replayable driver; robust to a throwing/cycling live picker |
| Compiler 1 / Compiler 2 + bridge | built; Verilog-2001, width-correct, banlist-enforced; **memory arrays** + **combinational outputs** + **FSM control / multi-cycle datapath** |
| LangGraph orchestration + status routing | built |
| Usage ledger + Agent-3 budget guard | built |
| Deterministic test suite | **358 passed, 0 xfailed** |

The deterministic spine runs **NL → RTL → cocotb PASS offline** on every design class below,
with all LLM boundaries mocked, exercising the real engine, both compilers, and cocotb.

---

## Design classes

Seven classes, all offline-proven (NL → RTL → **real cocotb PASS**); six confirmed on a live
LLM run (Agent 1 + Agent 3), the seventh (FSMD multiplier) offline-proven and awaiting its
first live run:

| Design | Live | Notes |
|---|---|---|
| 2-bit counter | ✅ `885b9fc0` | bounded-action-space proven live (3 clean, replayable picks) |
| traffic-light FSM | ✅ `15d3dd35` | active-low reset (RC1), clock contract (RC2), critic carve-out (RC3) |
| multi-op ALU | ✅ | free-input width inference (D2) |
| 8-bit accumulator | ✅ `121027-760bd3` | clean after a 3-run arc (RC4–RC8); active-low `rst_n` confirmed live |
| 8×8 register file | ✅ `155212-38cc17` | first **memory array**; clean on the first try |
| 4-deep FIFO | ✅ `190407` | first **combinational output**; clean live cocotb PASS via the spec-derived bench. The cross-check caught **two** Agent-1 false reds (v10 `empty`, v19 `rd_data`) and surfaced them — no false green. (`181016` was the codegen-validated false-red run that motivated the cross-check.) |
| 8×8 sequential multiplier | offline ✅ | first **FSMD** — control FSM (IDLE/BUSY/DONE) sequencing a multi-cycle shift-add datapath behind a start/done handshake. Reuses ONLY Init + Iteration (no new rule); shift/bit ops via `*2`/`/2`/`%2`. Offline real-cocotb PASS (10 vectors/multiply, derived per-cycle by spec_sim). Awaiting one live run. |

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
