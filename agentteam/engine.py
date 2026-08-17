"""The round engine: one generic choreography, parameterised by the recipe.

Each round:
  1. drain the inbox (human directions -- top priority)
  2. LEAD plans the round and assigns tasks
  3. WORKERS execute their tasks in parallel (in the job's isolated subtree)
  4. VERIFIER independently checks the workers' claims -- never trusts, re-derives/re-runs
  5. EXTRAS run (e.g. writer updates the tex deliverable; test-writer writes tests)
  6. executable CHECKS run (the verification spine: a claim is trusted iff its check passes)
  7. state.json + view.html are updated; per-round cost is logged

Bounded by ``rounds`` and ``budget_tokens``; interruptible by the ``.stop`` kill-switch.
Nothing decides the job's fate automatically -- when the loop ends you resume / freeze / abandon.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime

from . import HOME, backends, claims as claims_mod, render, roles, staffing, tripwires
from .jobs import Job, project_root


def _beat(job, spec, state, phase):
    """Heartbeat: record the current phase + timestamp and re-render, so the view (and
    `job status`/`job watch`) show live progress mid-round, not just at each round's end."""
    state["phase"] = phase
    state["heartbeat_ts"] = datetime.now().isoformat(timespec="seconds")
    job.save_state(state)
    job.save_spec(spec)
    render.render(job)


def staff_with_pi(job: Job) -> None:
    """Plan-and-go PI staffing: the PI writes an opening brief, may size the worker pool, and
    may set the depth each role runs at (bounded by policy.json).

    Produces the same artifact a human would: an updated spec + an initial plan in state.
    No approval gate (plan-and-go is the default); course-correct later via the inbox.
    """
    spec = job.load_spec()
    state = job.load_state()
    policy = staffing.load_policy()
    extra = (
        "You are staffing this job (plan-and-go: your plan runs immediately).\n"
        f"Available worker role: {spec['team']['workers'][0]}. "
        f"Current worker_count is {spec['worker_count']}.\n"
        "Write a short opening brief: the sub-goals, how to split them across workers, and the "
        "first concrete tasks. If a different number of parallel workers is clearly better, put a "
        "line `WORKERS: <n>` (1-6).\n"
        + _staffing_menu(spec, policy))
    res = _run_role(job, spec, spec["team"]["lead"], "pi-staffing", state, [], extra)
    state["plan"] = res.text
    state["opening_brief"] = res.text     # round 1 overwrites `plan`; the brief must survive
    m = re.search(r"WORKERS:\s*([1-6])", res.text)
    if m:
        requested = int(m.group(1))
        cap = policy.get("max_workers")
        spec["worker_count"] = min(requested, cap) if cap else requested
    _apply_pi_roles(job, spec, res.text, policy)
    _accumulate(spec, [res])
    job.save_spec(spec)
    job.save_state(state)
    render.render(job)


def _staffing_menu(spec, policy) -> str:
    """Offer the PI the per-role depth dial, listing only what policy.json actually permits."""
    return (
        "\nYou may also set the depth each role runs at -- shallow for grind work, deep for the "
        "verifier. For each role you want to change from the job default, put a line:\n"
        "  `ROLE <role>: effort=<effort>[, backend=<backend>]`\n"
        f"Roles on this team: {', '.join(staffing.team_roles(spec['team']))}.\n"
        f"Effort, cheap -> deep: {', '.join(staffing.allowed_efforts(policy))}. "
        f"Backends: {', '.join(staffing.allowed_backends(policy))}.\n"
        f"Omit a role to leave it at the job default ({spec.get('backend')} / "
        f"{spec.get('effort') or 'backend default'}). Raising effort costs real time and money -- "
        "give a one-clause reason for each raise. Putting the verifier on a *different* backend "
        "from the workers buys genuine independence; consider it.")


