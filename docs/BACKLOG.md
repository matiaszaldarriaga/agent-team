# agent-team — backlog

A running list of enhancements to consider. Add to it as ideas come up during real use.
(Deeper "not in v0.1, by design" items are also noted in `docs/DESIGN.md`.)

**Standing priorities (2026-07-27).** Functionality over reporting: the numbers exist so Matias
has a *sense* of a run, and a subscription means tokens aren't billed per-token anyway. So the
metering items (#3, #5, #7) are worth fixing but are not urgent, and — importantly — **nothing may
stop or kill a run mid-flight**. Checks after a run completes are fine; the invariant is that a run
always lands in a state that's easy to recover from.

## Enhancements

### 1. Per-role / PI-chosen model & effort (under a policy)  — ✅ DONE (see Done section)
Today model/effort/backend are **uniform per job**: set once at `job new`, stored in `spec.json`,
and read for *every* role call (`engine.py`, `run_agent(..., model=spec["model"], effort=spec["effort"])`).
The PI decides the task split and worker count, but **not** the model or effort.

Want (Matias's original idea): differentiate per role — e.g. a cheaper model for grind `worker`s,
`xhigh` only for the `verifier`; and/or let the PI pick per role, **bounded by `policy.json`**
(`effort_max`, `backends_allowed`, `max_workers` — only `max_workers` is wired today).
Sketch: recipes declare optional per-role `{model, effort, backend}`; PI staffing may set them
within the policy ceiling; `_run_role` reads the role's override, falling back to the job default.

### 2. Liveness / "is it going?" signal  — ✅ DONE (see Done section)
Mid-round the on-disk state only updates at each round's **END**, so `view.html` and `job status`
look frozen while workers are actually running (xhigh calls take minutes) — you can't confirm a
run started correctly without inspecting processes. Want:
- the engine writes a **heartbeat each step** (not just each round): a timestamp + current phase
  (`"round 2 · workers(2) running"` / `"verifier"` / `"writing"` / `"checks"`);
- `view.html` shows a **live badge** (`● live — updated 8s ago` vs `stale`);
- `job status` (or a new `job watch`) reports the phase, last-activity age, and whether the runner
  process is alive.

### 3. Rough token→$ money estimate  — LOW (reporting, not functionality)
`codex` reports tokens but no dollar cost (shows `$0.000`). Add a per-model token→$ rate table and
display an **estimated** cost next to tokens (for all backends; codex especially). Label it "est."
Note: on a subscription the dollar figure is a *sense of scale*, not a bill — so this is nice to
have, not load-bearing. Now that roles can run on different backends, a rate table would need to be
per-role to mean anything (`staffing.table(spec)` already gives the per-role breakdown).

### 4. Per-call timeout: too short + discards the report on kill  — ✅ DONE (see Done section)
`CALL_TIMEOUT=2400` (40 min) killed an xhigh codex worker mid-derivation (first real run).
Reframed: spend is the budget's job (lossless); a timeout is only a *hang* backstop.

### 5. Token budget vs. xhigh-codex reality  — MEDIUM (default budgets, NOT mid-round stops)
First real run burned **13.7M tokens in round 1** (one xhigh codex `exec` is an agentic loop —
~2–3M tokens/call with file reads + running oracles). A 3-round derive ≈ 30–40M tokens. The
budget guard only checks at round *boundaries*, so it overshoots within a round and a "safety"
20M cap silently cut the run to ~2 rounds.
**Decided (2026-07-27):** mid-round budget checks are **rejected** — nothing stops a run in
flight; a round always finishes so the job lands recoverable. What's left is: much larger default
budgets (a 400k default against a 13.7M round is theatre), and surfacing projected spend up front.
Partly mitigated already — tokens now accumulate *per call* rather than at each round's end, so
the view shows the burn climbing live instead of jumping once a round.
Per-role effort (#1) is the real lever here: shallow workers, deep verifier.

### 6. Smarter auto-slug  — LOW
`--name` now lets you set the job id, but the auto-slug still scrapes the intent's first words
(which can be a preamble). Could skip obvious preface lines or summarize. Minor.

### 7. Claude token accounting misses cache tokens → budget guard is inert  — MEDIUM (accuracy)
`_run_claude` sets `itok = usage.input_tokens`, `otok = usage.output_tokens`, and
`AgentResult.tokens = itok + otok`. Claude reports cached context in **separate** fields
(`cache_read_input_tokens`, `cache_creation_input_tokens`), so nearly all real consumption is
invisible to the meter. Measured 2026-07-24 on a trivial one-line prompt:
counted **7 tokens** (2 in + 5 out) while actually using **15,273 cache-read + 8,588
cache-creation**. Same trivial task through `run_agent`: claude reported **200** tokens, codex
**34,888**. Consequences: (a) `_budget_exceeded` never fires on the claude backend — only
`rounds` bounds a run; (b) token counts are not comparable across backends, so the five existing
codex jobs and any new claude job can't be read on the same axis.
Fix sketch: fold both cache fields into `itok` (one line), and note the semantics change in the
spec so old jobs stay interpretable. Related: since claude reports `total_cost_usd` and codex
reports nothing, a **`budget_usd`** stop may be the better meter for claude — see #3 and #5.
Sharper since #1: a job can now mix backends across roles, so a token column that means one thing
for the codex worker and another for the claude verifier isn't just imprecise, it's incoherent.
Fixing the claude side is what makes the per-role token numbers comparable.
(Still a *meter*, not a brake — see the standing priorities: it must not gain the power to stop a
run mid-flight.)

### 8. Role scheduling: no way to have a role run only at the END  — ✅ DONE (see Done section)
`extra` roles run **every round** (`for extra_role in spec["team"].get("extra", [])` in the round
loop), and `lead` / `verifier` are hard-indexed so they can't be dropped at all. The minimum team
is therefore 3 calls/round (pi + 1 worker + verifier). For a small job you often want the `writer`
to appear **once, at the end** — a polish/assembly pass over what the worker has been maintaining
— not on every round paying for a hand-off each time.
Today's workaround: run with `extra: []`, then `job resume <id> --rounds 1 --say "write-up only:
..."`, which extends the budget by exactly one round and gives a write-up pass with a direction.
Fix sketch: per-role schedule in the recipe/team spec — `{"role": "writer", "when": "last"}` with
`when ∈ every | first | last | [round numbers]`; default `every` so existing recipes are unchanged.

### 9. `job <verb> <id>` needs the exact id  — LOW
`_need()` does `Job(job_id)` and fails unless the string matches the directory name exactly, so
every command needs the full `2026-07-24_194925_derive-n6closure`. Accept a unique prefix or
substring (and `--name`'s slug) and error only on ambiguity.

## Done
- **Per-role model / effort / backend, and per-role schedules** (#1 + #8) — `spec["roles"]` maps
  a role name to `{backend, model, effort, when}`, each key falling back to the job default, so an
  untouched job behaves exactly as before. New module `agentteam/staffing.py` owns resolution;
  `_run_role` asks it instead of reading `spec["model"]`/`spec["effort"]`. Layered, later wins key
  by key: **recipe `roles` block → `job new --role <role>:<k>=<v>,… → PI staffing**, and only the
  PI layer is clamped by `policy.json` (you're the principal; your flags are never clamped).
  - The PI may now emit `ROLE <role>: effort=…[, backend=…]` alongside `WORKERS: <n>`; a
    malformed or out-of-team line is dropped and logged, never fatal.
  - `policy.json`'s `effort_max` and `backends_allowed` are now actually enforced (they were dead
    keys); every clamp is recorded in `log.jsonl`.
  - A role that switches backend picks up *that* backend's default model — the job-level model
    name belongs to the other CLI's vocabulary. Effort carries across (shared vocabulary).
    `backends.DEFAULTS` moved from `cli.py` to `backends.py` for this.
  - `when` ∈ `every` (default) | `first` | `last` | `[rounds]` schedules a recipe's `extra` roles,
    so `writer:when=last` is one assembly pass instead of a hand-off every round. `last` means the
    last round of *this run*, so it still fires when the lead signals `[[DONE]]` early or the
    budget/kill-switch ends things — the deliverable never goes unwritten. Lead and verifier still
    run every round by design.
  - Provenance: resolved staffing shows in `job new`/`job status`/`job staff`, in a **Staffing**
    table in `view.html` (only when the team isn't uniform), and per call in `log.jsonl`
    (`role_call` with backend/model/effort/tokens). With mixed backends the single job-level model
    line no longer says what produced a given claim.
  - Side effects: tokens now accumulate **per call** rather than at each round's end, so the view
    shows the burn climbing live; `Job.log` took a lock (parallel workers each log a line now).
  - Recipes are deliberately left uniform, so nothing changed cost or behaviour under you. To make
    the writer a single end pass, add `"roles": {"writer": {"when": "last"}}` to `recipes/derive.json`.
- **Idle-backstop timeouts + salvage** — replaced the aggressive 40-min hard timeout. Spend is
  now bounded only by the token budget + round count (lossless). The backend streams stdout/stderr
  and kills a call ONLY after `idle_timeout` (default 1800s) of **no output AND no file writes**
  — a working call that reads/runs/writes is never killed. A hard wall-clock `--timeout` is off by
  default (`--idle-timeout` also configurable, 0 disables). On any kill the partial report is
  salvaged from the stream (files on disk already persisted). Verified with fake processes:
  idle-kill, file-activity-keeps-alive, hard-timeout, and salvage.
- **Liveness / heartbeat** — the engine writes a phase + timestamp each step (`planning` /
  `workers running` / `verifying` / `writing` / `checks`) and re-renders, so the view updates
  mid-round. `view.html` shows the phase in the "updated" line; `job status` marks a live run with
  `●` + phase; a PID file + `is_running()` back it; new **`job watch <id>`** live-polls until the
  job ends. (Applies to runs started after this change — a run already in progress uses the old code.)
- **Intent box rendering** — was a `<p>` (collapsed newlines into a wall of text); now a
  `<pre class="intent">` so line breaks show and it scrolls if long.

## Deferred by design (see docs/DESIGN.md)
- Shared cross-job **project corpus** (verified results shared across jobs) — optional layer.
- PI **approval gate** (plan-and-go is the default).
- **Stopping a run mid-round.** Rejected 2026-07-27: a round always finishes, so a job lands in a
  state that's easy to recover from. Budget/kill-switch are checked between rounds only.
- `budget_tokens_max` in **policy.json** (`max_workers`, `effort_max`, `backends_allowed` are
  enforced as of #1).
- Flesh out **`draft` / `wiki`** recipes from real use.
