"""Smoke + behavior tests for agent-team. Zero dependencies (stdlib unittest + the mock backend).

Run:  python -m unittest discover -s tests    (or: python tests/test_agentteam.py)
"""

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from unittest import mock                               # noqa: E402

from agentteam import backends, claims, cli, engine, recipes, roles, staffing, tripwires  # noqa: E402
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

    def test_every_recipe_has_a_check_spine_and_a_provenance_registry(self):
        """A deliverable nobody can retrace is not a deliverable -- including for code jobs."""
        for name in recipes.available():
            r = recipes.load(name)
            self.assertTrue(r["checks"]["command"].strip(), f"{name} has no check spine")
            self.assertTrue(r.get("provenance", {}).get("registry"), f"{name} seeds no registry")

    def test_feature_recipe_authors_human_readable_notes(self):
        """The writer role authors `deliverable.path`, so a code job's deliverable must be tex."""
        r = recipes.load("feature")
        self.assertEqual(r["deliverable"]["type"], "tex")
        self.assertIn("writer", r["team"]["extra"])
        self.assertEqual(r["team"]["verifier"], "code-reviewer")

    def test_a_code_job_is_a_worker_and_a_reviewer_not_a_committee(self):
        """One pair of hands, one independent check, and the write-up once at the end.

        The first real feature run staffed 2 workers + reviewer + test-writer + a writer every
        round: 6 calls a round, of which the test-writer alone took 38% re-running a 313-test
        suite that the worker had already run. Tests ship with the code (roles/worker.md); the
        reviewer's job includes whether the change is covered."""
        r = recipes.load("feature")
        self.assertEqual(r["team"]["extra"], ["writer"])
        self.assertEqual(r["roles"]["writer"]["when"], "last")
        self.assertEqual(r["defaults"]["worker_count"], 1)

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
        # claude's own default, whatever it currently is -- never codex's model name
        self.assertEqual(got["model"], backends.DEFAULTS["claude"]["model"])
        self.assertNotEqual(got["model"], spec["model"])
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

    def test_code_job_change_set_captures_new_files(self):
        """A feature job's output is mostly *new* files, which a bare `git diff` never shows."""
        def git(*args):
            subprocess.run(["git", *args], cwd=self._tmp, capture_output=True, check=True)

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "test")
        with open(os.path.join(self._tmp, "existing.py"), "w") as fh:
            fh.write("x = 1\n")
        git("add", "-A")
        git("commit", "-qm", "base")

        job = Job.create(job_type="feature", intent="add a module", backend="mock",
                         model=None, effort=None, rounds=1)
        self.assertTrue(job.load_spec()["base_commit"], "code job must anchor to a commit")

        # what a worker would leave behind: one new file, one edit
        with open(os.path.join(self._tmp, "brand_new.py"), "w") as fh:
            fh.write("def added():\n    return 42\n")
        with open(os.path.join(self._tmp, "existing.py"), "w") as fh:
            fh.write("x = 2\n")

        engine._finalize_deliverable(job, job.load_spec())
        with open(os.path.join(job.out_dir, "changes.diff")) as fh:
            diff = fh.read()
        self.assertIn("brand_new.py", diff)   # the new file is the point
        self.assertIn("existing.py", diff)
        self.assertTrue(os.path.exists(os.path.join(job.dir, "out/notes.tex")))

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


