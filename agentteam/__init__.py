"""agent-team: fire a bounded team of agents into an isolated subtree to produce one deliverable.

Three levels (see README):
  roles/    -- the personalities (reusable prompt files): pi, worker, verifier, code-reviewer, ...
  recipes/  -- job types: cast roles into a team + choreography + deliverable
  a job     -- one run of a recipe in jobs/<id>/, staffed by you or the PI, that you then
               resume / freeze / abandon.

Pure standard library. Backends (claude, codex, mock) are swappable per job.
"""

import os

HOME = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))  # repo root (the tool)
ROLES_DIR = os.path.join(HOME, "roles")
RECIPES_DIR = os.path.join(HOME, "recipes")
TEMPLATES_DIR = os.path.join(HOME, "templates")

__version__ = "0.1.0"
