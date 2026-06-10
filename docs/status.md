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
| Compiler 1 / Compiler 2 + bridge | built; Verilog-2001, width-correct, banlist-enforced; **memory arrays** + **combinational outputs** |
| LangGraph orchestration + status routing | built |
| Usage ledger + Agent-3 budget guard | built |
| Deterministic test suite | **348 passed, 0 xfailed** |

The deterministic spine runs **NL → RTL → cocotb PASS offline** on every design class below,
with all LLM boundaries mocked, exercising the real engine, both compilers, and cocotb.

---

## Design classes

Six classes, all offline-proven; five confirmed on a live LLM run (Agent 1 + Agent 3):

| Design | Live | Notes |
|---|---|---|
| 2-bit counter | ✅ `885b9fc0` | bounded-action-space proven live (3 clean, replayable picks) |
| traffic-light FSM | ✅ `15d3dd35` | active-low reset (RC1), clock contract (RC2), critic carve-out (RC3) |
| multi-op ALU | ✅ | free-input width inference (D2) |
| 8-bit accumulator | ✅ `121027-760bd3` | clean after a 3-run arc (RC4–RC8); active-low `rst_n` confirmed live |
| 8×8 register file | ✅ `155212-38cc17` | first **memory array**; clean on the first try |
| 4-deep FIFO | codegen ✅ `181016` | first **combinational output**; live cocotb was a *false red* (one wrong Agent-1 vector) — now removed by the spec-derived cross-check |

---

## Key capabilities (load-bearing, with tests as the record)

- **Memory arrays** — `reg [w-1:0] mem [0:K-1]`, indexed read/write. Indexed write rides in
  the FormalSpec `updates` key (`{"mem[waddr]": "wdata"}`); `Variable.depth` marks a memory;
  `is_rtl_style` carves it out of the reset requirement. Reuses only Init + Iteration.
- **Combinational outputs** — `Transition.combinational: true` → born-concrete `assign`-driven
  wires (e.g. FIFO `full`/`empty`), never clocked or reset. Symmetric engine carve-out to memory.
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

- **FIFO live re-confirmation** — codegen is validated live; a metered re-run would confirm the
  spec-derived cross-check end to end (it now passes *and* flags Agent 1's v10 slip). Optional.
- **Disagreement → diagnoser** — an Agent-1/spec disagreement is surfaced but not yet *routed*
  to the diagnoser as a candidate spec bug.
- **Tier-2 rules** — `ParallelComposition`, `ExpandFrame`/`ContractFrame`,
  `WeakenPrecondition`/`StrengthenPostcondition` are designed (see [background.md](background.md)),
  not implemented — needed beyond FSM+datapath.
- **Sized-counter wrap idiom** — `count <= (count + 1) % 2^k` is correct and iverilog-clean but
  trips a cosmetic verilator `WIDTHTRUNC`; the explicit-wrap form is fully clean (fixtures use it).

Live runs are metered on the Agent-3 Anthropic key ([budget cap](agents.md#budget-guard)) and
need the two credential sets in [running.md](running.md#credentials).

---

## How issues are tracked

Current items only. Resolved work — the BUG-* sweep, the G01–G16 comprehensiveness audit, the
D1–D5 medium-design fixes, and the RC1–RC8 live-debugging arc — lives in the git history and in
the tests that pin each fix. When an item here is resolved, remove it and let its test stand as
the record.