class ClaimHarvesting(unittest.TestCase):
    """The round's durable record. A parser that fails quietly here costs more than any other
    defect in the tool: with no claims, later rounds re-derive everything from scratch."""

    def test_bare_lines_still_parse(self):
        """codex's house style -- the only one the original line-anchored regex handled."""
        text = ("VERIFIED: Frequency-sign-free census is exactly 20 of 35 words.\n"
                "UNCLEAR: the n=8 case — no exact shell available.\n")
        got = claims.parse(text)
        self.assertEqual([c["status"] for c in got], ["verified", "unclear"])
        self.assertTrue(got[0]["text"].startswith("Frequency-sign-free census"))

    def test_markdown_decorated_lines_parse(self):
        """claude's house style, which silently harvested ZERO claims for a whole 8-round run."""
        text = (
            "I re-ran everything independently rather than trusting the worker.\n"
            "**VERIFIED: commit `dcde3e0` is the split-safe loader** — `git show --stat` agrees\n"
            "- `VERIFIED: provenance checker reports 35 tags, 0 errors`\n"
            "  * REFUTED: the blend is C2 — the second derivative jumps at x_low\n"
            "3. __UNCLEAR__: whether status rows should be rejected\n")
        got = claims.parse(text)
        self.assertEqual([c["status"] for c in got],
                         ["verified", "verified", "refuted", "unclear"])
        self.assertNotIn("*", got[0]["text"])          # decoration stripped, not carried in
        self.assertTrue(got[0]["text"].startswith("commit"))

    def test_a_claims_block_wins_over_prose(self):
        text = ('Prose says VERIFIED: something sloppy\n'
                '```claims\n'
                '[{"status":"verified","text":"the real one"},'
                ' {"status":"refuted","text":"the broken one"}]\n'
                '```\n')
        got = claims.parse(text)
        self.assertEqual(got, [{"status": "verified", "text": "the real one"},
                               {"status": "refuted", "text": "the broken one"}])

    def test_malformed_block_falls_back_to_the_prose_scan(self):
        text = "VERIFIED: the fallback worked\n```claims\nnot json at all\n```\n"
        self.assertEqual(claims.parse(text),
                         [{"status": "verified", "text": "the fallback worked"}])

    def test_duplicates_collapse(self):
        text = "VERIFIED: same thing\nVERIFIED: same thing\n"
        self.assertEqual(len(claims.parse(text)), 1)

    def test_unharvested_is_the_alarm(self):
        """Mentions a status but nothing parsed => the contract is broken, not "nothing verified"."""
        self.assertTrue(claims.unharvested("the VERIFIED state is unclear to me, honestly"))
        self.assertFalse(claims.unharvested("VERIFIED: fine"))
        self.assertFalse(claims.unharvested("no statuses mentioned at all"))


class TaskPlanning(unittest.TestCase):
    """Surplus tasks are queued, never dropped. Truncating the lead's plan to the worker count is
    how "implement gauge.py" got assigned eight times and executed zero times."""

    PLAN = ("Plan: finish the foundations.\n"
            "1. Reconcile the provenance registry and commit.\n"
            "2. Implement fitlib/loader.py with the split guards.\n"
            "3. Implement fitlib/gauge.py with the frozen bands.\n"
            "4. Review the above and run the spine.\n")

    def test_one_worker_defers_the_rest_instead_of_discarding_them(self):
        assigned, deferred = engine._plan_tasks(self.PLAN, 1, [])
        self.assertEqual(len(assigned), 1)
        self.assertIn("Reconcile", assigned[0])
        self.assertEqual(len(deferred), 3)
        self.assertTrue(any("gauge.py" in t for t in deferred))

    def test_the_backlog_runs_first_next_round(self):
        _, deferred = engine._plan_tasks(self.PLAN, 1, [])
        assigned, still = engine._plan_tasks(self.PLAN, 1, deferred)
        self.assertIn("loader.py", assigned[0])       # the queue advanced
        self.assertEqual(len(still), 3)               # and did not grow: same items, dedup'd

    def test_more_workers_than_tasks_does_not_duplicate_work(self):
        assigned, deferred = engine._plan_tasks("1. only one thing to do\n", 4, [])
        self.assertEqual(len(assigned), 1)            # not the same task handed to four workers
        self.assertEqual(deferred, [])

    def test_a_plan_with_no_list_still_yields_a_task(self):
        assigned, deferred = engine._plan_tasks("no numbered items here", 2, [])
        self.assertEqual(len(assigned), 1)
        self.assertEqual(deferred, [])