def _apply_pi_roles(job, spec, text, policy):
    """Fold the PI's `ROLE ...` lines into the spec, clamped by policy.json.

    Lenient by design: a malformed or out-of-team line is dropped and logged, never fatal --
    the staffing call already cost money, and one bad line shouldn't waste the rest of it.
    """
    proposed = staffing.parse_pi(text)
    if not proposed:
        return
    kept, rejected = {}, []
    for role, cfg in proposed.items():
        try:
            kept.update(staffing.normalize({role: cfg}, spec["team"], source="PI staffing"))
        except ValueError as err:
            rejected.append(str(err))
    clamped, notes = staffing.clamp(kept, policy)
    if clamped:
        spec["roles"] = staffing.merge(spec.get("roles"), clamped)
    if clamped or notes or rejected:
        job.log({"event": "pi_roles", "applied": clamped, "clamped": notes, "rejected": rejected})


def run(job: Job, *, max_new_rounds: int | None = None) -> str:
    spec = job.load_spec()
    recipe = job.recipe()
    job.clear_stop()
    opening = job.load_state()
    prev_stop = opening.pop("stop_reason", None)
    if prev_stop:  # not still stopped for the old reason -- but the team must be TOLD it happened
        opening["last_stop_reason"] = prev_stop
        job.save_state(opening)
    spec["status"] = "running"
    job.save_spec(spec)
    job.write_pid()

    start = spec.get("round", 0)
    hard_limit = spec["rounds"]
    limit = hard_limit if max_new_rounds is None else min(hard_limit, start + max_new_rounds)

    while spec["round"] < limit:
        if job.stopped():
            spec["status"] = "stopped"
            job.log({"event": "stopped_by_switch", "round": spec["round"]})
            break
        if _budget_exceeded(spec):
            spec["status"] = "stopped"
            job.log({"event": "budget_exceeded", "tokens": spec["tokens"]})
            break

        spec["round"] += 1
        r = spec["round"]
        state = job.load_state()
        directions, new_directions = job.drain_inbox(state)
        _beat(job, spec, state, f"round {r} · planning")

        # 1-2. lead plans
        round_start_tokens = spec.get("tokens", 0)
        lead = spec["team"]["lead"]
        lead_res = _run_role(job, spec, lead, "lead", state, directions,
                             _lead_task(spec, state), new_directions)
        _accumulate(spec, [lead_res])
        state["plan"] = lead_res.text
        tasks, deferred = _plan_tasks(lead_res.text, spec["worker_count"], state.get("backlog"))
        state["backlog"] = deferred
        if deferred:
            job.log({"event": "tasks_deferred", "round": r, "assigned": len(tasks),
                     "deferred": deferred})
        _beat(job, spec, state, f"round {r} · workers ({len(tasks)}) running")

        # 3. workers in parallel
        worker_role = spec["team"]["workers"][0]
        worker_results = _run_parallel(
            job, spec, worker_role, state, directions,
            [f"Your task this round (worker {i+1} of {len(tasks)}):\n{t}" for i, t in enumerate(tasks)],
            new_directions)
        _accumulate(spec, worker_results)

        # 4. verifier -- independent, adversarial
        _beat(job, spec, state, f"round {r} · verifying")
        claims_blob = "\n\n".join(f"[worker-{i+1}] {wr.text}" for i, wr in enumerate(worker_results))
        ver_res = _run_role(job, spec, spec["team"]["verifier"], "verifier", state, directions,
                            "Independently verify each worker claim below. Do NOT trust it -- "
                            "re-derive or re-run it yourself.\n\n"
                            + claims_mod.BLOCK_INSTRUCTION + "\n\n" + claims_blob,
                            new_directions)
        _accumulate(spec, [ver_res])
        if claims_mod.unharvested(ver_res.text):
            job.log({"event": "claims_unharvested", "round": r,
                     "mentions": claims_mod.mentions(ver_res.text)})

        # 5. extras (writer updates deliverable, test-writer, ...), each on its own `when`
        #    schedule. `last` fires on the final round of THIS run -- the round budget is spent,
        #    the lead signalled done, or the budget/kill-switch is about to end it -- so a
        #    write-up-at-the-end role still gets its pass when a job stops early.
        final = (done_signal(lead_res) or r >= limit
                 or _budget_exceeded(spec) or job.stopped())
        # record round (compact) + verified claims -- before the extras, so the writer is handed
        # this round's ledger rather than having to go establish it for itself
        harvest = _record_round(state, r, worker_results, ver_res)

        for extra_role in spec["team"].get("extra", []):
            if not staffing.runs_in_round(spec, extra_role, round_no=r, final=final):
                continue
            if extra_role == "writer" and not _writer_has_work(state, final):
                job.log({"event": "writer_skipped", "round": r,
                         "reason": "no new verified claims since its last pass"})
                continue
            _beat(job, spec, state, f"round {r} · {extra_role}")
            ex_res = _run_role(job, spec, extra_role, extra_role, state, directions,
                               _extra_task(extra_role, spec), new_directions)
            _accumulate(spec, [ex_res])
            if extra_role == "writer":
                state["writer_seen_verified"] = _verified_count(state)

        # 6. executable checks, then the human's acceptance gate (which the team cannot edit)
        _beat(job, spec, state, f"round {r} · checks")
        state["checks"] = _run_checks(job, spec)
        state["acceptance"] = _run_acceptance(job, spec)

        # 7. persist + render + log
        state["round"] = r
        tripwires.record(state, round_no=r, claims=harvest["claims"],
                         new_verified=harvest["new_verified"], tasks=tasks,
                         tokens=spec.get("tokens", 0) - round_start_tokens,
                         checks_passed=state["checks"].get("passed"),
                         fingerprint=_project_fingerprint(spec))
        job.save_state(state)
        job.save_spec(spec)
        render.render(job)
        job.log({"event": "round", "round": r, "cost_usd": round(spec["cost_usd"], 4),
                 "tokens": spec["tokens"], "checks_passed": state["checks"].get("passed")})

        if done_signal(lead_res) and state["checks"].get("passed", True) \
                and state["acceptance"].get("passed", True):
            spec["status"] = "done"
            break

        tripped = ("the lead signalled [[BLOCKED]] -- it cannot make progress without a human"
                   if blocked_signal(lead_res) else tripwires.evaluate(spec, state))
        if tripped:
            spec["status"] = "stopped"
            state["stop_reason"] = tripped
            job.save_state(state)
            job.log({"event": "tripwire", "round": r, "reason": tripped})
            break
    else:
        spec["status"] = "stopped"  # hit the round limit without a DONE

    _finalize_deliverable(job, spec)
    final_state = job.load_state()
    final_state["phase"] = spec["status"]          # terminal phase shown in the view
    job.save_state(final_state)
    job.save_spec(spec)
    job.clear_pid()                                 # process is finishing -> not running
    render.render(job)
    return spec["status"]


