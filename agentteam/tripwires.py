"""Round-boundary tripwires: stop a job that has stopped getting anywhere.

A bounded round budget is not a safety net. A job can spend every round of it in a livelock --
the lead re-queues real work, something consumes the slot with bookkeeping, and the checks stay
green because they measure citation integrity rather than progress. That is how a run burns
hours and delivers a fifth of its intent, with every actor behaving correctly inside its own
narrow mandate.

So the engine watches a few coarse progress signals and stops the run when they flatline. Each
one is deterministic, computed from the per-round record in ``state["progress"]``, and evaluated
*after* the round's checks -- **nothing is ever stopped mid-flight**; a round always finishes and
the job lands recoverable, exactly as ``job stop`` leaves it.

A trip is not a failure verdict. It means "a human should look now rather than in three hours",
and ``job resume`` continues if the human disagrees.
"""

from __future__ import annotations

import re
import statistics

STALL_ROUNDS = 2      # rounds with no new verified claims before we stop
REPEAT_ROUNDS = 3     # identical lead task, this many rounds running, is a livelock
RUNAWAY_FACTOR = 3.0  # round spend this many times the running median is suspicious
RUNAWAY_MIN_HISTORY = 3


def record(state, *, round_no, claims, new_verified, tasks, tokens,
           checks_passed, fingerprint=None) -> dict:
    """Append this round's progress signals to ``state["progress"]`` and return them."""
    entry = {
        "round": round_no,
        "claims": claims,
        "new_verified": new_verified,
        "tasks": [_norm(t) for t in (tasks or [])],
        "tokens": tokens,
        "checks_passed": checks_passed,
        "fingerprint": fingerprint,
    }
    state.setdefault("progress", []).append(entry)
    state["progress"] = state["progress"][-40:]
    return entry


def evaluate(spec, state) -> str | None:
    """The reason to stop, or None to keep going."""
    if not spec.get("tripwires", True):
        return None
    prog = state.get("progress") or []
    if not prog:
        return None
    for rule in (_no_claims, _livelock, _stalled, _project_frozen, _runaway):
        reason = rule(spec, prog)
        if reason:
            return reason
    return None


def _no_claims(spec, prog) -> str | None:
    if prog[-1]["claims"] == 0:
        return ("the verifier returned no parsable claims this round -- either it verified "
                "nothing, or its report is not reaching the harvester (see agentteam/claims.py). "
                "Without claims there is no durable state, so every later round pays full price.")
    return None


def _livelock(spec, prog) -> str | None:
    if len(prog) < REPEAT_ROUNDS:
        return None
    firsts = [p["tasks"][0] for p in prog[-REPEAT_ROUNDS:] if p.get("tasks")]
    if len(firsts) == REPEAT_ROUNDS and len(set(firsts)) == 1:
        return (f"the same task has led the plan {REPEAT_ROUNDS} rounds running "
                f"({firsts[0][:120]!r}) -- the queue is not advancing")
    return None


def _stalled(spec, prog) -> str | None:
    recent = prog[-STALL_ROUNDS:]
    if len(recent) == STALL_ROUNDS and all(p["new_verified"] == 0 for p in recent):
        return f"no new verified claims in {STALL_ROUNDS} consecutive rounds"
    return None


def _project_frozen(spec, prog) -> str | None:
    """For code jobs: the project itself has not changed. Nothing was built, whatever was said."""
    if spec.get("kind") != "code" or len(prog) < STALL_ROUNDS + 1:
        return None
    window = prog[-(STALL_ROUNDS + 1):]
    prints = [p.get("fingerprint") for p in window]
    if any(f is None for f in prints):
        return None
    if len(set(prints)) == 1:
        return (f"the project has not changed in {STALL_ROUNDS} rounds "
                "(same diff against base_commit)")
    return None


def _runaway(spec, prog) -> str | None:
    if len(prog) < RUNAWAY_MIN_HISTORY + 1:
        return None
    prior = [p["tokens"] for p in prog[:-1] if p["tokens"]]
    if not prior:
        return None
    median = statistics.median(prior)
    last = prog[-1]["tokens"]
    if median > 0 and last > RUNAWAY_FACTOR * median:
        return (f"this round spent {last:,} tokens, over {RUNAWAY_FACTOR:g}x the "
                f"{median:,.0f}-token median of the rounds before it")
    return None


def _norm(task: str) -> str:
    return re.sub(r"\s+", " ", (task or "")).strip().lower()[:200]
