"""Command-line surface for agent-team.

  job new <type> "<intent>" [--pi] [--run] [--backend ...] [--model ...] [--effort ...]
                            [--rounds N] [--workers N] [--budget N]
  job run <id> [--rounds N]        run (up to N more rounds; default: to the job's round budget)
  job resume <id> [--say "..."] [--rounds N]   inject a direction and continue
  job say <id> "<direction>"       queue a direction for the next round
  job stop <id>                    request the kill-switch
  job freeze <id>                  mark done/kept (you weave it in later, in a session)
  job abandon <id>                 mark dead-end; left as a record
  job status [<id>]                list jobs / show one
  job serve [--port N]             start the HTML monitor + inject server
  job roles | recipes              list the installed cast / job types
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__, recipes as recipes_mod, roles as roles_mod
from . import engine
from .jobs import Job, list_jobs

# Backend defaults. Model/effort are recorded in every spec and shown in the view for provenance.
BACKEND_DEFAULTS = {
    "claude": {"model": "claude-opus-4-8[1m]", "effort": "high"},
    "codex": {"model": "gpt-5.6-sol", "effort": "xhigh"},
    "mock": {"model": None, "effort": None},
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="job", description="fire a team of agents into a subtree")
    p.add_argument("--version", action="version", version=f"agent-team {__version__}")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("new", help="create a job")
    sp.add_argument("type")
    sp.add_argument("intent")
    sp.add_argument("--pi", action="store_true", help="let the PI staff it (plan-and-go)")
    sp.add_argument("--run", action="store_true", help="run immediately after creating")
    sp.add_argument("--backend", default="claude", choices=list(BACKEND_DEFAULTS))
    sp.add_argument("--model", default=None)
    sp.add_argument("--effort", default=None)
    sp.add_argument("--rounds", type=int, default=None)
    sp.add_argument("--workers", type=int, default=None)
    sp.add_argument("--budget", type=int, default=None, help="token budget")

    for name in ("run", "resume"):
        q = sub.add_parser(name)
        q.add_argument("id")
        q.add_argument("--rounds", type=int, default=None)
        if name == "resume":
            q.add_argument("--say", default=None)

    for name in ("stop", "freeze", "abandon"):
        q = sub.add_parser(name)
        q.add_argument("id")

    q = sub.add_parser("staff"); q.add_argument("id")
    q = sub.add_parser("say"); q.add_argument("id"); q.add_argument("text")
    q = sub.add_parser("status"); q.add_argument("id", nargs="?")
    q = sub.add_parser("serve"); q.add_argument("--port", type=int, default=None)
    sub.add_parser("roles")
    sub.add_parser("recipes")

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 1
    return _dispatch(args)


def _dispatch(args):
    if args.cmd == "new":
        return _cmd_new(args)
    if args.cmd in ("run", "resume"):
        return _cmd_run(args)
    if args.cmd == "staff":
        job = _need(args.id)
        engine.staff_with_pi(job)
        print(f"{args.id}: staffed (worker_count={job.load_spec()['worker_count']})")
        print("--- PI plan ---\n" + (job.load_state().get("plan", "")[:2000]))
        return 0
    if args.cmd == "say":
        _need(args.id).say(args.text)
        print(f"queued for {args.id}")
        return 0
    if args.cmd in ("stop", "freeze", "abandon"):
        job = _need(args.id)
        if args.cmd == "stop":
            job.request_stop(); print(f"stop requested for {args.id}")
        else:
            status = "frozen" if args.cmd == "freeze" else "abandoned"
            job.set_status(status)
            print(f"{args.id} -> {status}")
        return 0
    if args.cmd == "status":
        return _cmd_status(args)
    if args.cmd == "serve":
        from . import serve as serve_mod
        from .render import DEFAULT_PORT
        serve_mod.serve(args.port or DEFAULT_PORT)
        return 0
    if args.cmd == "roles":
        print("\n".join(roles_mod.available()) or "(none)")
        return 0
    if args.cmd == "recipes":
        for r in recipes_mod.available():
            print(f"{r:10s} {recipes_mod.load(r).get('description','')}")
        return 0
    return 1


def _read_intent(intent):
    """Intent text, or the contents of a file if given as @path (handy for long LaTeX intents)."""
    if intent.startswith("@"):
        with open(os.path.expanduser(intent[1:]), encoding="utf-8") as fh:
            return fh.read().strip()
    return intent


def _cmd_new(args):
    d = BACKEND_DEFAULTS[args.backend]
    model = args.model if args.model is not None else d["model"]
    effort = args.effort if args.effort is not None else d["effort"]
    job = Job.create(job_type=args.type, intent=_read_intent(args.intent), backend=args.backend,
                     model=model, effort=effort, rounds=args.rounds,
                     budget_tokens=args.budget, worker_count=args.workers)
    from . import render
    render.render(job)
    print(f"created {job.id}")
    print(f"  backend={args.backend} model={model} effort={effort}")
    print(f"  dir: {job.dir}")
    print(f"  view: {job.view_path}")
    if args.pi:
        print("  staffing with PI (plan-and-go)...")
        engine.staff_with_pi(job)
    if args.run:
        status = engine.run(job)
        print(f"  finished: {status}")
    else:
        print(f"  next: job run {job.id}")
    return 0


def _cmd_run(args):
    job = _need(args.id)
    if args.cmd == "resume" and getattr(args, "say", None):
        job.say(args.say)
    spec = job.load_spec()
    if args.rounds is not None:
        spec["rounds"] = spec.get("round", 0) + args.rounds
        job.save_spec(spec)
    elif spec.get("round", 0) >= spec.get("rounds", 0):
        # already at the round budget: extend by the recipe default so resume does something
        add = recipes_mod.load(spec["type"])["defaults"]["rounds"]
        spec["rounds"] = spec["round"] + add
        job.save_spec(spec)
        print(f"  extended round budget by {add}")
    status = engine.run(job)
    print(f"{args.id}: {status}  (round {job.load_spec()['round']}, "
          f"${job.load_spec()['cost_usd']:.3f})")
    print(f"  decide next: job resume {args.id} --say \"...\" | job freeze {args.id} | job abandon {args.id}")
    return 0


def _cmd_status(args):
    if args.id:
        job = _need(args.id)
        spec = job.load_spec()
        import json
        print(json.dumps(spec, indent=2))
        print(f"view: {job.view_path}")
        return 0
    jobs = list_jobs()
    if not jobs:
        print("no jobs yet.  create one:  job new derive \"...\"")
        return 0
    print(f"{'STATUS':10s} {'ROUND':7s} {'COST':>8s}  JOB")
    for job in jobs:
        try:
            s = job.load_spec()
        except OSError:
            continue
        print(f"{s.get('status',''):10s} {str(s.get('round',0))+'/'+str(s.get('rounds',0)):7s} "
              f"${s.get('cost_usd',0.0):>7.3f}  {job.id}")
    return 0


def _need(job_id) -> Job:
    job = Job(job_id)
    if not job.exists():
        print(f"no such job: {job_id}", file=sys.stderr)
        raise SystemExit(2)
    return job