# --- role invocation ---------------------------------------------------------

def _run_parallel(job, spec, role, state, directions, extras, new_directions=0):
    if len(extras) == 1:
        return [_run_role(job, spec, role, "worker-1", state, directions, extras[0],
                          new_directions)]
    results = [None] * len(extras)
    with cf.ThreadPoolExecutor(max_workers=min(len(extras), 6)) as ex:
        futs = {ex.submit(_run_role, job, spec, role, f"worker-{i+1}", state, directions, e,
                          new_directions): i
                for i, e in enumerate(extras)}
        for fut in cf.as_completed(futs):
            results[futs[fut]] = fut.result()
    return results


def done_signal(lead_res) -> bool:
    return "[[DONE]]" in lead_res.text


def blocked_signal(lead_res) -> bool:
    """The lead's escape hatch: it can hand the job back instead of spinning out the budget."""
    return "[[BLOCKED]]" in lead_res.text


def _run_role(job, spec, role_name, label, state, directions, extra_task,
              new_directions=0) -> backends.AgentResult:
    os.makedirs(job.reports_dir, exist_ok=True)   # the role writes its report into it
    prompt = _build_prompt(job, spec, role_name, state, directions, extra_task,
                           label=label, new_directions=new_directions)
    cfg = staffing.resolve(spec, role_name)  # this role's backend/model/effort, not the job's
    idle = spec.get("idle_timeout")
    if idle is None:
        idle = backends.DEFAULT_IDLE_TIMEOUT
    res = backends.run_agent(prompt, backend=cfg["backend"], model=cfg["model"],
                             effort=cfg["effort"], cwd=job.dir,
                             timeout=spec.get("timeout"), idle_timeout=idle)
    job.save_transcript(spec.get("round", 0), label, res.text, header={
        "role": role_name, "label": label, "round": spec.get("round", 0),
        "tokens": res.tokens, "ok": res.ok, **cfg})
    job.log({"event": "role_call", "role": label, "round": spec.get("round", 0),
             **cfg, "tokens": res.tokens, "ok": res.ok})
    if not res.ok:
        job.log({"event": "agent_error", "role": label, "error": res.error})
    elif res.note:
        job.log({"event": "agent_note", "role": label, "note": res.note})
    return res