class Tripwires(unittest.TestCase):
    """Stop when progress flatlines -- at a round boundary, never mid-flight."""

    def _state(self, rounds):
        state = {}
        for i, kw in enumerate(rounds, start=1):
            tripwires.record(state, round_no=i, **kw)
        return state

    HEALTHY = {"claims": 3, "new_verified": 2, "tasks": ["do a thing"],
               "tokens": 1000, "checks_passed": True}

    def test_healthy_progress_does_not_trip(self):
        state = self._state([dict(self.HEALTHY, tasks=[f"task {i}"]) for i in range(4)])
        self.assertIsNone(tripwires.evaluate({}, state))

    def test_no_parsable_claims_trips_immediately(self):
        state = self._state([dict(self.HEALTHY, claims=0, new_verified=0)])
        self.assertIn("no parsable claims", tripwires.evaluate({}, state))

    def test_two_rounds_without_new_verified_claims_trips(self):
        state = self._state([self.HEALTHY,
                             dict(self.HEALTHY, new_verified=0, tasks=["a"]),
                             dict(self.HEALTHY, new_verified=0, tasks=["b"])])
        self.assertIn("no new verified claims", tripwires.evaluate({}, state))

    def test_the_same_leading_task_three_rounds_running_is_a_livelock(self):
        same = dict(self.HEALTHY, tasks=["reconcile the provenance registry and commit"])
        state = self._state([same, same, same])
        self.assertIn("not advancing", tripwires.evaluate({}, state))

    def test_a_code_job_whose_project_never_changes_trips(self):
        frozen = dict(self.HEALTHY, fingerprint="deadbeef")
        state = self._state([dict(frozen, tasks=["a"]), dict(frozen, tasks=["b"]),
                             dict(frozen, tasks=["c"])])
        self.assertIn("has not changed", tripwires.evaluate({"kind": "code"}, state))
        self.assertIsNone(tripwires.evaluate({"kind": "understanding"}, state))  # not a code job

    def test_a_runaway_round_trips(self):
        rounds = [dict(self.HEALTHY, tasks=[f"t{i}"], tokens=1_000_000) for i in range(3)]
        rounds.append(dict(self.HEALTHY, tasks=["t9"], tokens=9_000_000))
        self.assertIn("over 3x", tripwires.evaluate({}, self._state(rounds)))

    def test_tripwires_can_be_switched_off(self):
        state = self._state([dict(self.HEALTHY, claims=0, new_verified=0)])
        self.assertIsNone(tripwires.evaluate({"tripwires": False}, state))


class ClaudeUsage(unittest.TestCase):
    """input_tokens alone omits the cache, which is most of an agentic call's real spend."""

    def test_cache_tokens_are_counted(self):
        itok, otok = backends._claude_usage({"usage": {
            "input_tokens": 1_200, "cache_creation_input_tokens": 40_000,
            "cache_read_input_tokens": 900_000, "output_tokens": 7_000}})
        self.assertEqual((itok, otok), (941_200, 7_000))

    def test_falls_back_to_the_per_model_breakdown(self):
        itok, otok = backends._claude_usage({"modelUsage": {
            "claude-opus-5[1m]": {"input_tokens": 10, "cache_read_input_tokens": 500,
                                  "output_tokens": 20}}})
        self.assertEqual((itok, otok), (510, 20))

    def test_no_usage_at_all_is_zero_not_a_crash(self):
        self.assertEqual(backends._claude_usage({}), (0, 0))


class RoundLoopIntegration(unittest.TestCase):
    """End-to-end on the mock backend: the mechanisms that failed in the first real feature run."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["AGENT_TEAM_PROJECT"] = self._tmp

    def tearDown(self):
        os.environ.pop("AGENT_TEAM_PROJECT", None)

    def _job(self, **kw):
        kw.setdefault("job_type", "derive")
        kw.setdefault("intent", "exercise the loop")
        kw.setdefault("backend", "mock")
        kw.setdefault("model", None)
        kw.setdefault("effort", None)
        kw.setdefault("checkpoint_rounds", 0)
        return Job.create(**kw)

    def test_claims_reach_durable_state(self):
        """The regression that cost a whole run: a verifier's report must land in state.claims."""
        job = self._job(rounds=2)
        engine.run(job)
        state = job.load_state()
        verified = [c for c in state["claims"] if c["status"] == "verified"]
        self.assertTrue(verified, "no claims harvested -- the round has no durable record")
        self.assertEqual([p["claims"] for p in state["progress"]], [2, 2])
        self.assertTrue(all(p["new_verified"] >= 1 for p in state["progress"]))

    def test_surplus_tasks_are_queued_not_dropped(self):
        job = self._job(rounds=1, worker_count=1)
        engine.run(job)
        self.assertTrue(job.load_state()["backlog"], "the lead's extra tasks vanished")
        with open(job.log_path) as fh:
            events = [json.loads(ln) for ln in fh if ln.strip()]
        self.assertTrue([e for e in events if e.get("event") == "tasks_deferred"])

    def test_every_role_call_is_kept_on_disk(self):
        job = self._job(rounds=1)
        engine.run(job)
        files = sorted(os.listdir(job.transcript_dir))
        self.assertIn("r01-lead.txt", files)
        self.assertIn("r01-verifier.txt", files)
        with open(os.path.join(job.transcript_dir, "r01-lead.txt")) as fh:
            body = fh.read()
        self.assertIn("# role: pi", body)          # header records who/what/how much
        self.assertIn("mock task one", body)       # and the full reply, not a 600-char excerpt

    def test_the_writer_is_skipped_when_nothing_new_was_verified(self):
        state = {"claims": [{"status": "verified", "text": "x"}], "writer_seen_verified": 0}
        self.assertTrue(engine._writer_has_work(state, final=False))
        state["writer_seen_verified"] = 1
        self.assertFalse(engine._writer_has_work(state, final=False))
        self.assertTrue(engine._writer_has_work(state, final=True))  # final pass always runs

    def test_a_writer_with_no_new_claims_costs_nothing(self):
        job = self._job(rounds=2)
        with mock.patch.object(engine.claims_mod, "parse", return_value=[]):
            engine.run(job)
        with open(job.log_path) as fh:
            events = [json.loads(ln) for ln in fh if ln.strip()]
        self.assertTrue([e for e in events if e.get("event") == "writer_skipped"])

    def test_a_flatlined_run_stops_itself(self):
        """With nothing verifiable, the job stops at the round boundary instead of spending out."""
        job = self._job(rounds=8)
        spec = job.load_spec()
        spec["tripwires"] = True          # mock jobs disable them; this test is about the wire
        job.save_spec(spec)
        with mock.patch.object(engine.claims_mod, "parse", return_value=[]):
            status = engine.run(job)
        self.assertEqual(status, "stopped")
        self.assertEqual(job.load_spec()["round"], 1)      # after ONE round, not eight
        self.assertIn("no parsable claims", job.load_state()["stop_reason"])
        with open(job.view_path) as fh:
            self.assertIn("stopped early", fh.read())

    def test_a_blocked_lead_hands_the_job_back(self):
        job = self._job(rounds=6)
        with mock.patch.object(engine, "blocked_signal", return_value=True):
            status = engine.run(job)
        self.assertEqual(status, "stopped")
        self.assertEqual(job.load_spec()["round"], 1)
        self.assertIn("BLOCKED", job.load_state()["stop_reason"])

    def test_a_resumed_run_clears_the_old_stop_reason(self):
        job = self._job(rounds=4)
        state = job.load_state()
        state["stop_reason"] = "an old reason"
        job.save_state(state)
        engine.run(job, max_new_rounds=1)
        self.assertIsNone(job.load_state().get("stop_reason"))


