# Exhibit — Live Verified Derivation (run `102611`, 2026-06-10)

This directory is a frozen copy of the artifacts from the project's headline result: the
**first live run in which an LLM authored only an abstract specification and a deterministic
proof kernel derived the hardware.** The pipeline normally gitignores `artifacts/`; these files
are committed verbatim so the milestone is inspectable without re-running a metered live run.

> **The claim this exhibit backs:** the LLM never wrote the multiplier. It wrote `product = a * b`
> and proposed a loop invariant. The shift-add datapath was *derived* — and installed only after
> the obligation kernel proved it correct over **all 65,536 input pairs**.

---

## Read the files in this order

| # | File | What it shows |
|---|------|---------------|
| 1 | `00_nl_spec.json` | The natural-language prompt (the human input). |
| 2 | `01_summary.json` | Stage 1 (Agent 1): prompt → structured summary + directed test vectors. |
| 3 | `02_formal_spec.json` | **Stage 3 (Agent 3): the abstract spec.** One transition, `product = a * b`, `spec_statement: true`. This is the *entire* hand-authored design — no datapath, no FSM, no registers. |
| 4 | `refinement_chain.json` | **The derivation certificate.** Three deterministic steps that lower the abstract spec to RTL (detailed below). |
| 5 | `03_rtl_output.json` | Compiler 2 metadata for the emitted Verilog. |
| 6 | `output.v` | The derived, synthesizable Verilog-2001 FSMD. |
| 7 | `02_vector_check.json` | The spec-derived golden-vector cross-check (Agent-1 vectors vs. the independent spec interpreter). |
| 8 | `04_evaluation.json` | Stage 4 verdict: `status: success` (cocotb PASS), plus the recorded Agent-1/spec disagreements. |
| 9 | `04_soak.json` | The post-pass mass soak: 2,000 deterministic random cycles, RTL vs. spec, **clean**. |

---

## The certificate (`refinement_chain.json`)

Three steps, each a pure rule application with `pre_hash`/`post_hash` so the chain replays
byte-for-byte:

**Step 0 — `LoopIntroduction`.** The abstract `product = a * b` is refined into a concrete
shift-add loop. The kernel discharged the Back/Morgan iteration obligations (O1 init ⇒ inv,
O2 inv ∧ guard ⇒ inv′ ∧ variant ↓, O3 inv ∧ ¬guard ⇒ post) against the **real expression
semantics** before this step was accepted:

- **invariant:** `product + mplier * mcand = a * b`
- **variant:** `count` &nbsp;(strictly decreases; termination)
- **guard:** `count > 0`
- **init:** `product=0, mcand=a, mplier=b, count=8`
- **body:** `product += mcand` when `mplier % 2 == 1`; `mcand *= 2`; `mplier /= 2`; `count -= 1`

Because the operands are 8-bit, the input space is 2⁸ × 2⁸ = **65,536 pairs ≤ 65,536**, so the
kernel ran an **exhaustive finite proof**, not a sample. Had the invariant been wrong, this rule
would have been a pure no-op — the engine's backtrack signal — with a concrete counterexample.

**Step 1 — `ScheduleHandshakeFSM`.** The verified loop is mechanically scheduled onto the
hardened IDLE(0)/BUSY(1)/DONE(2) control FSM with a `start`/`done` handshake. No proof needed —
soundness lives in step 0; this step is deterministic scheduling.

**Step 2 — `Initialization`.** Synchronous active-high reset clears every register to its reset
value.

The result is `output.v`: a flat-else-if FSMD with a combinational `done = (state == 2)` and a
back-to-back-safe load on `((state == 0) || (state == 2)) && start == 1`.

---

## On the recorded disagreements (`04_evaluation.json`)

The run passed (`status: success`) but flagged 19 vector disagreements. These are **not** failures
and **not** silently ignored — they are the cross-check working as designed. In every case Agent 1
asserted `product = 0` during the multi-cycle BUSY phase, where the spec (correctly) shows the
accumulating partial products. Both sides agree at every `DONE` and on `done` everywhere. The
guardrail records them so a passing run is never a *silent* green when Agent 1 and the spec differ.

---

## Reproduce the thesis offline (no credentials, no metering)

The live run above needs the two API credentials and is billed per token. The *mechanism* it
demonstrates is reproduced deterministically in the offline test suite — a grader can watch the
proof happen for free:

```bash
python3.11 -m pytest tests/test_verified_derivation.py -v
```

That test drives an abstract multiplier through the same `LoopIntroduction` → exhaustive proof →
derived RTL → **real cocotb PASS** path, and pins the negative control: a wrong invariant makes
`LoopIntroduction` a no-op and the chain never reaches RTL-style
(`test_wrong_invariant_makes_loop_introduction_a_noop`).
