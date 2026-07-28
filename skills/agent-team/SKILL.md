---
name: agent-team
description: Run and manage agent-team "jobs" — fire a bounded team of agents (on codex or claude) into an isolated subtree to produce ONE deliverable, then resume/monitor/freeze it. Use when the user wants to start, staff, run, resume, steer, or check a multi-agent job to derive/prove a result, write a draft, add a code feature, or build a wiki. Human-initiated only; confirm before any run (real spend).
---

# agent-team

A portable CLI (`job`) that fires a **bounded team of agents into an isolated subtree**
(`jobs/<id>/` in the current project) to produce one deliverable, then stops. The human decides
what happens next: resume with a nudge, freeze, or abandon. Backends `codex` and `claude` are
interchangeable per job. Tool repo: `~/Dropbox/agent-team` (README has full detail).

## When to use
The user asks to start / staff / run / resume / steer / monitor a "job" or an "agent team" for a
concrete outcome: derive or prove something, write a draft/notes, add a code feature, build a wiki.

## Guardrails (important)
- **Human-initiated only.** Never start, staff, or run a job on your own initiative.
- **Confirm before `job staff` or `job run`** — each spends real tokens on `codex`/`claude`, and a
  run can be long (many calls at high effort). State the rough cost/time and get an explicit go.
- **Never auto-launch, loop, or schedule** a run. No background timers. Bounded rounds only.
- Use `--backend mock` to exercise the machinery with zero spend when demonstrating.

## Discover the specifics at runtime (don't guess)
```
job --help            # all verbs
job recipes           # job types: derive, feature, draft, wiki
job roles             # the cast: pi, worker, verifier, code-reviewer, test-writer, writer
job status            # existing jobs and their <id>
```

## Typical flow
```
# create (scaffold; zero spend). Long/LaTeX intents: put them in a file and pass @file.
job new derive "@intent.txt" --pi --backend codex --model gpt-5.6-sol --effort xhigh --rounds 3
job staff <id>        # (optional) PI plans + sizes the team — 1 call; show the user its decision
job run <id>          # run the rounds (spend!) — run in the background for long jobs
# monitor: job status (● = live + phase) · job watch <id> · jobs/<id>/view.html · job serve
# steer:   job say <id> "..."
job resume <id> --say "focus on the resonant channel"
job freeze <id>       # keep it (weave into the project later)  |  job abandon <id>
```
Also on `job new`: `--name <slug>` (readable job id), `--workers N`, `--budget N`,
`--idle-timeout N` (hang backstop, default 1800s; 0 disables), `--timeout N` (hard wall-clock,
off by default), `--checkpoint N` (see below), `--acceptance CMD` + `--acceptance-guard PATH`.

**`--pi` on `job new` staffs immediately** — it is plan-and-go, so it makes one paid PI call at
creation. If the human wants a zero-spend scaffold, omit `--pi` and run `job staff <id>` later
after showing them the configuration.

## Scoping a code job (do this before writing any intent)

The `feature` recipe is deliberately small: **one worker, one independent `code-reviewer`, tests in
the same commit as the code, one `writer` pass at the end.** Do not add roles or ask for extra
artifacts unless the user asks. A third agent re-running the suite the worker already ran cost 38%
of the first real run.

Help the user split the work this way:

1. **They write an executable acceptance test first**, red — that is the specification. Offer to
   draft it; it is the highest-value thing in the whole setup and it cannot be gamed.
2. **The intent stays short** (~50 lines) and covers only what a test cannot express: environment
   and how to run things, frozen conventions, the data boundary, and explicit anti-goals ("do not
   test the test suite", "do not rewrite module X").
3. **3 rounds, then read.** Not 8.

Prose demands like "eight named certificates, per-mode breakdowns, bootstrap uncertainties, a
provenance-backed report" turn a half-day of code into a multi-round research artifact. If the user
wants a harness they will eyeball themselves, say so and keep it lean — and if the task is small
and well-understood, say plainly that doing it interactively may be the better call. A job earns
its overhead when independent verification is the *product*, not when it is ceremony.

## Yield, not price: what actually goes wrong

A run's failure mode is rarely "too expensive per round" (a derive round is millions of tokens by
nature). It is **spending the whole round budget on the wrong thing**. Recommend these:

- **Checkpoint.** A fresh job stops after 2 rounds for a human look; `job resume` continues.
  `--checkpoint 0` disables. Never let a first run of a new intent go 8 rounds unattended.