class AcceptanceGate(unittest.TestCase):
    """The human's definition of done -- the team can pass it, never edit it."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["AGENT_TEAM_PROJECT"] = self._tmp

    def tearDown(self):
        os.environ.pop("AGENT_TEAM_PROJECT", None)

    def _job(self, acceptance, guard=None):
        return Job.create(job_type="derive", intent="gated", backend="mock", model=None,
                          effort=None, rounds=1, checkpoint_rounds=0,
                          acceptance=acceptance, acceptance_guard=guard)

    def test_a_passing_gate_is_recorded(self):
        job = self._job("exit 0")
        engine.run(job)
        self.assertTrue(job.load_state()["acceptance"]["passed"])

    def test_a_failing_gate_blocks_done(self):
        job = self._job("exit 1")
        with mock.patch.object(engine, "done_signal", return_value=True):
            status = engine.run(job)
        self.assertEqual(status, "stopped")           # the lead said done; the gate said no
        self.assertFalse(job.load_state()["acceptance"]["passed"])

    def test_a_passing_gate_lets_done_through(self):
        job = self._job("exit 0")
        with mock.patch.object(engine, "done_signal", return_value=True):
            self.assertEqual(engine.run(job), "done")

    def test_editing_the_gate_fails_it(self):
        target = os.path.join(self._tmp, "test_acceptance.py")
        with open(target, "w") as fh:
            fh.write("assert True\n")
        job = self._job("exit 0", guard=["test_acceptance.py"])
        self.assertTrue(job.load_spec()["acceptance"]["files"]["test_acceptance.py"])
        with open(target, "w") as fh:                 # the team "fixes" the gate
            fh.write("# nothing to see here\n")
        engine.run(job)
        acc = job.load_state()["acceptance"]
        self.assertFalse(acc["passed"])
        self.assertEqual(acc["tampered"], ["test_acceptance.py"])
        self.assertIn("never edited", acc["detail"])

    def test_no_gate_configured_is_not_a_gate(self):
        job = Job.create(job_type="derive", intent="ungated", backend="mock", model=None,
                         effort=None, rounds=1, checkpoint_rounds=0)
        engine.run(job)
        self.assertEqual(job.load_state()["acceptance"], {})


class ReadableDeliverable(unittest.TestCase):
    """A deliverable the human cannot open is not a deliverable."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["AGENT_TEAM_PROJECT"] = self._tmp

    def tearDown(self):
        os.environ.pop("AGENT_TEAM_PROJECT", None)

    def test_a_tex_deliverable_is_compiled_to_pdf(self):
        if not shutil.which("pdflatex"):
            self.skipTest("pdflatex not installed")
        job = Job.create(job_type="derive", intent="readable", backend="mock", model=None,
                         effort=None, rounds=1, checkpoint_rounds=0)
        tex = os.path.join(job.dir, "out", "notes.tex")
        with open(tex, "w") as fh:
            fh.write("\\documentclass{article}\\begin{document}Hello.\\end{document}\n")
        engine._finalize_deliverable(job, job.load_spec())
        pdf = os.path.join(job.dir, "out", "notes.pdf")
        self.assertTrue(os.path.exists(pdf) and os.path.getsize(pdf) > 0)

    def test_an_empty_tex_deliverable_is_not_compiled(self):
        job = Job.create(job_type="derive", intent="empty", backend="mock", model=None,
                         effort=None, rounds=1, checkpoint_rounds=0)
        tex = os.path.join(job.dir, "out", "notes.tex")
        open(tex, "w").close()
        engine._finalize_deliverable(job, job.load_spec())   # must not raise
        self.assertFalse(os.path.exists(os.path.join(job.dir, "out", "notes.pdf")))