def _gate_txt(d, name):
    if not d:
        return f"{name}: not configured"
    return (f"{name}: PASSED" if d.get("passed")
            else f"{name}: FAILED -- {(d.get('detail') or '')[:1200]}")


def _ledger_index(job, spec, state, directions, new_directions) -> str:
    """A short INDEX of the ledger plus an order to read it -- not a copy of it.

    The engine used to paste a filtered slice of state into every prompt: the last 12 verified
    claims and nothing else. Everything a role most needed was therefore invisible to it --
    what the verifier refuted, what the human's acceptance gate says, why the last run stopped,
    what any other role actually did. The whole ledger is tens of kilobytes and the roles are
    agents with file tools sitting in the job directory, so pasting a lossy summary costs more
    than reading the real thing. This function tells them what is there and what the counts
    are; the files carry the substance.
    """
    claims = state.get("claims", [])
    by = {s: sum(1 for c in claims if c.get("status") == s)
          for s in ("verified", "refuted", "unclear")}
    open_n = by["refuted"] + by["unclear"]
    prev = spec.get("round", 1) - 1
    prev_reports = job.reports_of_round(prev) if prev >= 1 else []
    L = [
        "\n---\n# THE LEDGER -- read the files, do not work from these counts",
        "Your working directory IS the job directory. Every path below is relative to it, and "
        "all of them are small. Read them before you do anything else:",
        "  spec.json     the contract: the full intent, team, deliverable, check command",
        "  state.json    the ledger: EVERY claim with its status and round, the current plan, "
        "the backlog, the check and acceptance results, the round history",
        "  inbox.jsonl   every human direction ever sent. They do not expire",
        "  reports/      what each role wrote, per round: r<NN>-<label>.md",
        "",
        "Index -- counts only:",
        f"  claims        {len(claims)}: {by['verified']} verified, {by['refuted']} refuted, "
        f"{by['unclear']} unclear",
    ]
    if open_n:
        L.append(f"  UNRESOLVED    {open_n} claims are refuted or unclear. Each one is your "
                 f"responsibility: resolve it, or state in your report why it stands. They are "
                 f"in state.json under `claims`; the reasoning behind them is in "
                 f"reports/ and transcript/")
    L += [
        f"  directions    {len(directions)} in inbox.jsonl ({new_directions} new this round); "
        f"all of them remain in force",
        f"  {_gate_txt(state.get('checks'), 'checks      ')}",
        f"  {_gate_txt(state.get('acceptance'), 'acceptance  ')}",
    ]
    if state.get("last_stop_reason"):
        L.append(f"  last stop     the previous run stopped: {state['last_stop_reason']}")
    if state.get("backlog"):
        L.append(f"  backlog       {len(state['backlog'])} task(s) queued from earlier rounds")
    L.append(f"  reports       last round left: "
             f"{', '.join(prev_reports) if prev_reports else '(none)'}")
    L.append("")
    L.append("Also present, larger, read only what you need: transcript/ (every role call in "
             "full), work/ (the sandbox), out/ (the deliverable).")
    return "\n".join(L)


