"""Per-role staffing: what each role runs on, and when it runs.

Backend/model/effort are properties of a **role**, not of the job. A grind ``worker`` can run
cheap and shallow while the ``verifier`` runs deep -- and a verifier on a *different backend*
than the worker is a genuinely independent check, rather than the same model grading its own
homework.

Resolution is layered; later wins, key by key:

    job default (--backend/--model/--effort)
      -> recipe "roles" block
      -> `job new --role <role>:<key>=<value>,...`
      -> PI staffing (`ROLE <role>: effort=...` lines), bounded by policy.json

Only the PI layer is clamped by ``policy.json`` (``effort_max``, ``backends_allowed``,
``max_workers``): you are the principal, so your own flags are never second-guessed.

A role also carries an optional ``when`` schedule -- ``every`` (default) | ``first`` | ``last``
| a list of round numbers -- so the ``writer`` can be a single assembly pass at the end instead
of charging a hand-off every round. Schedules apply to a recipe's ``extra`` roles; the lead and
the verifier run every round by design (verification is a permanent member of every team).
"""

from __future__ import annotations

import json
import os
import re

from . import HOME, backends

CONFIG_KEYS = ("backend", "model", "effort", "when")
EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh")  # cheap/shallow -> expensive/deep
WHEN_KEYWORDS = ("every", "first", "last")

_ROLE_LINE = re.compile(r"^\s*ROLE\s+([A-Za-z0-9_-]+)\s*:\s*(.+?)\s*$", re.MULTILINE)
_KV = re.compile(r"([a-z_]+)\s*=\s*([^\s,;]+)")


# --- policy ------------------------------------------------------------------

