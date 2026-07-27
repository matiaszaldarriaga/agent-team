"""A job = an isolated subtree (jobs/<id>/) + a lifecycle.

Layout of a job dir:
  spec.json   intent + team + backend/model/effort + budget + status   (machine; you rarely open)
  state.json  compact resumable "where I am"                            (machine; rendered to HTML)
  inbox.jsonl human-injected directions (append-only; consumed on resume)
  view.html   self-contained monitor + inject page                     (you open this)
  out/        the deliverable you read (tex/pdf/notebook/diff/html)
  work/       the agents' sandbox
  log.jsonl   per-round cost/events
  .stop       kill-switch sentinel (present only when a stop was requested)

Nothing about a job's fate is baked in: it runs, it stops, and *you* decide
resume(+direction) / freeze / abandon.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime

from . import recipes as recipes_mod, staffing

STATUSES = ("created", "running", "stopped", "done", "frozen", "abandoned")

_LOG_LOCK = threading.Lock()  # parallel workers all log their own per-call line


def project_root() -> str:
    return os.environ.get("AGENT_TEAM_PROJECT", os.getcwd())


def jobs_root() -> str:
    return os.environ.get("AGENT_TEAM_JOBS", os.path.join(project_root(), "jobs"))


def git_head() -> str | None:
    """The project's current commit, or None if it isn't a git repo.

    Recorded as a code job's ``base_commit`` so the change set it produces has a fixed anchor
    (and the human an exact point to roll back to).
    """
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project_root(),
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def _slug(text: str, n: int = 4) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:n]) or "job"


class Job:
    def __init__(self, job_id: str):
        self.id = job_id
        self.dir = os.path.join(jobs_root(), job_id)

    # --- paths ---
    @property
    def spec_path(self):   return os.path.join(self.dir, "spec.json")
    @property
    def state_path(self):  return os.path.join(self.dir, "state.json")
    @property
    def inbox_path(self):  return os.path.join(self.dir, "inbox.jsonl")
    @property
    def log_path(self):    return os.path.join(self.dir, "log.jsonl")
    @property
    def stop_path(self):   return os.path.join(self.dir, ".stop")
    @property
    def view_path(self):   return os.path.join(self.dir, "view.html")
    @property
    def work_dir(self):    return os.path.join(self.dir, "work")
    @property
    def out_dir(self):     return os.path.join(self.dir, "out")

    # --- creation ---
    @classmethod
    def create(cls, *, job_type, intent, backend, model, effort, rounds=None,
               budget_tokens=None, worker_count=None, name=None,
               timeout=None, idle_timeout=None, roles=None) -> "Job":
        recipe = recipes_mod.load(job_type)
        # per-role overrides: recipe defaults first, your --role flags on top (key by key)
        role_cfg = staffing.merge(recipe.get("roles"),
                                  staffing.normalize(roles, recipe["team"], source="--role"))
        d = recipe["defaults"]
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        slug = _slug(name, n=8) if name else _slug(intent)  # --name overrides the intent-derived slug
        job_id = f"{stamp}_{job_type}-{slug}"
        job = cls(job_id)
        os.makedirs(job.work_dir, exist_ok=True)
        os.makedirs(job.out_dir, exist_ok=True)
        spec = {
            "id": job_id,
            "type": job_type,
            "intent": intent,
            "status": "created",
            "backend": backend,
            "model": model,
            "effort": effort,
            "rounds": rounds if rounds is not None else d["rounds"],
            "worker_count": worker_count if worker_count is not None else d["worker_count"],
            "budget_tokens": budget_tokens if budget_tokens is not None else d["budget_tokens"],
            "timeout": timeout,            # hard per-call wall-clock ceiling; None = off
            "idle_timeout": idle_timeout,  # None = backend default (1800s); 0 = off
            "team": recipe["team"],
            "roles": role_cfg,             # per-role backend/model/effort/when; {} = all uniform
            "kind": recipe["kind"],
            "deliverable": recipe["deliverable"],
            "checks": recipe.get("checks", {"command": ""}),
            "created": datetime.now().isoformat(timespec="seconds"),
            "round": 0,
            "cost_usd": 0.0,
            "tokens": 0,
        }
        if recipe["kind"] == "code":
            spec["base_commit"] = git_head()  # anchor for out/changes.diff; None if not a git repo
        job.save_spec(spec)
        job.save_state({
            "intent": intent, "round": 0, "status": "created",
            "plan": "", "rounds_log": [], "claims": [],
            "checks": {}, "inbox_cursor": 0,
        })
        job._seed_deliverable(recipe)
        job._seed_provenance(recipe)
        return job

    def _seed_deliverable(self, recipe):
        dtype = recipe["deliverable"]["type"]
        path = os.path.join(self.dir, recipe["deliverable"]["path"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            return
        seed = {
            "tex": ("% Deliverable for: " + self.id + "\n\\documentclass{article}\n"
                    "\\begin{document}\n% The writer role fills this in.\n\\end{document}\n"),
            "diff": "# The changes (unified diff) land here; the code itself is edited in the project.\n",
            "html": "<!doctype html><meta charset=utf-8><title>" + self.id + "</title>\n",
            "notebook": ("# %% [markdown]\n# Deliverable notebook for " + self.id
                         + " (jupytext percent format)\n"),
        }.get(dtype, "")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(seed)

    def _seed_provenance(self, recipe):
        prov = recipe.get("provenance")
        if not prov:
            return
        reg = os.path.join(self.dir, prov["registry"])
        os.makedirs(os.path.dirname(reg), exist_ok=True)
        if not os.path.exists(reg):
            with open(reg, "w", encoding="utf-8") as fh:
                fh.write("{}\n")

    # --- spec / state IO ---
    def load_spec(self) -> dict:
        with open(self.spec_path, encoding="utf-8") as fh:
            return json.load(fh)

    def save_spec(self, spec: dict):
        _atomic_write_json(self.spec_path, spec)

    def load_state(self) -> dict:
        with open(self.state_path, encoding="utf-8") as fh:
            return json.load(fh)

    def save_state(self, state: dict):
        _atomic_write_json(self.state_path, state)

    def recipe(self) -> dict:
        return recipes_mod.load(self.load_spec()["type"])

    def exists(self) -> bool:
        return os.path.exists(self.spec_path)

    # --- inbox (human steering) ---
    def say(self, text: str):
        with open(self.inbox_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                                 "text": text}) + "\n")

    def drain_inbox(self, state: dict) -> list[str]:
        if not os.path.exists(self.inbox_path):
            return []
        with open(self.inbox_path, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        cursor = state.get("inbox_cursor", 0)
        new = lines[cursor:]
        state["inbox_cursor"] = len(lines)
        out = []
        for ln in new:
            try:
                out.append(json.loads(ln)["text"])
            except (json.JSONDecodeError, KeyError):
                out.append(ln)
        return out

    # --- stop / lifecycle ---
    def request_stop(self):
        with open(self.stop_path, "w") as fh:
            fh.write(datetime.now().isoformat())

    def stopped(self) -> bool:
        return os.path.exists(self.stop_path)

    def clear_stop(self):
        if os.path.exists(self.stop_path):
            os.unlink(self.stop_path)

    # --- liveness (a runner writes its pid; is_running verifies the process is alive) ---
    @property
    def pid_path(self):
        return os.path.join(self.dir, "job.pid")

    def write_pid(self):
        with open(self.pid_path, "w") as fh:
            fh.write(str(os.getpid()))

    def clear_pid(self):
        if os.path.exists(self.pid_path):
            os.unlink(self.pid_path)

    def is_running(self) -> bool:
        try:
            pid = int(open(self.pid_path).read().strip())
        except (OSError, ValueError):
            return False
        try:
            os.kill(pid, 0)  # signal 0: existence check, doesn't actually signal
        except OSError:
            return False  # stale pid file (process gone) -> not running
        return True

    def set_status(self, status: str):
        assert status in STATUSES, status
        spec = self.load_spec()
        spec["status"] = status
        self.save_spec(spec)
        state = self.load_state()
        state["status"] = status
        self.save_state(state)

    def log(self, event: dict):
        event = {"ts": datetime.now().isoformat(timespec="seconds"), **event}
        with _LOG_LOCK:  # workers run in parallel threads; keep lines whole
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event) + "\n")


def list_jobs() -> list["Job"]:
    root = jobs_root()
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        job = Job(name)
        if job.exists():
            out.append(job)
    return out


def _atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
    os.replace(tmp, path)
