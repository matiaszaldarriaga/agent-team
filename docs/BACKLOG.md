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

### 2. Liveness / "is it going?" signal  — HIGH
Mid-round the on-disk state only updates at each round's **END**, so `view.html` and `job status`
look frozen while workers are actually running (xhigh calls take minutes) — you can't confirm a
run started correctly without inspecting processes. Want:
- the engine writes a **heartbeat each step** (not just each round): a timestamp + current phase
  (`"round 2 · workers(2) running"` / `"verifier"` / `"writing"` / `"checks"`);
- `view.html` shows a **live badge** (`● live — updated 8s ago` vs `stale`);
- `job status` (or a new `job watch`) reports the phase, last-activity age, and whether the runner
  process is alive.

### 3. Rough token→$ money estimate  — requested (minor)
`codex` reports tokens but no dollar cost (shows `$0.000`). Add a per-model token→$ rate table and
display an **estimated** cost next to tokens (for all backends; codex especially). Label it "est."

### 4. Smarter auto-slug  — LOW
`--name` now lets you set the job id, but the auto-slug still scrapes the intent's first words
(which can be a preamble). Could skip obvious preface lines or summarize. Minor.

## Done
- **Intent box rendering** — was a `<p>` (collapsed newlines into a wall of text); now a
  `<pre class="intent">` so line breaks show and it scrolls if long.

## Deferred by design (see docs/DESIGN.md)
- Shared cross-job **project corpus** (verified results shared across jobs) — optional layer.
- PI **approval gate** (plan-and-go is the default).
- Full **policy.json** enforcement (only `max_workers` today).
- Flesh out **`draft` / `wiki`** recipes from real use.
