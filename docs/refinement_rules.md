# Refinement Rules

## Table 1: Refinement Rules for Process-Level Development

> **Shorthand:** `ss` = `w : [pre, dur, false] ‖ env`

| Name | Transformation and Conditions |
|------|-------------------------------|
| **Weaken Environment** | `ss ⊑ w : [pre', dur, post] ‖ env'`, provided `env ⇒ env'`. |
| **Strengthen During** | `ss ⊑ w : [pre, dur', post] ‖ env`, provided `dur' ⇒ dur`. |
| **Parallel Composition** | `sf ⊑ w_a : [pre, dur_a, false] ‖ env ‖ w_b : [pre, dur_b, false` *[truncated]*`]`, provided i) `w_a ∩ w_b = ∅`, ii) `w_a ∉ vars(dur_b) ∧ w_b ∉ vars(dur_a` *[truncated]* |
| **Piping Composition** | `sf ⊑ w_a : [pre, dur_a, false] ‖ env ∧ dur_b` *[truncated]*, provided i) `w_a ∩ w_b = ∅`, ii) `w_a ∈ vars(dur_b) ∧ w_b ∈` *[truncated]* |
| **Bidirectional Composition** | `sf ⊑ w_a : [pre, dur_a, false] ‖ env ∧ dur_b` *[truncated]*, provided i) `w_a ∩ w_b = ∅`, ii) `w_a ∈ vars(dur_b) ∧ w_b ∈` *[truncated]* |
| **Initialization** | `ss ⊑ w : [pre, dur ∧ rst ⇒` *[truncated]* `[pre, dur, post]`, provided i) `w = reset_state ⇒ pre`, ii) `w = reset` *[truncated]* |
| **Iteration** | `sf ⊑ process[output : w]{w : [T = t_0 ∧ inv, env, T = (t_0` *[truncated]*`]}`, provided i) `pre ⇒ T = 0 ∧ inv`, ii) `inv ⇒ dur`. |

---

## Table 2: Refinement Rules for Control and Data Flow Development

> **Shorthand:** `ss` = `w : [pre, dur, post]`

| Name | Transformation and Conditions |
|------|-------------------------------|
| **Weaken Precondition** | `ss ⊑ w : [pre', dur, post]`, provided `pre ⇒ pre'`. |
| **Strengthen Postcondition** | `ss ⊑ w : [pre, dur, post']`, provided `post' ⇒ post`. |
| **Expand Frame** | `ss ⊑ w : [pre, dur, post ∧ x = x_0]`. |
| **Contract Frame** | `w, x : [pre, dur, post] ⊑ w : [pre, dur, post[x_0/x]]`. |
| **Sequential Composition** | `w : [pre, dur, post] ⊑ w : [pre, dur, mid]; w : [mid, dur_j` *[truncated]*`]`, provided i) `w_0, x_0 ∉ vars(mid)`, ii) `pre ∧ dur ⇒ mid`, iii) `mid ∧ d` *[truncated]* |
| **Assignment** | `w, x : [pre, dur, post] ⊑ w := E; w ⊑ w ⇒ pos` *[truncated]* |
| **Concurrent Assignment** | `w, x : [pre, dur, post] ⊑ w := E, F ⊑ w := E, F[w/E]`, provided `w ∩ n = ∅`. |
| **Leading Assignment** | `w := E; x := F[w/E] ⊑ w := E; x := F`, provided `w ∩ n = ∅`. |
| **Following Assignment** | `w, x : [pre, dur, post[w/E]]; w :=` *[truncated]* |
| **Skip Statement** | `ss ⊑ skip`, provided `pre ∧ dur ⇒ post`. |
| **Introduce Variable** | `ss ⊑ Var x; w, x : [pre, dur, post]`. |
| **Alternation** | `ss ⊑ if G_i then {w : [pre ∧ G_i, dur, post]}` for `i = 1 … n`. |
| **Procedure Assignment** | `w, a := E; P(f/a] ⊑ P(a)`, provided procedure `P(f)` *[truncated]* |
| **Procedure Specification** | `w, a : [pre, dur, post] ⊑ P(a)`, provided i) procedure `P(f)` *[truncated]*, ii) `pre ∧ dur ⇒ pre'[f/a]`, iii) `post'[f/a] ⇒ post`. |
| **Feasibility** | `ss` is feasible, provided `pre ∧ dur ⇒ ∃w.post`. |
