"""Load role personalities from roles/*.md.

A role is just a prompt file. It defines *who* an agent is (a verifier, a coder, the PI);
recipes decide *how many* and *in what order*. Roles are the portable, shareable cast that
travels with the repo.
"""

import os

from . import ROLES_DIR


def load(role_name: str) -> str:
    path = os.path.join(ROLES_DIR, f"{role_name}.md")
    if not os.path.exists(path):
        raise FileNotFoundError(f"role {role_name!r} not found at {path}")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def available() -> list[str]:
    if not os.path.isdir(ROLES_DIR):
        return []
    return sorted(f[:-3] for f in os.listdir(ROLES_DIR) if f.endswith(".md"))