- **An acceptance gate the human writes, red before the run:**
  `--acceptance "cd {PROJECT} && pytest tests/test_acceptance.py" --acceptance-guard tests/test_acceptance.py`.
  The guarded file is hash-pinned, so the team can pass it but never edit it, and `[[DONE]]` is
  refused while it fails. Without one, "done" means only "cited and self-tested" — a team measured
  on citations will optimise citations.
- **Round count is a multiplier, not a target.** Prefer few rounds plus `job resume`.
- **Progress tripwires stop the run** at a round boundary when the verifier returns no claims, no
  new claim is verified for 2 rounds, the same task leads the plan 3 rounds running, a code job's
  project stops changing, or a round's spend blows past the running median. `state["stop_reason"]`
  and `job run`'s output say which. Resume if you disagree; don't disable them casually.
- Surplus tasks beyond `worker_count` are queued to `state["backlog"]`, not dropped — but the lead
  still plans best when the worker count matches the shape of the work.
- Post-mortems: `jobs/<id>/transcript/r<NN>-<role>.txt` holds every role call in full.

## Per-role depth (`--role`, repeatable)
Backend/model/effort are per **role**, not per job — so grind work runs shallow while the check
runs deep. Keys: `backend | model | effort | when`; anything unset falls back to the job default.
```
job new derive "..." --backend codex \
  --role worker:effort=medium \
  --role verifier:effort=xhigh,backend=claude \
  --role writer:when=last
```
- **A verifier on a different backend from the workers is a genuinely independent check** — a
  different model family re-deriving the claim, not the same model grading its own homework.
  Worth suggesting to the user for derive/draft jobs. A role that switches backend picks up that
  backend's default model; effort carries across.
- **`when`** ∈ `every` (default) | `first` | `last` | list of rounds (recipe files only). It
  schedules a recipe's `extra` roles — `writer:when=last` is one assembly pass at the end instead
  of a hand-off every round. `last` = the last round of *this run*, so it still fires on an early
  `[[DONE]]` or a spent budget. Lead and verifier run every round by design.
- Recipes can carry the same block as a default (`"roles": {...}` in `recipes/<name>.json`); the
  `--role` flags override it key by key.
- With `--pi`, the PI may also set per-role depth itself (`ROLE <role>: effort=...` lines),
  bounded by `policy.json` if present (`max_workers`, `effort_max`, `backends_allowed` are
  enforced). Policy bounds the PI only — the human's own flags are never clamped.
- A typo'd role or key fails at `job new`, before anything bills.

## Conventions
- Jobs are created in `./jobs/` relative to the current directory (the project you're in).
- The deliverable is in `jobs/<id>/out/` (tex/pdf/notebook/diff/html); the agents' sandbox is `work/`.
- Provenance is enforced for read-deliverables: every claim needs a `\src{key}` resolving in
  `out/provenance.json`; the check spine (incl. `out/checks.py`) blocks "done" while anything is
  unbacked.
- Each job's `spec.json` records backend/model/effort **per role** for provenance; `view.html`
  shows live state plus a Staffing table when the team isn't uniform; `log.jsonl` records every
  call (`role_call`: role, backend, model, effort, tokens). With mixed backends the job-level
  model line no longer says what produced a given claim — read the per-role rows.
- **Nothing stops a run mid-round.** The budget and the `.stop` kill-switch are checked *between*
  rounds, so no in-flight work is discarded and a job always lands easy to resume from. Don't
  propose mid-round budget cutoffs.
- The `verifier`/`code-reviewer` records the round by ending its reply with a ```` ```claims ````
  JSON block; a decoration-tolerant prose scan is the fallback. `VERIFIED` entries become durable
  state nobody re-derives — which is what keeps later rounds cheap — so a round that harvests no
  claims is logged as a broken contract and trips a stop.
- The `writer` **transcribes** that ledger; it does not verify. It is skipped in rounds where
  nothing new was verified, and may answer `NOOP`. If a write-up role is consuming a large share of
  a run, claim harvesting is broken upstream — look there first.
- `feature` jobs deliver `out/notes.tex` (+ `out/notes.pdf`) *and* `out/changes.diff` against the
  job's `base_commit`; the spine is `out/checks.sh`. Have the project under git first so the diff
  and the rollback point exist.

Weaving a finished job's deliverable into the wider project is a separate, human-directed step —
not automated.
