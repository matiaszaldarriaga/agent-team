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
import json
import os
import re
import subprocess
from datetime import datetime

from . import HOME, backends, render, roles
from .jobs import Job, project_root


def _beat(job, spec, state, phase):
    """Heartbeat: record the current phase + timestamp and re-render, so the view (and
    `job status`/`job watch`) show live progress mid-round, not just at each round's end."""
    state["phase"] = phase
    state["heartbeat_ts"] = datetime.now().isoformat(timespec="seconds")
    job.save_state(state)
    job.save_spec(spec)
    render.render(job)


def _load_policy() -> dict:
    """Bounds for PI-staffed jobs, from policy.json next to the tool (optional)."""
    path = os.path.join(HOME, "policy.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def staff_with_pi(job: Job) -> None:
    """Plan-and-go PI staffing: the PI writes an opening brief and may size the worker pool.

    Produces the same artifact a human would: an updated spec + an initial plan in state.
    No approval gate (plan-and-go is the default); course-correct later via the inbox.
    """
    spec = job.load_spec()
    state = job.load_state()
    extra = (
        "You are staffing this job (plan-and-go: your plan runs immediately).\n"
        f"Available worker role: {spec['team']['workers'][0]}. "
        f"Current worker_count is {spec['worker_count']}.\n"
        "Write a short opening brief: the sub-goals, how to split them across workers, and the "
        "first concrete tasks. If a different number of parallel workers is clearly better, put a "
        "line `WORKERS: <n>` (1-6).")
    res = _run_role(job, spec, spec["team"]["lead"], "pi-staffing", state, [], extra)
    state["plan"] = res.text
    m = re.search(r"WORKERS:\s*([1-6])", res.text)
    if m:
        requested = int(m.group(1))
        cap = _load_policy().get("max_workers")
        spec["worker_count"] = min(requested, cap) if cap else requested
    _accumulate(spec, [res])
    job.save_spec(spec)
    job.save_state(state)
    render.render(job)


def run(job: Job, *, max_new_rounds: int | None = None) -> str:
    spec = job.load_spec()
    recipe = job.recipe()
    job.clear_stop()
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
        directions = job.drain_inbox(state)
        round_results = []
        _beat(job, spec, state, f"round {r} · planning")

        # 1-2. lead plans
        lead = spec["team"]["lead"]
        lead_res = _run_role(job, spec, lead, "lead", state, directions,
                             "Plan this round. Give each worker one concrete task as a numbered "
                             "list (`1.`, `2.`, ...). If the deliverable is complete AND its checks "
                             "pass, put `[[DONE]]` on its own line.")
        round_results.append(lead_res)
        state["plan"] = lead_res.text
        tasks = _parse_tasks(lead_res.text, spec["worker_count"])
        _beat(job, spec, state, f"round {r} · workers ({len(tasks)}) running")

        # 3. workers in parallel
        worker_role = spec["team"]["workers"][0]
        worker_results = _run_parallel(
            job, spec, worker_role, state, directions,
            [f"Your task this round (worker {i+1} of {len(tasks)}):\n{t}" for i, t in enumerate(tasks)])
        round_results.extend(worker_results)

        # 4. verifier -- independent, adversarial
        _beat(job, spec, state, f"round {r} · verifying")
        claims_blob = "\n\n".join(f"[worker-{i+1}] {wr.text}" for i, wr in enumerate(worker_results))
        ver_res = _run_role(job, spec, spec["team"]["verifier"], "verifier", state, directions,
                            "Independently verify each worker claim below. Do NOT trust it -- "
                            "re-derive or re-run it yourself. For each, output a line "
                            "`VERIFIED: ...`, `REFUTED: ...`, or `UNCLEAR: ...` with a one-line "
                            "reason.\n\n" + claims_blob)
        round_results.append(ver_res)

        # 5. extras (writer updates deliverable, test-writer, ...)
        for extra_role in spec["team"].get("extra", []):
            _beat(job, spec, state, f"round {r} · {extra_role}")
            ex_res = _run_role(job, spec, extra_role, extra_role, state, directions,
                               _extra_task(extra_role, spec))
            round_results.append(ex_res)

        # record round (compact) + verified claims
        _record_round(state, r, worker_results, ver_res)

        # 6. executable checks
        _beat(job, spec, state, f"round {r} · checks")
        state["checks"] = _run_checks(job, spec)

        # 7. persist + render + log
        _accumulate(spec, round_results)
        state["round"] = r
        job.save_state(state)
        job.save_spec(spec)
        render.render(job)
        job.log({"event": "round", "round": r, "cost_usd": round(spec["cost_usd"], 4),
                 "tokens": spec["tokens"], "checks_passed": state["checks"].get("passed")})

        if "[[DONE]]" in lead_res.text and state["checks"].get("passed", True):
            spec["status"] = "done"
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

def _run_parallel(job, spec, role, state, directions, extras):
    if len(extras) == 1:
        return [_run_role(job, spec, role, "worker-1", state, directions, extras[0])]
    results = [None] * len(extras)
    with cf.ThreadPoolExecutor(max_workers=min(len(extras), 6)) as ex:
        futs = {ex.submit(_run_role, job, spec, role, f"worker-{i+1}", state, directions, e): i
                for i, e in enumerate(extras)}
        for fut in cf.as_completed(futs):
            results[futs[fut]] = fut.result()
    return results


def _run_role(job, spec, role_name, label, state, directions, extra_task) -> backends.AgentResult:
    prompt = _build_prompt(job, spec, role_name, state, directions, extra_task)
    idle = spec.get("idle_timeout")
    if idle is None:
        idle = backends.DEFAULT_IDLE_TIMEOUT
    res = backends.run_agent(prompt, backend=spec["backend"], model=spec.get("model"),
                             effort=spec.get("effort"), cwd=job.dir,
                             timeout=spec.get("timeout"), idle_timeout=idle)
    if not res.ok:
        job.log({"event": "agent_error", "role": label, "error": res.error})
    elif res.note:
        job.log({"event": "agent_note", "role": label, "note": res.note})
    return res


def _build_prompt(job, spec, role_name, state, directions, extra_task) -> str:
    role_md = roles.load(role_name)
    verified = [c for c in state.get("claims", []) if c.get("status") == "verified"][-12:]
    verified_txt = "\n".join(f"- {c['text']}" for c in verified) or "- (none yet)"
    checks = state.get("checks") or {}
    checks_txt = ("not configured" if not checks else
                  ("PASSED" if checks.get("passed") else f"FAILED: {checks.get('detail', '')[:300]}"))
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
        "\nVerified so far (trust these; do NOT re-derive):\n" + verified_txt,
        f"\nExecutable checks: {checks_txt}",
        "\nHuman direction (TOP PRIORITY):\n" + direction_txt,
        "\n# YOUR TASK THIS ROUND\n" + extra_task,
    ]
    return "\n".join(lines)


