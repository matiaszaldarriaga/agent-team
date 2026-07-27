"""Load job-type recipes from recipes/*.json.

A recipe casts roles into a team and sets the choreography defaults for a job type.

Schema (all fields documented in recipes/*.json):
  name         str    -- job type, matches the filename
  description  str
  kind         str    -- "understanding" | "code" (affects defaults & workdir)
  workdir      str    -- "job"  (self-contained subtree; e.g. derivations) or
                         "project" (agents edit the surrounding project; e.g. code features)
  team:
    lead       str    -- role that plans each round / staffs in --pi mode (usually "pi")
    workers    [str]  -- worker role(s); the engine runs `worker_count` of the first one
    verifier   str    -- the independent checker; ALWAYS present (math -> verifier,
                         code -> code-reviewer)
    extra      [str]  -- additional roles run once per round after workers (e.g. test-writer)
  roles       {}     -- OPTIONAL per-role overrides, keyed by role name (see agentteam/staffing.py):
                        {"verifier": {"effort": "xhigh"}, "writer": {"when": "last"}}
                        keys: backend | model | effort | when
                        (when: "every" (default) | "first" | "last" | [round numbers];
                         schedules apply to `extra` roles -- lead and verifier run every round)
  deliverable:
    type       str    -- "tex" | "diff" | "notebook" | "html"
    path       str    -- where the deliverable lands, relative to the job dir (e.g. out/notes.tex)
  checks:
    command    str    -- executable verification run each round from the job dir ("" = none)
  defaults:
    rounds        int
    worker_count  int
    budget_tokens int
"""

import json
import os

from . import RECIPES_DIR
from . import staffing


def load(recipe_name: str) -> dict:
    path = os.path.join(RECIPES_DIR, f"{recipe_name}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"recipe {recipe_name!r} not found at {path} (available: {', '.join(available())})"
        )
    with open(path, encoding="utf-8") as fh:
        recipe = json.load(fh)
    _validate(recipe, recipe_name)
    return recipe


def available() -> list[str]:
    if not os.path.isdir(RECIPES_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(RECIPES_DIR) if f.endswith(".json"))


def _validate(recipe: dict, name: str) -> None:
    for key in ("name", "kind", "team", "deliverable", "defaults"):
        if key not in recipe:
            raise ValueError(f"recipe {name!r} missing required key: {key!r}")
    team = recipe["team"]
    if not team.get("verifier"):
        raise ValueError(
            f"recipe {name!r} has no verifier -- verification is a permanent member of every team"
        )
    recipe.setdefault("workdir", "job" if recipe["kind"] == "understanding" else "project")
    recipe["team"].setdefault("lead", "pi")
    recipe["team"].setdefault("workers", ["worker"])
    recipe["team"].setdefault("extra", [])
    recipe["checks"] = recipe.get("checks", {"command": ""})
    # per-role overrides are optional; validate here so a typo fails at load, not at spend time
    recipe["roles"] = staffing.normalize(recipe.get("roles"), recipe["team"],
                                         source=f"recipe {name!r}")
    recipe["defaults"].setdefault("worker_count", 1)
    recipe["defaults"].setdefault("rounds", 4)
    recipe["defaults"].setdefault("budget_tokens", 300000)