class Checkpoint(unittest.TestCase):
    """A fresh job stops for a look before spending its whole budget."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["AGENT_TEAM_PROJECT"] = self._tmp

    def tearDown(self):
        os.environ.pop("AGENT_TEAM_PROJECT", None)

    def test_first_run_stops_at_the_checkpoint_then_resumes(self):
        job = Job.create(job_type="derive", intent="checkpointed", backend="mock", model=None,
                         effort=None, rounds=8, checkpoint_rounds=2)
        self.assertEqual(job.load_spec()["checkpoint_rounds"], 2)

        args = argparse.Namespace(cmd="run", id=job.id, rounds=None, say=None)
        cli._cmd_run(args)
        self.assertEqual(job.load_spec()["round"], 2)        # not 8
        self.assertEqual(job.load_spec()["rounds"], 8)       # budget itself untouched

        cli._cmd_run(argparse.Namespace(cmd="resume", id=job.id, rounds=None, say=None))
        self.assertEqual(job.load_spec()["round"], 8)        # resume runs it out

    def test_an_explicit_round_count_overrides_the_checkpoint(self):
        job = Job.create(job_type="derive", intent="explicit", backend="mock", model=None,
                         effort=None, rounds=8, checkpoint_rounds=2)
        cli._cmd_run(argparse.Namespace(cmd="run", id=job.id, rounds=3, say=None))
        self.assertEqual(job.load_spec()["round"], 3)

    def test_a_failed_compile_leaves_no_litter_in_out(self):
        if not shutil.which("pdflatex"):
            self.skipTest("pdflatex not installed")
        job = Job.create(job_type="derive", intent="broken tex", backend="mock", model=None,
                         effort=None, rounds=1, checkpoint_rounds=0)
        tex = os.path.join(job.dir, "out", "notes.tex")
        with open(tex, "w") as fh:
            fh.write("\\documentclass{article}\\begin{document}\\undefinedmacro\\end{document}\n")
        engine._finalize_deliverable(job, job.load_spec())
        litter = [f for f in os.listdir(os.path.join(job.dir, "out"))
                  if f.endswith((".aux", ".log", ".out", ".toc"))]
        self.assertEqual(litter, [], f"pdflatex left {litter} in out/")


class SpineRunner(unittest.TestCase):
    """bin/run_spine.sh: a spine that dies early must not read as a pass."""

    def _run(self, body, executable=True, sentinel=None):
        d = tempfile.mkdtemp()
        script = os.path.join(d, "checks.sh")
        with open(script, "w") as fh:
            fh.write(body)
        if executable:
            os.chmod(script, 0o755)
        cmd = ["bash", os.path.join(REPO, "bin", "run_spine.sh"), script]
        if sentinel:
            cmd.append(sentinel)
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_a_complete_spine_passes(self):
        got = self._run("#!/bin/sh\necho running the suite\necho SPINE OK\n")
        self.assertEqual(got.returncode, 0)
        self.assertIn("running the suite", got.stdout)

    def test_the_zsh_under_bash_death_no_longer_reads_as_a_pass(self):
        """The real defect: `${0:a:h:h}` under bash 3.2 exits 0 having run nothing."""
        got = self._run("#!/bin/bash\nset -u\nJOB=${0:a:h:h}\necho suite ran\necho SPINE OK\n")
        self.assertNotEqual(got.returncode, 0, "a spine that ran nothing was reported as passing")
        self.assertNotIn("suite ran", got.stdout)
        self.assertIn("never printed", got.stdout)

    def test_the_shebang_chooses_the_interpreter(self):
        """That same script under its own zsh shebang works -- so honour it."""
        if not shutil.which("zsh"):
            self.skipTest("zsh not installed")
        got = self._run("#!/bin/zsh\nset -u\nJOB=${0:a:h}\necho suite ran\necho SPINE OK\n")
        self.assertEqual(got.returncode, 0, got.stdout)
        self.assertIn("suite ran", got.stdout)

    def test_a_real_failure_still_fails(self):
        got = self._run("#!/bin/sh\necho a test failed\nexit 1\n")
        self.assertEqual(got.returncode, 1)

    def test_a_missing_spine_is_not_a_failure(self):
        got = subprocess.run(["bash", os.path.join(REPO, "bin", "run_spine.sh"),
                              "/nonexistent/checks.sh"], capture_output=True, text=True)
        self.assertEqual(got.returncode, 0)

    def test_a_non_executable_spine_still_runs(self):
        got = self._run("echo suite ran\necho SPINE OK\n", executable=False)
        self.assertEqual(got.returncode, 0)
        self.assertIn("suite ran", got.stdout)

    def test_a_deliverable_with_figures_compiles(self):
        """\\includegraphics resolves against the working directory, not the source file --
        so a notes.tex referencing figures/ only builds if pdflatex runs in out/."""
        if not shutil.which("pdflatex"):
            self.skipTest("pdflatex not installed")
        job = Job.create(job_type="derive", intent="figures", backend="mock", model=None,
                         effort=None, rounds=1, checkpoint_rounds=0)
        out = os.path.join(job.dir, "out")
        os.makedirs(os.path.join(out, "figures"), exist_ok=True)
        # a 1x1 PNG, so \includegraphics has something real to find
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAA"
            "AABJRU5ErkJggg==")
        with open(os.path.join(out, "figures", "plot.png"), "wb") as fh:
            fh.write(png)
        with open(os.path.join(out, "notes.tex"), "w") as fh:
            fh.write("\\documentclass{article}\\usepackage{graphicx}\\begin{document}\n"
                     "\\includegraphics[width=2cm]{figures/plot.png}\n\\end{document}\n")
        engine._finalize_deliverable(job, job.load_spec())
        pdf = os.path.join(out, "notes.pdf")
        self.assertTrue(os.path.exists(pdf) and os.path.getsize(pdf) > 0,
                        "a deliverable with figures did not build")


class ProvenancePathRoots(unittest.TestCase):
    """A code job's registry is project-relative; the checker runs from the job dir."""

    def test_project_relative_paths_resolve_from_the_job_dir(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "tests"))
        open(os.path.join(root, "tests", "test_x.py"), "w").close()
        job_out = os.path.join(root, "jobs", "j1", "out")
        os.makedirs(job_out)
        with open(os.path.join(job_out, "notes.tex"), "w") as fh:
            fh.write(r"the claim \src{k}." + "\n")
        with open(os.path.join(job_out, "provenance.json"), "w") as fh:
            json.dump({"k": {"statement": "s", "type": "check",
                             "reproduce": "tests/test_x.py::test_x"}}, fh)
        got = subprocess.run(
            [sys.executable, os.path.join(REPO, "bin", "check_provenance.py"),
             "out/notes.tex", "out/provenance.json"],
            cwd=os.path.join(root, "jobs", "j1"), capture_output=True, text=True)
        self.assertEqual(got.returncode, 0, got.stdout)

    def test_a_genuinely_missing_path_still_fails_and_says_where_it_looked(self):
        root = tempfile.mkdtemp()
        with open(os.path.join(root, "notes.tex"), "w") as fh:
            fh.write(r"the claim \src{k}." + "\n")
        with open(os.path.join(root, "provenance.json"), "w") as fh:
            json.dump({"k": {"statement": "s", "type": "check",
                             "reproduce": "tests/nope.py"}}, fh)
        got = subprocess.run(
            [sys.executable, os.path.join(REPO, "bin", "check_provenance.py"),
             "notes.tex", "provenance.json"], cwd=root, capture_output=True, text=True)
        self.assertEqual(got.returncode, 1)
        self.assertIn("looked in:", got.stdout)
