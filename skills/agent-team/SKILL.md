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

## Conventions
- Jobs are created in `./jobs/` relative to the current directory (the project you're in).
- The deliverable is in `jobs/<id>/out/` (tex/pdf/notebook/diff/html); the agents' sandbox is `work/`.
- Provenance is enforced for read-deliverables: every claim needs a `\src{key}` resolving in
  `out/provenance.json`; the check spine (incl. `out/checks.py`) blocks "done" while anything is
  unbacked.
- Each job's `spec.json` records backend/model/effort for provenance; `view.html` shows live state.

Weaving a finished job's deliverable into the wider project is a separate, human-directed step —
not automated.
