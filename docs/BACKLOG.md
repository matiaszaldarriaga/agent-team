# agent-team — backlog

A running list of enhancements to consider. Add to it as ideas come up during real use.
(Deeper "not in v0.1, by design" items are also noted in `docs/DESIGN.md`.)

## Enhancements

### 1. Per-role / PI-chosen model & effort (under a policy)  — HIGH interest
Today model/effort/backend are **uniform per job**: set once at `job new`, stored in `spec.json`,
and read for *every* role call (`engine.py`, `run_agent(..., model=spec["model"], effort=spec["effort"])`).
The PI decides the task split and worker count, but **not** the model or effort.

Want (Matias's original idea): differentiate per role — e.g. a cheaper model for grind `worker`s,
`xhigh` only for the `verifier`; and/or let the PI pick per role, **bounded by `policy.json`**
(`effort_max`, `backends_allowed`, `max_workers` — only `max_workers` is wired today).
Sketch: recipes declare optional per-role `{model, effort, backend}`; PI staffing may set them
within the policy ceiling; `_run_role` reads the role's override, falling back to the job default.

### 2. Codex dollar-cost reporting  — LOW
`codex` (unlike `claude`) doesn't emit `total_cost_usd`, so `cost_usd` shows `$0.000` while
`tokens` is tracked correctly. Options: a token→cost estimate table per model, or just surface
tokens as the codex meter and hide the dollar field for codex. Tokens suffice for now.

### 3. Smarter auto-slug  — LOW
`--name` now lets you set the job id, but the auto-slug still scrapes the intent's first words
(which can be a preamble). Could skip obvious preface lines or summarize. Minor.

## Deferred by design (see docs/DESIGN.md)
- Shared cross-job **project corpus** (verified results shared across jobs) — optional layer.
- PI **approval gate** (plan-and-go is the default).
- Full **policy.json** enforcement (only `max_workers` today).
- Flesh out **`draft` / `wiki`** recipes from real use.
