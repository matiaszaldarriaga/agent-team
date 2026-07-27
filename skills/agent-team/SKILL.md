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
off by default).

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
- Token counts on the `claude` backend currently **undercount badly** (cache tokens aren't
  counted; see `docs/BACKLOG.md` #7). Codex counts are real. Don't compare the two.

Weaving a finished job's deliverable into the wider project is a separate, human-directed step —
not automated.
