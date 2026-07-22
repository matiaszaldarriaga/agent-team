"""Smoke + behavior tests for agent-team. Zero dependencies (stdlib unittest + the mock backend).

Run:  python -m unittest discover -s tests    (or: python tests/test_agentteam.py)
"""

import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from agentteam import engine, recipes, roles          # noqa: E402
from agentteam.jobs import Job                          # noqa: E402


class RecipesAndRoles(unittest.TestCase):
    def test_all_recipes_load_and_validate(self):
        names = recipes.available()
        self.assertEqual(set(names), {"derive", "feature", "draft", "wiki"})
        for name in names:
            r = recipes.load(name)
            self.assertTrue(r["team"]["verifier"], f"{name} must have a verifier")
            self.assertIn(r["kind"], ("understanding", "code"))

    def test_roles_present(self):
        self.assertEqual(
            set(roles.available()),
            {"pi", "worker", "verifier", "code-reviewer", "test-writer", "writer"},
        )

    def test_verifierless_recipe_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "bad.json")
            with open(bad, "w") as fh:
                fh.write('{"name":"bad","kind":"code","team":{"lead":"pi"},'
                         '"deliverable":{"type":"diff","path":"out/x"},"defaults":{}}')
            # load() resolves by name from RECIPES_DIR, so validate directly:
            import json
            from agentteam.recipes import _validate
            with open(bad) as fh:
                recipe = json.load(fh)
            with self.assertRaises(ValueError):
                _validate(recipe, "bad")


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["AGENT_TEAM_PROJECT"] = self._tmp

    def tearDown(self):
        os.environ.pop("AGENT_TEAM_PROJECT", None)

    def test_create_run_resume_freeze(self):
        job = Job.create(job_type="derive", intent="warm up the machinery",
                         backend="mock", model=None, effort=None, rounds=2)
        # scaffold
        self.assertTrue(os.path.exists(job.spec_path))
        self.assertTrue(os.path.exists(os.path.join(job.dir, "out/notes.tex")))
        self.assertTrue(os.path.exists(os.path.join(job.dir, "out/provenance.json")))

        # run to the round cap
        status = engine.run(job)
        self.assertEqual(status, "stopped")
        state = job.load_state()
        self.assertEqual(state["round"], 2)
        self.assertEqual(len(state["rounds_log"]), 2)
        # provenance check on the empty seed passes
        self.assertTrue(state["checks"].get("passed"))

        # view.html fully rendered (no unreplaced placeholders)
        with open(job.view_path) as fh:
            self.assertNotIn("{{", fh.read())

        # resume consumes an injected direction. `job resume` extends the round budget first
        # (a job already at its cap won't run more rounds), so do the same here.
        job.say("focus on the resonant channel")
        spec = job.load_spec()
        spec["rounds"] = spec["round"] + 1
        job.save_spec(spec)
        engine.run(job, max_new_rounds=1)
        self.assertEqual(job.load_state()["inbox_cursor"], 1)

        # freeze is terminal state
        job.set_status("frozen")
        self.assertEqual(job.load_spec()["status"], "frozen")


class ProvenanceChecker(unittest.TestCase):
    """bin/check_provenance.py: exit 0 iff every tag resolves and every entry is reproducible."""

    def _run(self, deliverable_text, registry_json):
        d = tempfile.mkdtemp()
        deliverable = os.path.join(d, "notes.tex")
        registry = os.path.join(d, "provenance.json")
        with open(deliverable, "w") as fh:
            fh.write(deliverable_text)
        with open(registry, "w") as fh:
            fh.write(registry_json)
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO, "bin", "check_provenance.py"),
             "notes.tex", "provenance.json"],
            cwd=d, capture_output=True, text=True)
        return proc.returncode, d

    def test_clean_passes(self):
        rc, _ = self._run("hello, no claims here\n", "{}")
        self.assertEqual(rc, 0)

    def test_unbacked_tag_fails(self):
        rc, _ = self._run(r"the answer is 42 \src{ans}." + "\n", "{}")
        self.assertEqual(rc, 1)

    def test_missing_reproduce_path_fails(self):
        reg = '{"ans": {"statement":"42","type":"check","reproduce":"out/checks.py::t"}}'
        rc, _ = self._run(r"the answer is 42 \src{ans}." + "\n", reg)
        self.assertEqual(rc, 1)

    def test_valid_passes(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "out"))
        with open(os.path.join(d, "out", "checks.py"), "w") as fh:
            fh.write("def t():\n    assert True\n")
        with open(os.path.join(d, "notes.tex"), "w") as fh:
            fh.write(r"the answer is 42 \src{ans}." + "\n")
        with open(os.path.join(d, "provenance.json"), "w") as fh:
            fh.write('{"ans": {"statement":"42","type":"check",'
                     '"reproduce":"out/checks.py::t","detail":"t"}}')
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO, "bin", "check_provenance.py"),
             "notes.tex", "provenance.json"],
            cwd=d, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