def _report_task(spec, label) -> str:
    return (
        f"\n\n# YOUR REPORT -- required, and part of the task\n"
        f"Before you finish, write `reports/r{spec.get('round', 0):02d}-{label}.md`: what you "
        f"did, what you found, the numbers, and what you could NOT settle. If you are "
        f"addressing something previously refuted or left unclear, say which and how.\n"
        f"This file is what the other roles read next round. Your reply text is not carried "
        f"forward -- only this file and the ledger are. Keep it factual and short; it is a "
        f"record, not an essay.")


def _build_prompt(job, spec, role_name, state, directions, extra_task,
                  label=None, new_directions=0) -> str:
    role_md = roles.load(role_name)
    direction_txt = "\n".join(f"- {d}" for d in directions) or "- (none)"
    lines = [
        f"# ROLE: {role_name}",
        role_md.strip(),
        "\n---\n# JOB CONTEXT",
        f"Job type: {spec['type']} ({spec['kind']})",
        f"Intent: {spec['intent']}",
        f"Round: {spec['round']} of {spec['rounds']}",
        f"Your working directory is this job dir. Sandbox: work/. "
        f"Deliverable: {spec['deliverable']['path']} (type {spec['deliverable']['type']}).",
    ]
    if spec["kind"] == "code":
        lines.append(f"Project under change: {project_root()} -- edit files there directly; "
                     f"summarise changes into the deliverable.")
    lines += [
        "\nLatest plan:\n" + (state.get("plan") or "(none)"),
        _ledger_index(job, spec, state, directions, new_directions),
        "\nHuman direction (TOP PRIORITY -- every one of these is still in force):\n"
        + direction_txt,
        "\n# YOUR TASK THIS ROUND\n" + extra_task,
    ]
    if label:
        lines.append(_report_task(spec, label))
    return "\n".join(lines)


def _lead_task(spec, state):
    """The lead's brief -- including how many workers it actually has.

    It used to be asked for "a task per worker" without ever being told the worker count, while
    the engine silently kept only the first ``worker_count`` items. A lead planning for a team
    that no longer exists loses most of its plan every round, which is exactly how real work
    (build the next module) can be re-queued for eight rounds and executed zero times.
    """
    n = spec["worker_count"]
    backlog = state.get("backlog") or []
    lines = [
        f"Plan this round. You have exactly {n} worker(s) available, so give at most {n} "
        f"concrete task(s) as a numbered list (`1.`, `2.`, ...) -- one per worker.",
        "Anything beyond that is queued and run in a later round, not this one, so put the task "
        "that most advances the intent first.",
        "Before planning, compare the intent's required deliverables against what actually "
        "exists. If bookkeeping keeps crowding out the substance, say so and reorder.",
        "If the deliverable is complete AND its checks pass, put `[[DONE]]` on its own line. "
        "If you cannot make progress and need the human, put `[[BLOCKED]]` on its own line.",
    ]
    if backlog:
        queued = "\n".join(f"  - {t}" for t in backlog[:6])
        lines.append(f"Queued from earlier rounds (these run first unless you re-order "
                     f"them):\n{queued}")
    return "\n".join(lines)


def _extra_task(role, spec):
    if role == "writer":
        return (
            f"Transcribe the verified ledger above into {spec['deliverable']['path']}, and keep "
            "the provenance registry in step with it.\n"
            "You are NOT a verifier: do not re-derive, re-run tests, or re-establish what is "
            "already recorded as verified. Anything not in the ledger is an open point, not a "
            "claim -- write it under open points and move on.\n"
            "Extend and refine what is already written rather than rewriting it. If nothing has "
            "been verified since your last pass, change nothing and reply `NOOP`.")
    if role == "test-writer":
        return ("Write or extend tests that exercise this round's changes, then run them. Report "
                "pass/fail; the changes are not done until tests pass. Never write a test that "
                "asserts an intended deliverable is still missing -- that pins the gap instead of "
                "closing it. Record gaps as open points, not as passing tests.")
    return "Do your role's standard job for this round given the context above."


# --- helpers -----------------------------------------------------------------

