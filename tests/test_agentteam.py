"""Smoke + behavior tests for agent-team. Zero dependencies (stdlib unittest + the mock backend).

Run:  python -m unittest discover -s tests    (or: python tests/test_agentteam.py)
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from agentteam import engine, recipes, roles, staffing  # noqa: E402
from agentteam.jobs import Job                          # noqa: E402

TEAM = {"lead": "pi", "workers": ["worker"], "verifier": "verifier", "extra": ["writer"]}


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


class Staffing(unittest.TestCase):
    """Per-role backend/model/effort/when (docs/BACKLOG.md #1, #8)."""

    def test_falls_back_to_job_defaults(self):
        spec = {"backend": "codex", "model": "gpt-5.6-sol", "effort": "high", "roles": {}}
        self.assertEqual(staffing.resolve(spec, "worker"),
                         {"backend": "codex", "model": "gpt-5.6-sol", "effort": "high"})

    def test_role_override_wins(self):
        spec = {"backend": "codex", "model": "gpt-5.6-sol", "effort": "high",
                "roles": {"worker": {"effort": "medium"}, "verifier": {"effort": "xhigh"}}}
        self.assertEqual(staffing.resolve(spec, "worker")["effort"], "medium")
        self.assertEqual(staffing.resolve(spec, "verifier")["effort"], "xhigh")
        self.assertEqual(staffing.resolve(spec, "verifier")["model"], "gpt-5.6-sol")

    def test_backend_switch_does_not_inherit_the_other_backends_model(self):
        """A claude verifier must not be handed a codex model name."""
        spec = {"backend": "codex", "model": "gpt-5.6-sol", "effort": "high",
                "roles": {"verifier": {"backend": "claude"}}}
        got = staffing.resolve(spec, "verifier")
        self.assertEqual(got["backend"], "claude")
        self.assertEqual(got["model"], "claude-opus-4-8[1m]")  # claude's default, not codex's
        self.assertEqual(got["effort"], "high")                # effort IS portable, so it carries

    def test_cli_parsing(self):
        got = staffing.parse_cli(["worker:effort=medium",
                                  "verifier:effort=xhigh,backend=claude",
                                  "writer:when=last"])
        self.assertEqual(got["worker"], {"effort": "medium"})
        self.assertEqual(got["verifier"], {"effort": "xhigh", "backend": "claude"})
        self.assertEqual(got["writer"], {"when": "last"})

    def test_cli_rejects_typos(self):
        for bad in (["verifier"], ["verifier:xhigh"], ["verifier:effortt=xhigh"],
                    ["notarole:effort=high"], ["verifier:backend=gemini"]):
            with self.assertRaises(ValueError, msg=bad):
                staffing.normalize(staffing.parse_cli(bad), TEAM, source="--role")

    def test_layers_merge_key_by_key(self):
        merged = staffing.merge({"verifier": {"model": "m1", "effort": "high"}},
                                {"verifier": {"effort": "xhigh"}})
        self.assertEqual(merged["verifier"], {"model": "m1", "effort": "xhigh"})

    def test_pi_lines_parsed_out_of_prose(self):
        text = ("Here is the plan.\n\nWORKERS: 3\n"
                "ROLE worker: effort=medium   (grind work, shallow is fine)\n"
                "ROLE verifier: effort=xhigh, backend=claude\n"
                "I will revisit this next round.\n")
        got = staffing.parse_pi(text)
        self.assertEqual(got, {"worker": {"effort": "medium"},
                               "verifier": {"effort": "xhigh", "backend": "claude"}})

    def test_policy_clamps_the_pi(self):
        policy = {"effort_max": "high", "backends_allowed": ["codex"]}
        clamped, notes = staffing.clamp(
            {"verifier": {"effort": "xhigh", "backend": "claude", "model": "claude-opus-4-8[1m]"},
             "worker": {"effort": "low"}}, policy)
        self.assertEqual(clamped["verifier"], {"effort": "high"})  # backend + its model dropped
        self.assertEqual(clamped["worker"], {"effort": "low"})     # under the ceiling, untouched
        self.assertEqual(len(notes), 2)

    def test_policy_absent_means_no_clamp(self):
        proposed = {"verifier": {"effort": "xhigh", "backend": "claude"}}
        clamped, notes = staffing.clamp(proposed, {})
        self.assertEqual(clamped, proposed)
        self.assertEqual(notes, [])

    def test_when_schedule(self):
        spec = {"roles": {"writer": {"when": "last"}, "test-writer": {"when": "first"},
                          "auditor": {"when": [2, 4]}}}
        runs = lambda role, r, final: staffing.runs_in_round(spec, role, round_no=r, final=final)
        self.assertFalse(runs("writer", 1, False))
        self.assertTrue(runs("writer", 3, True))
        self.assertTrue(runs("test-writer", 1, False))
        self.assertFalse(runs("test-writer", 2, False))
        self.assertEqual([r for r in range(1, 6) if runs("auditor", r, False)], [2, 4])
        self.assertTrue(runs("unlisted", 1, False))  # default: every round


class PIStaffing(unittest.TestCase):
    """The PI may set per-role depth; policy.json bounds it, and one bad line is never fatal."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["AGENT_TEAM_PROJECT"] = self._tmp
        self.job = Job.create(job_type="derive", intent="staffing", backend="mock",
                              model=None, effort=None, rounds=1)

    def tearDown(self):
        os.environ.pop("AGENT_TEAM_PROJECT", None)

    def _apply(self, text, policy):
        spec = self.job.load_spec()
        engine._apply_pi_roles(self.job, spec, text, policy)
        events = []
        if os.path.exists(self.job.log_path):  # nothing to say -> nothing logged
            with open(self.job.log_path) as fh:
                events = [json.loads(ln) for ln in fh if ln.strip()]
        return spec.get("roles", {}), [e for e in events if e["event"] == "pi_roles"]

    def test_applies_and_clamps(self):
        roles, events = self._apply(
            "ROLE worker: effort=medium\nROLE verifier: effort=xhigh\n", {"effort_max": "high"})
        self.assertEqual(roles["worker"], {"effort": "medium"})
        self.assertEqual(roles["verifier"], {"effort": "high"})  # clamped
        self.assertEqual(len(events[0]["clamped"]), 1)

    def test_bad_line_is_dropped_not_fatal(self):
        roles, events = self._apply(
            "ROLE nosuchrole: effort=high\nROLE worker: effort=low\n", {})
        self.assertEqual(roles, {"worker": {"effort": "low"}})  # the good line survived
        self.assertEqual(len(events[0]["rejected"]), 1)

    def test_no_role_lines_leaves_the_spec_uniform(self):
        roles, events = self._apply("Just a plan, no staffing lines.\nWORKERS: 3\n", {})
        self.assertEqual(roles, {})
        self.assertEqual(events, [])


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

    def test_per_role_config_and_schedule_end_to_end(self):
        """A `when: last` writer costs one hand-off, not one per round; per-role effort
        reaches the actual call and is logged for provenance."""
        job = Job.create(job_type="derive", intent="one write-up pass, at the end",
                         backend="mock", model=None, effort=None, rounds=3,
                         roles={"writer": {"when": "last"}, "worker": {"effort": "low"}})
        self.assertEqual(job.load_spec()["roles"]["writer"], {"when": "last"})
        engine.run(job)

        with open(os.path.join(job.dir, "work", "mock_activity.log")) as fh:
            ran = fh.read()
        self.assertEqual(ran.count("writer ran"), 1)    # once, on the final round
        self.assertEqual(ran.count("verifier ran"), 3)  # verification stays every round

        with open(job.log_path) as fh:
            calls = [e for e in (json.loads(ln) for ln in fh if ln.strip())
                     if e.get("event") == "role_call"]
        workers = [c for c in calls if c["role"].startswith("worker")]
        self.assertEqual(len(workers), 6)  # 2 workers x 3 rounds
        self.assertTrue(all(c["effort"] == "low" for c in workers))
        self.assertTrue(all(c["effort"] is None for c in calls if c["role"] == "verifier"))

        with open(job.view_path) as fh:
            view = fh.read()
        self.assertNotIn("{{", view)
        self.assertIn("Staffing", view)  # the table appears once staffing isn't uniform

    def test_last_role_still_runs_when_the_lead_signals_done_early(self):
        """`last` means the last round of THIS run -- otherwise an early finish would leave
        the deliverable unwritten."""
        job = Job.create(job_type="derive", intent="stop early", backend="mock",
                         model=None, effort=None, rounds=5,
                         roles={"writer": {"when": "last"}})
        real_done = engine.done_signal
        engine.done_signal = lambda res: True  # the mock lead can't emit [[DONE]] itself
        try:
            status = engine.run(job)
        finally:
            engine.done_signal = real_done
        self.assertEqual(status, "done")
        self.assertEqual(job.load_spec()["round"], 1)  # ended 4 rounds early
        with open(os.path.join(job.dir, "work", "mock_activity.log")) as fh:
            self.assertEqual(fh.read().count("writer ran"), 1)


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
