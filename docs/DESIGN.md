# agent-team — design & rationale

Why this tool is shaped the way it is. The README says *how* to use it; this says *why* it exists
and why the decisions went the way they did. Written 2026-07, distilling the redesign discussion.

## What it replaces

The predecessor was a multi-agent "bot research" system that accreted **three generations**:

1. **Root 5-bot loop** — a shell round-loop running literature/numerical/pen-and-paper bots plus a
   critic. Implicated in a runaway-cost postmortem (a forked scheduler firing thousands of
   unattended runs).
2. **PI + students** — an orchestrator assigning two parallel workers each round, coordinating
   through a `board.json` + per-bot YAML registries, viewed through a Flask dashboard.
3. **The "fable" single agent** — the newest work threw all of that out: one autonomous agent
   driven by three curated Markdown files (`START_HERE` / `ESTABLISHED` / `STATUS`) plus a
   re-runnable `checks.py`.

The drift across generations was toward **less scaffolding**, and that was the key signal.

## The core diagnosis

Two systems had been abandoned at *opposite* extremes:

- **PI + students** — too much autonomy; the human lost control and understanding mid-problem.
- **ai-workspace-framework** (a separate, heavier meta-framework) — too much ceremony: binding
  contracts, staged stops, mandatory handoff packets, a symlink fabric.

The common enemy was not "structure" — it was **friction and opacity**. The fable design won
because it minimized both. So the target for this tool is: keep durable memory + verification,
drop the ceremony and the opacity.

## What a stronger model changed

The original justification for a multi-agent loop was *reasoning breadth* — many agents plus a
critic beat one weak model. With current frontier models that premium shrank. But two things a
better model still does **not** give you, and the scaffold does:

1. **Durability** — a verified corpus that accumulates across many context windows and weeks.
2. **Verification discipline** — executable checks that stop the model believing its own
   plausible-but-wrong work.

So the tool optimizes for **persistence and verification, not "many minds at once."** Corollary:
start every problem with a one-shot; invoke the machine only when the problem outlives one context.

## Governing principles

1. **Control is a dial the project sets, not a mode baked in.** Objective-type work (a code
   feature, a formula) — delegate freely. Understanding-type work (a proof) — the human needs
   tight control and comprehension, not just a `SOLVED.md`. The dial also moves *within* a project
   (grind phase vs. "I need to understand this step"). The old sin was that once you launched the
   heavy loop you were stuck at max-orchestration with no cheap way back. Here the "dial" is just
   *which job you invoke and whether you invoke it again* — there is nothing to dial back down,
   because a job ends.
2. **Vendor-neutral.** Never married to Claude or Codex — they excel at different things, cost
   differently, and quotas run out. Orchestration sits *above* both CLIs (`backends.py`); nothing
   else in the system knows which model is behind a call. A harness (Claude Code / Codex) is a
   convenience, never the foundation.
3. **Disposable jobs per outcome.** Each run is a bounded team producing ONE deliverable in an
   isolated subtree (`jobs/<id>/`). Fire-and-forget from the terminal; nothing about its fate is
   automated — the human decides **resume(+direction) / freeze / abandon**.
4. **Verification is the spine.** A verifier is a permanent member of every team (`verifier` for
   math, `code-reviewer` + `test-writer` for code). A claim becomes durable "verified" state only
   once independently confirmed, so nobody re-verifies it later.
5. **Nothing is invented.** Provenance is enforced *executably*: every important statement in a
   deliverable carries a `\src{key}` resolving to `out/provenance.json`, which says how to
   reproduce it; the check spine blocks "done" while anything is unbacked. This was the single
   feature called crucial in the redesign.
6. **Human surfaces are not Markdown state files.** Reading substance: tex/pdf, notebooks.
   Monitoring + steering: a self-contained `view.html` per job (not a Flask dashboard). The human
   never hand-maintains a `.md` state file; agents keep machine state and render an HTML view.
7. **No scheduler, ever.** Bounded rounds + token budget + a `.stop` kill-switch + per-round cost
   logging + explicit model/effort in the spec. Nothing spawns background timers or self-restarts.
   (Directly from the runaway-cost postmortem.)