def _plan_tasks(lead_text, n, backlog=None):
    """``(assigned, deferred)``: the backlog runs first, and surplus tasks are kept, not dropped.

    Truncating the lead's plan to the worker count silently discarded whatever came after item
    ``n``, every round. Now the remainder is carried to the next round, so over-planning costs a
    round of latency instead of the work itself.
    """
    parsed = [t.strip() for t in re.findall(r"^\s*\d+[.)]\s+(.*)$", lead_text, flags=re.MULTILINE)]
    parsed = [t for t in parsed if t]
    queue, seen = [], set()
    for task in list(backlog or []) + parsed:
        key = re.sub(r"\s+", " ", task).strip().lower()
        if key and key not in seen:
            seen.add(key)
            queue.append(task)
    if not queue:
        queue = ["Advance the intent per the latest plan; report concrete results."]
    return queue[:max(1, n)], queue[max(1, n):]


def _record_round(state, r, worker_results, ver_res):
    """Harvest the round's claims into durable state; return what was found."""
    before = _verified_count(state)
    harvested = claims_mod.parse(ver_res.text)
    for entry in harvested:
        state.setdefault("claims", []).append(
            {"round": r, "status": entry["status"], "text": entry["text"]})
    # keep the claims list bounded (compaction: verified are durable, drop stale unclear/refuted)
    claims = state.get("claims", [])
    verified = [c for c in claims if c["status"] == "verified"]
    recent_other = [c for c in claims if c["status"] != "verified"][-20:]
    state["claims"] = verified[-60:] + recent_other
    state.setdefault("rounds_log", []).append(
        {"round": r, "workers": len(worker_results), "verifier": ver_res.text[:6000]})
    state["rounds_log"] = state["rounds_log"][-30:]
    return {"claims": len(harvested), "new_verified": max(0, _verified_count(state) - before)}


def _verified_count(state):
    return sum(1 for c in state.get("claims", []) if c.get("status") == "verified")


def _writer_has_work(state, final) -> bool:
    """The writer transcribes verified state, so with nothing newly verified it has nothing to do.

    Running it anyway is not harmless: handed an empty ledger it goes and re-establishes the
    verified state itself, at full depth, over a document that grows every round -- which is how
    a write-up role can end up consuming most of a run.
    """
    if final:
        return True
    return _verified_count(state) > state.get("writer_seen_verified", 0)


def _run_checks(job, spec):
    cmd = (spec.get("checks") or {}).get("command", "")
    if not cmd:
        return {}
    cmd = cmd.replace("{TOOL}", HOME).replace("{JOB}", job.dir)  # resolve tool/job paths
    try:
        proc = subprocess.run(cmd, cwd=job.dir, shell=True, capture_output=True,
                              text=True, timeout=600)
        passed = proc.returncode == 0
        detail = (proc.stdout + proc.stderr).strip()[-2000:]
        return {"command": cmd, "passed": passed, "detail": detail}
    except subprocess.TimeoutExpired:
        return {"command": cmd, "passed": False, "detail": "checks timed out (600s)"}


def _run_acceptance(job, spec):
    """The human's acceptance gate: red before the run, and the team may not edit it.

    ``checks`` are written by the team, so they can only ever say "what we claimed is cited and
    our own tests pass" -- they cannot say "this is what the human asked for". The acceptance
    command points at a file the human wrote in advance; its hash is pinned at creation, so the
    only way past it is to make it pass. ``[[DONE]]`` is refused while it fails.
    """
    cfg = spec.get("acceptance") or {}
    cmd = cfg.get("command")
    if not cmd:
        return {}
    tampered = [p for p, want in (cfg.get("files") or {}).items()
                if _sha256(_abs_in_project(p)) != want]
    if tampered:
        return {"command": cmd, "passed": False, "tampered": tampered,
                "detail": ("acceptance file(s) modified since the job was created: "
                           + ", ".join(tampered) + " -- the gate may be passed, never edited")}
    cmd_r = cmd.replace("{TOOL}", HOME).replace("{JOB}", job.dir).replace(
        "{PROJECT}", project_root())
    try:
        proc = subprocess.run(cmd_r, cwd=job.dir, shell=True, capture_output=True,
                              text=True, timeout=1800)
        return {"command": cmd_r, "passed": proc.returncode == 0,
                "detail": (proc.stdout + proc.stderr).strip()[-2000:]}
    except subprocess.TimeoutExpired:
        return {"command": cmd_r, "passed": False, "detail": "acceptance timed out (1800s)"}


