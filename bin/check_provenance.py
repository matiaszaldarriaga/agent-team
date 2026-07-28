#!/usr/bin/env python3
r"""check_provenance.py -- enforce that nothing in a deliverable is invented.

Every important statement in the deliverable must carry a provenance tag, and every tag must
resolve to a registry entry that says, concretely, how to reproduce it.

Usage:
    check_provenance.py <deliverable> <provenance.json>

Tags recognised in the deliverable:
    \src{key}        (LaTeX)  -- may be comma-separated: \src{k1,k2}
    [src:key]        (html / markdown / notebook)

Registry (provenance.json): a JSON object { "key": {entry}, ... }; each entry has:
    statement   str   what is being claimed                                    (required)
    type        str   check | script | data | source | derivation             (required)
    reproduce   str   how to reproduce it                                      (required)
                        - check/script/data : a FILE PATH (existence is enforced; a
                          "path::selector" form is allowed, e.g. out/checks.py::test_soft)
                        - source/derivation : a citation / location (free text)
    detail      str   optional: the function / line / page that pins it down

Exit 0 iff every tag resolves and every entry is well-formed and reproducible.
Unreferenced entries are warnings only (they do not fail the check).
"""

import json
import os
import re
import sys

PATH_TYPES = {"check", "script", "data"}
KNOWN_TYPES = PATH_TYPES | {"source", "derivation"}
REQUIRED = ("statement", "type", "reproduce")
TAG_RES = (re.compile(r"\\src\{([^}]+)\}"), re.compile(r"\[src:([^\]]+)\]"))


def _search_roots(deliverable):
    """Where a `reproduce` path may be relative to.

    The checker runs from the job directory, but a **code** job's team lives in the surrounding
    project and naturally writes project-relative paths (`tests/test_x.py`). Insisting on one root
    turns a correct registry into 35 spurious errors -- which then blocks `[[DONE]]` and burns the
    remaining rounds. So accept either, in a fixed order, and say which roots were tried when a
    path genuinely does not exist.
    """
    roots, seen = [], set()
    here = os.path.dirname(os.path.abspath(deliverable))
    for cand in [os.getcwd(), here] + [os.path.abspath(os.path.join(here, *[".."] * n))
                                       for n in range(1, 5)]:
        if cand not in seen:
            seen.add(cand)
            roots.append(cand)
    return roots


def _resolve(path, roots):
    if os.path.isabs(path):
        return path if os.path.exists(path) else None
    for root in roots:
        cand = os.path.join(root, path)
        if os.path.exists(cand):
            return cand
    return None


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    deliverable, registry_path = argv
    errors, warnings = [], []

    if not os.path.exists(deliverable):
        print(f"ERROR: deliverable not found: {deliverable}")
        return 1
    with open(deliverable, encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    registry = {}
    if os.path.exists(registry_path):
        try:
            with open(registry_path, encoding="utf-8") as fh:
                registry = json.load(fh)
        except json.JSONDecodeError as exc:
            print(f"ERROR: {registry_path} is not valid JSON: {exc}")
            return 1
    if not isinstance(registry, dict):
        print(f"ERROR: {registry_path} must be a JSON object of key -> entry")
        return 1

    roots = _search_roots(deliverable)

    keys_used = set()
    for rx in TAG_RES:
        for match in rx.findall(text):
            for key in match.split(","):
                key = key.strip()
                if key:
                    keys_used.add(key)

    for key in sorted(keys_used):
        if key not in registry:
            errors.append(rf"tag \src{{{key}}} used in the deliverable has no registry entry")

    for key, entry in registry.items():
        if not isinstance(entry, dict):
            errors.append(f"entry {key!r} is not an object")
            continue
        for field in REQUIRED:
            if not str(entry.get(field, "")).strip():
                errors.append(f"entry {key!r} is missing required field {field!r}")
        etype = str(entry.get("type", "")).strip()
        repro = str(entry.get("reproduce", "")).strip()
        if etype and etype not in KNOWN_TYPES:
            warnings.append(f"entry {key!r} has unusual type {etype!r}")
        if etype in PATH_TYPES and repro:
            path = repro.split("::", 1)[0].strip()
            if not _resolve(path, roots):
                errors.append(f"entry {key!r} ({etype}) reproduce path not found: {path} "
                              f"(looked in: {', '.join(roots)})")
        if key not in keys_used:
            warnings.append(f"entry {key!r} is never referenced by a tag")

    for w in warnings:
        print(f"WARN: {w}")
    for e in errors:
        print(f"ERROR: {e}")
    print(f"provenance: {len(keys_used)} tag(s), {len(registry)} entry(ies), "
          f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