## Key decisions & why

| Decision | Rationale |
|---|---|
| **Three levels: `roles/` → `recipes/` → a job** | Personalities are reusable prompt files; recipes cast them into a team per job type; a job is one run. Separates "who they are" from "how they're cast" from "this run." |
| **State = one compact `state.json`, rendered to HTML** | The old per-bot append-only `claims.yaml` was the token-bloat problem: agents re-read unbounded registries on every wake-up. Here state is machine-owned, compacted (verified claims durable, stale ones dropped), and the human reads a rendered view, never raw yaml. |
| **Provenance as an executable check, not prose** | The old writer role *described* a provenance rule; here `check_provenance.py` enforces it and gates "done." Fits principle 4/5. |
| **PI staffing = plan-and-go, same artifact as manual** | "You staff it" and "the PI staffs it" both emit the same `spec.json`, so the runner has one code path. Plan-and-go by default (no approval gate); revisit only if it misbehaves. |
| **Discovery via the shared Agent Skills format** | A cloned repo is invisible to a fresh Claude/Codex session. One `SKILL.md` installed into `~/.claude/skills` and `~/.codex/skills` makes the tool discoverable everywhere, with human-initiated / confirm-before-spend / no-auto-launch guardrails encoded. |
| **Pure standard library, JSON configs** | Zero-dependency so it runs in any project it's dropped into; no install step for the runtime beyond `install.sh` wiring. |

## Deliberately dropped

- **The structured-argument registry** (old `logic.yaml`: thesis + ordered `argument_flow` +
  gaps + sensitivity). It is job-dependent — not every problem needs it — and it mainly fed a
  dashboard tab that went unused. The tex deliverable carries the argument for a human reader.
- **The Flask dashboard, `board.json`, hand-maintained `.md` state files, governance contracts,
  the symlink fabric.** All ceremony or opacity; replaced by `view.html` + `inbox.jsonl` + machine
  state.
- **Agent-to-agent message board.** Agents coordinate through the engine + shared state, not a
  posting protocol.

## Not in v0.1 (by design)

- **A shared, cross-job project corpus** (verified results shared across several jobs). Within a
  job, resume-memory is always on; a project-wide verified store is an optional layer to add only
  when an understanding-type job needs it — kept out of the core to avoid re-introducing ceremony.
- **A PI approval gate** (plan-and-go is the default). `policy.json` enforces `max_workers`,
  `effort_max` and `backends_allowed`; `budget_tokens_max` is still reserved.
- **Stopping a run mid-round.** The budget and the kill-switch are checked *between* rounds only,
  so no in-flight work is discarded and a job always lands in a state that's easy to resume from.
- **`draft` / `wiki`** are wired end-to-end but lightly specified; grow them from real use.

## Architecture map

```
bin/job                 launcher (Python entrypoint; put on PATH via install.sh)
agentteam/
  cli.py                parse verbs, dispatch
  jobs.py               a job = jobs/<id>/ : spec, state, inbox, lifecycle
  recipes.py            load a job type from recipes/*.json (team + deliverable)
  roles.py              load a personality from roles/*.md
  staffing.py           what each role runs on (backend/model/effort) and when it runs
  engine.py             the round loop: lead -> workers -> verifier -> extras -> checks
  backends.py           vendor-neutral shim: prompt -> codex / claude / mock call
  render.py             write view.html from state
  serve.py              optional browser monitor + inject server
bin/check_provenance.py the provenance gate (run by the check spine)
roles/*.md              the cast (pi, worker, verifier, code-reviewer, test-writer, writer)
recipes/*.json          the job types (derive, feature, draft, wiki)
skills/agent-team/      the discovery skill (Claude + Codex)
templates/view.html.tmpl the monitor page
install.sh              wire job onto PATH + skill into both agents' skill dirs
```

The one-line summary: **a portable, vendor-neutral way to fire a bounded, verifiable, disposable
team of agents at one outcome — optimized for persistence and verification, not for many minds,
and with the ceremony deliberately stripped out.**