def load_policy() -> dict:
    """Bounds for PI-chosen staffing, from policy.json next to the tool (optional)."""
    path = os.path.join(HOME, "policy.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def allowed_efforts(policy: dict) -> list[str]:
    """The effort ladder the PI may choose from, truncated at ``effort_max``."""
    ceiling = policy.get("effort_max")
    if ceiling in EFFORT_ORDER:
        return list(EFFORT_ORDER[: EFFORT_ORDER.index(ceiling) + 1])
    return list(EFFORT_ORDER)


def allowed_backends(policy: dict) -> list[str]:
    allowed = policy.get("backends_allowed")
    return list(allowed) if allowed else [b for b in backends.DEFAULTS if b != "mock"]


# --- the team's roles --------------------------------------------------------

def team_roles(team: dict) -> list[str]:
    """Every role name this team can call, in choreography order, de-duplicated."""
    names = [team.get("lead") or "pi"]
    names += list(team.get("workers") or [])
    names += [team.get("verifier")]
    names += list(team.get("extra") or [])
    seen, out = set(), []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


# --- validation / merging ----------------------------------------------------

def normalize(raw: dict, team: dict, *, source: str) -> dict:
    """Validate a ``{role: {backend, model, effort, when}}`` block.

    Raises ``ValueError`` with a pointed message: a typo'd role name or key would otherwise be
    silently ignored, and you'd only find out from the bill.
    """
    known = team_roles(team)
    out = {}
    for role, cfg in (raw or {}).items():
        if role not in known:
            raise ValueError(
                f"{source}: unknown role {role!r} -- this team has: {', '.join(known)}")
        if not isinstance(cfg, dict):
            raise ValueError(
                f"{source}: role {role!r} must map to an object, got {type(cfg).__name__}")
        clean = {}
        for key, val in cfg.items():
            if key not in CONFIG_KEYS:
                raise ValueError(f"{source}: role {role!r} has unknown key {key!r} "
                                 f"(allowed: {', '.join(CONFIG_KEYS)})")
            if key == "backend" and val not in backends.DEFAULTS:
                raise ValueError(f"{source}: role {role!r} backend {val!r} unknown "
                                 f"(known: {', '.join(backends.DEFAULTS)})")
            clean[key] = _normalize_when(val, role, source) if key == "when" else val
        if clean:
            out[role] = clean
    return out


def _normalize_when(val, role, source):
    if isinstance(val, str) and val in WHEN_KEYWORDS:
        return val
    if isinstance(val, list) and val and all(isinstance(v, int) for v in val):
        return sorted(val)
    raise ValueError(f"{source}: role {role!r} has invalid when={val!r} "
                     f"(use {' | '.join(WHEN_KEYWORDS)}, or a list of round numbers)")


def merge(*layers) -> dict:
    """Later layers win **key by key**, so a role's model can come from the recipe while its
    effort comes from the command line."""
    out: dict = {}
    for layer in layers:
        for role, cfg in (layer or {}).items():
            out.setdefault(role, {}).update(cfg)
    return out


# --- the two authoring surfaces ----------------------------------------------

def parse_cli(items) -> dict:
    """``--role verifier:effort=xhigh,backend=claude`` (repeatable) -> ``{role: {...}}``."""
    out: dict = {}
    for item in items or []:
        role, sep, rest = item.partition(":")
        if not sep or not role.strip():
            raise ValueError(
                f"--role {item!r}: expected <role>:<key>=<value>[,<key>=<value>]")
        cfg = out.setdefault(role.strip(), {})
        for pair in re.split(r"[,\s]+", rest.strip()):
            if not pair:
                continue
            key, sep, val = pair.partition("=")
            if not sep:
                raise ValueError(f"--role {item!r}: {pair!r} is not <key>=<value>")
            key, val = key.strip(), val.strip()
            cfg[key] = _coerce_when(val) if key == "when" else val
        if not cfg:
            raise ValueError(f"--role {item!r}: no <key>=<value> given")
    return out


def parse_pi(text: str) -> dict:
    """Read ``ROLE worker: effort=medium, backend=codex`` lines out of the PI's staffing brief.

    Deliberately lenient about surrounding prose -- this is model output, not a config file.
    """
    out: dict = {}
    for role, rest in _ROLE_LINE.findall(text or ""):
        cfg = {key: (_coerce_when(val) if key == "when" else val)
               for key, val in _KV.findall(rest) if key in CONFIG_KEYS}
        if cfg:
            out[role] = cfg
    return out


def _coerce_when(val):
    """`when=3` on the command line means round 3; lists stay a recipe-file affordance."""
    return [int(val)] if isinstance(val, str) and val.isdigit() else val


def clamp(proposed: dict, policy: dict) -> tuple[dict, list[str]]:
    """Bound the PI's choices by policy.json. Returns ``(clamped, notes)``; the notes go to the
    job log so a clamp is visible rather than mysterious."""
    allowed, ceiling = policy.get("backends_allowed"), policy.get("effort_max")
    out, notes = {}, []
    for role, cfg in (proposed or {}).items():
        cfg = dict(cfg)
        if allowed and cfg.get("backend") and cfg["backend"] not in allowed:
            notes.append(f"{role}: backend {cfg['backend']!r} not in backends_allowed -- dropped")
            cfg.pop("backend")
            cfg.pop("model", None)  # the model named the dropped backend's vocabulary
        if (ceiling in EFFORT_ORDER and cfg.get("effort") in EFFORT_ORDER
                and EFFORT_ORDER.index(cfg["effort"]) > EFFORT_ORDER.index(ceiling)):
            notes.append(f"{role}: effort {cfg['effort']} -> {ceiling} (effort_max)")
            cfg["effort"] = ceiling
        if cfg:
            out[role] = cfg
    return out, notes


# --- what the engine asks ----------------------------------------------------

def resolve(spec: dict, role_name: str) -> dict:
    """The concrete ``{backend, model, effort}`` this role runs on."""
    cfg = (spec.get("roles") or {}).get(role_name) or {}
    backend = cfg.get("backend") or spec.get("backend")
    model = cfg.get("model")
    if not model:
        # A role that switched backend cannot inherit the job's model -- that name belongs to
        # the other CLI's vocabulary. Take the new backend's default instead.
        model = (spec.get("model") if backend == spec.get("backend")
                 else backends.DEFAULTS.get(backend, {}).get("model"))
    return {"backend": backend, "model": model, "effort": cfg.get("effort") or spec.get("effort")}


def when_of(spec: dict, role_name: str):
    return ((spec.get("roles") or {}).get(role_name) or {}).get("when", "every")


def runs_in_round(spec: dict, role_name: str, *, round_no: int, final: bool) -> bool:
    """Does this role run in round ``round_no``? ``final`` marks the last round of *this run*
    (the round budget is spent, the lead signalled done, or the run is ending) -- which is why
    ``last`` still fires when a job stops early."""
    when = when_of(spec, role_name)
    if isinstance(when, list):
        return round_no in when
    if when == "first":
        return round_no == 1
    if when == "last":
        return final
    return True


def table(spec: dict) -> list[dict]:
    """One resolved row per role: what actually runs, on what, how often, how many at a time.

    This is provenance, not decoration -- with per-role backends the job-level model line no
    longer tells you what produced a given claim.
    """
    team = spec.get("team") or {}
    workers = set(team.get("workers") or [])
    rows = []
    for role in team_roles(team):
        when = when_of(spec, role)
        rows.append({
            "role": role,
            "n": spec.get("worker_count", 1) if role in workers else 1,
            "when": when if isinstance(when, str) else ", ".join(str(v) for v in when),
            **resolve(spec, role),
        })
    return rows