def _abs_in_project(path):
    return path if os.path.isabs(path) else os.path.join(project_root(), path)


def _sha256(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _project_fingerprint(spec):
    """A hash of the project's change set, so "nothing was built" is detectable."""
    if spec.get("kind") != "code":
        return None
    try:
        subprocess.run(["git", "add", "-N", "."], cwd=project_root(),
                       capture_output=True, text=True, timeout=60)
        cmd = ["git", "diff"] + ([spec["base_commit"]] if spec.get("base_commit") else [])
        proc = subprocess.run(cmd, cwd=project_root(), capture_output=True,
                              text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return hashlib.sha256(proc.stdout.encode("utf-8", "replace")).hexdigest()


def _finalize_deliverable(job, spec):
    """Ensure the deliverable exists; for code jobs, always capture the project change set too."""
    d = spec["deliverable"]
    path = os.path.join(job.dir, d["path"])
    if spec["kind"] == "code":
        diff_path = path if d["type"] == "diff" else os.path.join(job.out_dir, "changes.diff")
        _capture_project_diff(diff_path, spec.get("base_commit"))
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
    if d["type"] == "tex":
        _compile_pdf(path)


def _compile_pdf(tex_path):
    """Leave a readable PDF next to the tex. A deliverable the human cannot open isn't one.

    Built in a scratch directory and copied back, so a failed or page-less compile leaves no
    ``.aux``/``.log`` litter in ``out/`` -- what lands there is read, and increasingly committed.
    """
    if not shutil.which("pdflatex") or not os.path.getsize(tex_path):
        return
    build = tempfile.mkdtemp(prefix="agentteam_tex_")
    try:
        for _ in range(2):  # twice, so \ref/\label and the ToC settle
            # cwd = the tex file's own directory: \includegraphics{figures/...} resolves against
            # the *working* directory, not the source file, so a deliverable with figures silently
            # fails to build from anywhere else.
            subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                            "-output-directory", build, os.path.basename(tex_path)],
                           cwd=os.path.dirname(os.path.abspath(tex_path)),
                           capture_output=True, text=True, timeout=300)
        built = os.path.join(build, os.path.splitext(os.path.basename(tex_path))[0] + ".pdf")
        if os.path.exists(built) and os.path.getsize(built):
            shutil.copyfile(built, os.path.splitext(tex_path)[0] + ".pdf")
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        shutil.rmtree(build, ignore_errors=True)


def _capture_project_diff(path, base_commit):
    """Write the project's change set to ``path`` (best effort; silent if not a git repo).

    A code job's output is mostly *new* files, which a bare ``git diff`` never shows -- so mark
    untracked files intent-to-add first (index-only, no content staged). Diffing against the job's
    ``base_commit`` captures work the agents committed themselves as well as what they left in the
    working tree.
    """
    try:
        subprocess.run(["git", "add", "-N", "."], cwd=project_root(), capture_output=True,
                       text=True, timeout=60)
        cmd = ["git", "diff"] + ([base_commit] if base_commit else [])
        proc = subprocess.run(cmd, cwd=project_root(), capture_output=True, text=True, timeout=120)
        if proc.returncode == 0 and proc.stdout.strip():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(proc.stdout)
    except (OSError, subprocess.SubprocessError):
        pass


def _accumulate(spec, results):
    spec["cost_usd"] = spec.get("cost_usd", 0.0) + sum(r.cost_usd for r in results)
    spec["tokens"] = spec.get("tokens", 0) + sum(r.tokens for r in results)


def _budget_exceeded(spec):
    budget = spec.get("budget_tokens", 0)
    return budget and spec.get("tokens", 0) >= budget