def _extra_task(role, spec):
    if role == "writer":
        return (f"Update the deliverable {spec['deliverable']['path']} so it reflects the current "
                "verified state. Write only what is verified; mark open points explicitly.")
    if role == "test-writer":
        return ("Write or extend tests that exercise this round's changes, then run them. Report "
                "pass/fail; the changes are not done until tests pass.")
    return "Do your role's standard job for this round given the context above."


# --- helpers -----------------------------------------------------------------

def _parse_tasks(lead_text, n):
    tasks = re.findall(r"^\s*\d+[.)]\s+(.*)$", lead_text, flags=re.MULTILINE)
    tasks = [t.strip() for t in tasks if t.strip()]
    if not tasks:
        tasks = ["Advance the intent per the latest plan; report concrete results."]
    # pad/truncate to n workers
    if len(tasks) < n:
        tasks += [tasks[-1]] * (n - len(tasks))
    return tasks[:n]


def _record_round(state, r, worker_results, ver_res):
    for line in ver_res.text.splitlines():
        m = re.match(r"\s*(VERIFIED|REFUTED|UNCLEAR)\s*[:\-]\s*(.+)", line, re.IGNORECASE)
        if m:
            state.setdefault("claims", []).append(
                {"round": r, "status": m.group(1).lower().replace("verified", "verified"),
                 "text": m.group(2).strip()[:400]})
    # keep the claims list bounded (compaction: verified are durable, drop stale unclear/refuted)
    claims = state.get("claims", [])
    verified = [c for c in claims if c["status"] == "verified"]
    recent_other = [c for c in claims if c["status"] != "verified"][-20:]
    state["claims"] = verified[-60:] + recent_other
    state.setdefault("rounds_log", []).append(
        {"round": r, "workers": len(worker_results), "verifier": ver_res.text[:600]})
    state["rounds_log"] = state["rounds_log"][-30:]


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


def _finalize_deliverable(job, spec):
    """Ensure the deliverable exists; for code jobs, capture the project diff if it's a git repo."""
    d = spec["deliverable"]
    path = os.path.join(job.dir, d["path"])
    if d["type"] == "diff" and spec["kind"] == "code":
        try:
            proc = subprocess.run(["git", "diff"], cwd=project_root(), capture_output=True,
                                  text=True, timeout=60)
            if proc.returncode == 0 and proc.stdout.strip():
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(proc.stdout)
        except (OSError, subprocess.SubprocessError):
            pass
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()


def _accumulate(spec, results):
    spec["cost_usd"] = spec.get("cost_usd", 0.0) + sum(r.cost_usd for r in results)
    spec["tokens"] = spec.get("tokens", 0) + sum(r.tokens for r in results)


def _budget_exceeded(spec):
    budget = spec.get("budget_tokens", 0)
    return budget and spec.get("tokens", 0) >= budget
