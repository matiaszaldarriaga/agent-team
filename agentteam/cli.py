"""Command-line surface for agent-team.

  job new <type> "<intent>" [--pi] [--run] [--backend ...] [--model ...] [--effort ...]
                            [--rounds N] [--workers N] [--budget N]
                            [--role <role>:<key>=<value>,...]   per-role depth, repeatable
  job run <id> [--rounds N]        run (up to N more rounds; default: to the job's round budget)
  job resume <id> [--say "..."] [--rounds N]   inject a direction and continue
  job say <id> "<direction>"       queue a direction for the next round
  job stop <id>                    request the kill-switch
  job freeze <id>                  mark done/kept (you weave it in later, in a session)
  job abandon <id>                 mark dead-end; left as a record
  job status [<id>]                list jobs / show one (● marks a live run + its phase)
  job watch <id>                   live-poll a job's phase/status until it ends
  job serve [--port N]             start the HTML monitor + inject server
  job roles | recipes              list the installed cast / job types
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

from . import __version__, recipes as recipes_mod, roles as roles_mod
from . import backends, engine, staffing
from .jobs import Job, list_jobs

# Backend defaults live in backends.py (a *role* may switch backend mid-job and then needs that
# backend's default model). Model/effort are recorded in every spec and shown for provenance.
BACKEND_DEFAULTS = backends.DEFAULTS


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
    sp.add_argument("--name", default=None, help="short name for the job id (else derived from intent)")
    sp.add_argument("--role", action="append", default=None, metavar="ROLE:K=V[,K=V]",
                    help="per-role override, repeatable: backend|model|effort|when. "
                         "e.g. --role worker:effort=medium --role verifier:effort=xhigh,"
                         "backend=codex --role writer:when=last")
    sp.add_argument("--timeout", type=int, default=None,
                    help="hard per-call wall-clock seconds (default: off)")
    sp.add_argument("--idle-timeout", type=int, default=None,
                    help="kill a call after N s of no output AND no file writes (default 1800; 0 off)")

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
    q = sub.add_parser("watch"); q.add_argument("id")
    q.add_argument("--interval", type=int, default=5, help="poll seconds (default 5)")
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
        spec = job.load_spec()
        print(f"{args.id}: staffed (worker_count={spec['worker_count']})")
        _print_roles(spec)
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
    if args.cmd == "watch":
        return _cmd_watch(args)
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
    try:
        role_cfg = staffing.parse_cli(args.role)
        job = Job.create(job_type=args.type, intent=_read_intent(args.intent),
                         backend=args.backend, model=model, effort=effort, rounds=args.rounds,
                         budget_tokens=args.budget, worker_count=args.workers, name=args.name,
                         timeout=args.timeout, idle_timeout=args.idle_timeout, roles=role_cfg)
    except ValueError as err:  # a typo'd role/key -- fail now, not after the first call bills
        print(f"error: {err}", file=sys.stderr)
        return 2
    from . import render
    render.render(job)
    print(f"created {job.id}")
    print(f"  backend={args.backend} model={model} effort={effort}")
    _print_roles(job.load_spec())
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


def _print_roles(spec, indent="  "):
    """Show the resolved per-role staffing, but only when it isn't uniform -- with per-role
    backends the single job-level model line no longer tells you what produced what."""
    if not spec.get("roles"):
        return
    print(f"{indent}roles:")
    for row in staffing.table(spec):
        count = f" x{row['n']}" if row["n"] > 1 else ""
        sched = "" if row["when"] == "every" else f"  when={row['when']}"
        print(f"{indent}  {row['role']:<13}{row['backend']:<7} "
              f"{row['model'] or '—':<24} {row['effort'] or '—':<7}{count}{sched}")


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
        _print_roles(spec, indent="")
        print(f"view: {job.view_path}")
        return 0
    jobs = list_jobs()
    if not jobs:
        print("no jobs yet.  create one:  job new derive \"...\"")
        return 0
    print(f"{'':2s}{'STATUS':10s} {'ROUND':7s} {'TOKENS':>10s}  JOB")
    for job in jobs:
        try:
            s = job.load_spec()
        except OSError:
            continue
        live = "●" if job.is_running() else " "
        phase = ""
        if live == "●":
            try:
                phase = "  " + job.load_state().get("phase", "")
            except OSError:
                pass
        print(f"{live} {s.get('status',''):10s} "
              f"{str(s.get('round', 0)) + '/' + str(s.get('rounds', 0)):7s} "
              f"{s.get('tokens', 0):>10,}  {job.id}{phase}")
    return 0


def _cmd_watch(args):
    job = _need(args.id)
    print(f"watching {args.id}  (Ctrl-C to stop; the job is unaffected)")
    try:
        while True:
            spec, state = job.load_spec(), job.load_state()
            alive = job.is_running()
            phase = state.get("phase", "") if alive else ""
            mark = "● live" if alive else "○ idle"
            print(f"  {datetime.now():%H:%M:%S}  {mark}  {spec['status']:8s}  "
                  f"round {spec['round']}/{spec['rounds']}  tok {spec['tokens']:,}  {phase}")
            terminal = spec["status"] in ("done", "frozen", "abandoned") or \
                (spec["status"] == "stopped" and not alive)
            if terminal:
                print(f"  -> {spec['status']}")
                return 0
            time.sleep(max(1, args.interval))
    except KeyboardInterrupt:
        print("\n(stopped watching)")
        return 0


def _need(job_id) -> Job:
    job = Job(job_id)
    if not job.exists():
        print(f"no such job: {job_id}", file=sys.stderr)
        raise SystemExit(2)
    return job
