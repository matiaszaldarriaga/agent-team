# agent-team

A portable, vendor-neutral toolkit you drop into any project to **fire a bounded team of agents
into an isolated subtree to produce one deliverable** — a code feature, a draft, a derivation, a
wiki — and then stop. You decide what happens next: resume it with a nudge, freeze it, or abandon
it. Weaving the result into the wider project happens later, in an interactive session — not
automatically.

It runs on **Claude Code (`claude`) or Codex (`codex`) interchangeably**, chosen per job. Pure
Python standard library — no install, no dependencies.

## The three levels

```
roles/     the personalities — one prompt file per reusable role
           pi · worker · verifier · code-reviewer · test-writer · writer
recipes/   the job types — cast roles into a team + choreography + deliverable
           derive · feature · draft · wiki
a job      one run of a recipe, in jobs/<id>/, staffed by you or the PI
```

**`roles/` = who they are → `recipes/` = how they're cast into a team → a job = the concrete run.**
The verifier is a permanent member of every team: for math it's `verifier`, for code it's
`code-reviewer` + `test-writer`. A recipe with no verifier is rejected.

## Install

```sh
git clone git@github.com:matiaszaldarriaga/agent-team.git
cd agent-team && ./install.sh          # one-time; re-run after `git pull`
```

`install.sh` wires the **three consumers** of the tool:
- **you** → `job` symlinked onto your PATH (`~/bin`), runnable from any shell;
- **Claude** → the `agent-team` skill into `~/.claude/skills/`, discovered by every Claude session;
- **Codex** → the same skill into `~/.codex/skills/`, discovered by every Codex session.

Claude and Codex share the **Agent Skills** format, so one `SKILL.md` (`skills/agent-team/`) serves
both. Everything is symlinked, so `git pull` updates it in place. Cloning alone does none of this —
without the skill, a fresh Claude/Codex session has no idea the tool exists.

Run `job` from inside whatever project you're working on — jobs are created in `./jobs/` there;
the tool code stays in the cloned repo. (Point elsewhere with `AGENT_TEAM_PROJECT` /
`AGENT_TEAM_JOBS`.)

## Quickstart

Exercise the machinery offline, no spend, with the mock backend:

```sh
job new derive "warm-up" --backend mock --run --rounds 2
job status
```

A real derivation on Claude, staffed by the PI (plan-and-go), then continued with a steer:

```sh
job new derive "show the soft factor is 2(t_i+t_j)" --pi --run
open jobs/<id>/view.html                 # monitor + inject box
job resume <id> --say "focus on the resonant channel"
job freeze <id>                          # keep it; you weave it in later
```

On Codex instead: add `--backend codex`. Override the model/effort with `--model` / `--effort`
(both are recorded in the spec and shown in the view, for provenance).

## Lifecycle

```
job new <type> "<intent>" [--pi] [--run]     create (optionally PI-staffed / run now)
job run <id> [--rounds N]                    run up to N more rounds (default: the round budget)
job resume <id> --say "..." [--rounds N]     inject a direction and continue where it stopped
job say <id> "..."                           queue a direction (or use the HTML inject box)
job stop <id>                                kill-switch (checked each round)
job freeze <id> | abandon <id>               keep it / mark dead-end — its subtree stays as a record
job status [<id>]                            list jobs / dump one spec (● = live run + phase)
job watch <id>                               live-poll a job's phase until it ends
job serve [--port 8757]                      HTML monitor + inject server for all jobs
job roles | recipes                          list the installed cast / job types
```

Nothing about a job's fate is baked in. It runs bounded (by `rounds` and `budget_tokens`), stops,
and hands the decision to you.

## What a job looks like

```
jobs/<id>/
  spec.json    intent · team · backend/model/effort · budget · status   (machine)
  state.json   compact resumable "where I am" — verified claims, plan    (machine → rendered to HTML)
  view.html    self-contained monitor + inject page                      (you open this)
  out/         the deliverable you read (tex/pdf/notebook/diff/html)
  work/        the agents' sandbox
  inbox.jsonl  your injected directions          log.jsonl  per-round cost/events
  .stop        kill-switch sentinel
```

You only ever touch **`view.html`** (watch + steer) and **`out/`** (read the result).

## Each round

lead plans → workers execute in parallel → **verifier independently re-checks** (never trusts) →
extras author the deliverable (`writer`) / write tests (`test-writer`) → executable **checks**
run → `state.json` + `view.html` update, cost logged. A claim becomes durable *verified* state
only once the verifier confirms it — so nobody re-verifies it later (the "verified stays verified"
rule). For a `derive` job, drop a `out/checks.py`; it's run every round as the verification spine.

## Provenance: nothing is invented

For deliverables you read (`derive`, `draft`, `wiki`), every important statement must trace to a
recorded, reproducible place. The `writer` role keeps two artifacts in lockstep: the deliverable,
and `out/provenance.json` — a registry mapping each tagged claim to *how to reproduce it*.

- Tag claims in the deliverable: `\src{key}` (tex) or `[src:key]` (html/notebook).
- Each key has a registry entry: `{statement, type, reproduce, detail}`, where `type` is
  `check | script | data | source | derivation`. For `check`/`script`/`data`, `reproduce` is a
  file path whose existence is enforced (an executable check is the strongest provenance).
- `bin/check_provenance.py` runs as part of the check spine every round and **blocks `DONE`**
  while any tag is unbacked or any entry lacks a reproduce pointer. A statement with no
  reproducible backing is not written — it's recorded as an open point instead.

For code (`feature`) the analogue is tests: a change isn't done until `test-writer` has it covered
and passing.

## Staffing: you or the PI, same artifact

A job's team is written either by you (flags) or by the PI (`--pi`) — both produce the same
`spec.json`, so the runner has one code path. PI-staffed jobs are **plan-and-go** by default (no
approval gate; course-correct via the inbox). Copy `policy.example.json` → `policy.json` to bound
what the PI may do; in v0.1 `max_workers` is enforced (it clamps the PI's `WORKERS:` request), the
rest is reserved.

## Extending

- **New role:** add `roles/<name>.md`. It's immediately usable in any recipe's team.
- **New job type:** add `recipes/<name>.json` (see the schema in `agentteam/recipes.py`).
- **New backend:** add a branch in `agentteam/backends.py`; every recipe can then use it.

## Not in v0.1 (by design)

- A shared, cross-job *project corpus* (verified results shared across jobs) — jobs resume their
  own state; a project-wide store is an optional layer to add when an understanding-type job needs
  it.
- A PI approval gate (plan-and-go is the default) and full policy enforcement.
- `draft` / `wiki` are wired end-to-end but lightly specified; grow them from real use.

## Discipline (learned the hard way)

Bounded rounds only — **never a scheduler**. Every run has a round cap and a token budget, a
`.stop` kill-switch checked each round, per-round cost logging, and explicit model/effort in the
spec. Nothing here spawns background timers or self-restarts.

## Docs, tests, license

- **`docs/DESIGN.md`** — why the tool is shaped this way (the rationale and the decisions).
- **Tests** — `python -m unittest discover -s tests` (zero-dependency; uses the `mock` backend).
- **License** — MIT (`LICENSE`).
