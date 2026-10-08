"""
Tests for pipeline_log_generator.py.

They run every failure kind and every scenario at very high speed and check that
the output matches the patterns used by bindplane/cicd-structured-processor.yaml.
KEEP THE PATTERNS BELOW IN SYNC with that file (they are the same regexes, just
without the YAML/OTTL double-escaping).

Run:  python3 -m unittest discover -s tests -v
"""
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pipeline_log_generator as g  # noqa: E402

SPEED = 5000  # compress time so the whole suite runs in seconds

LINE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2}T[^\]]+Z)\] ?(?P<msg>.*)$")
ERROR = re.compile(r"ERROR|FAILED|FAILURE|FATAL|Exception|Error:|BackOff|ErrImagePull|^\s+at |^Pipeline failed|\u2502 (CRITICAL|HIGH) \u2502")
WARN = re.compile(r"\[WARNING\]|^WARNING:")
CHECKOUT = re.compile(r"^Checking out Revision (?P<sha>[0-9a-f]{40}) \(origin/(?P<branch>[^)]+)\)")
STAGE = re.compile(r"^\[Pipeline\] \{ \((?P<stage>[^)]+)\)")
STATUS = re.compile(r"^Finished: (?P<status>[A-Z]+)$")
FAILED_STAGE = re.compile(r"^Pipeline failed in stage: (?P<failed_stage>.+)$")
STARTED = re.compile(r"^Started by (?P<trigger>GitHub push by|user) (?P<author>\S+)$")
PATH = re.compile(r"/jobs/(?P<pipeline_name>[^/]+)/builds/(?P<build_number>\d+)/log$")
COMMIT_MSG = re.compile(r"^Commit message: .(?P<commit_msg>.*).$")
PROCESSOR_YAML = os.path.join(os.path.dirname(__file__), "..", "bindplane", "cicd-structured-processor.yaml")


def read(base, res):
    path = os.path.join(base, "jobs", res["job"], "builds", str(res["build"]), "log")
    with open(path, encoding="utf-8") as f:
        return path, f.read().splitlines()


def msgs(lines):
    out = []
    for ln in lines:
        m = LINE.match(ln)
        assert m, f"line does not match the Timestamper format: {ln!r}"
        out.append(m.group("msg"))
    return out


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def run_one(self, job, fail=None, overrides=None):
        res = g.run_pipeline(self.base, job, g.JOBS[job], SPEED, fail=fail, overrides=overrides)
        path, lines = read(self.base, res)
        return res, path, lines

    def test_success_run_is_clean(self):
        for job in g.JOBS:
            res, path, lines = self.run_one(job, overrides={"branch": "main"})
            m = msgs(lines)
            self.assertEqual(res["status"], "SUCCESS")
            self.assertEqual(STATUS.match(m[-1]).group("status"), "SUCCESS")
            self.assertEqual(sum(1 for x in m if ERROR.search(x)), 0, "no line of a green run should be classified ERROR")
            stages = [STAGE.match(x).group("stage") for x in m if STAGE.match(x)]
            self.assertEqual(stages[:7], [n for n, _ in g.STAGES])
            self.assertEqual(sum(1 for x in m if CHECKOUT.match(x)), 1)
            p = PATH.search(str(path).replace("\\", "/"))
            self.assertEqual(p.group("pipeline_name"), job)
            self.assertEqual(p.group("build_number"), str(res["build"]))

    def test_every_failure_kind(self):
        for kind, stage in g.FAILURE_STAGE.items():
            with self.subTest(kind=kind):
                res, _, lines = self.run_one("payment-api-ci-cd", fail=kind, overrides={"branch": "main"})
                m = msgs(lines)
                self.assertEqual(res["status"], "FAILURE")
                self.assertEqual(res["failed_stage"], stage)
                self.assertEqual(STATUS.match(m[-1]).group("status"), "FAILURE")
                failed = [FAILED_STAGE.match(x).group("failed_stage") for x in m if FAILED_STAGE.match(x)]
                self.assertEqual(failed, [stage])
                self.assertGreaterEqual(sum(1 for x in m if ERROR.search(x)), 2)
                self.assertTrue(any(x.startswith("ERROR: script returned exit code") for x in m))
                # stages after the failure must be marked skipped, never executed
                idx = [n for n, _ in g.STAGES].index(stage)
                for later, _ in g.STAGES[idx + 1:]:
                    self.assertTrue(any(f'Stage "{later}" skipped' in x for x in m), f"{later} not skipped")

    def test_stack_traces_are_classified_as_errors(self):
        _, _, lines = self.run_one("portal-ui-ci-cd", fail="e2e-failure")
        frames = [x for x in msgs(lines) if x.startswith("    at ")]
        self.assertTrue(frames)
        self.assertTrue(all(ERROR.search(x) for x in frames))

    def test_non_main_branch_skips_prod(self):
        res, _, lines = self.run_one("portal-ui-ci-cd", overrides={"branch": "develop"})
        self.assertTrue(any('Stage "Deploy Prod" skipped due to when conditional' in x for x in msgs(lines)))
        self.assertEqual(res["status"], "SUCCESS")

    def test_prod_rollback_forces_main(self):
        res, _, lines = self.run_one("payment-api-ci-cd", fail="prod-rollback", overrides={"branch": "develop"})
        self.assertEqual(res["branch"], "main")
        self.assertTrue(any("Rollback was a success" in x for x in msgs(lines)))

    def test_same_commit_reproduces_same_failure(self):
        sha = "a" * 40
        _, _, l1 = self.run_one("portal-ui-ci-cd", fail="e2e-failure", overrides={"sha": sha})
        _, _, l2 = self.run_one("portal-ui-ci-cd", fail="e2e-failure", overrides={"sha": sha})
        pick = lambda ls: sorted(x for x in msgs(ls) if "\u2716" in x and "still" not in x)
        strip = lambda xs: [re.sub(r"\[chrome[^\]]*\] |\(\d+\.\ds\)", "", x).strip() for x in xs]
        self.assertEqual(sorted(strip(pick(l1))), sorted(strip(pick(l2))))

    def test_scenarios(self):
        expected = {
            "bad-commit": ["FAILURE", "FAILURE", "FAILURE", "SUCCESS"],
            "cve-blocked": ["FAILURE", "SUCCESS"],
            "prod-rollback": ["FAILURE", "SUCCESS"],
            "broken-build": ["FAILURE", "FAILURE", "SUCCESS"],
            "test-regression": ["FAILURE", "SUCCESS"],
            "flaky-qa-deploy": ["FAILURE", "SUCCESS"],
        }
        self.assertEqual(set(expected), set(g.SCENARIOS))
        for name, statuses in expected.items():
            with self.subTest(scenario=name):
                results = g.run_scenario(self.base, name, SPEED, 0)
                self.assertEqual([r["status"] for r in results], statuses)
                commits = []
                for r in results:
                    _, lines = read(self.base, r)
                    commits.append(next(CHECKOUT.match(x).group("sha") for x in msgs(lines) if CHECKOUT.match(x)))
                if name == "bad-commit":
                    self.assertEqual(len(set(commits[:3])), 1, "retries must reuse the same commit")
                    self.assertNotEqual(commits[0], commits[3])
                    started = [STARTED.match(x).group("trigger") for r in results for x in msgs(read(self.base, r)[1]) if STARTED.match(x)]
                    self.assertEqual(started, ["GitHub push by", "user", "user", "GitHub push by"])
        manifest = os.path.join(self.base, "_scenarios", "manifest.jsonl")
        self.assertTrue(os.path.exists(manifest))
        # the manifest must live outside jobs/ so a log agent never ingests it
        self.assertFalse(manifest.startswith(os.path.join(self.base, "jobs")))

    def test_commit_message_pattern(self):
        _, _, lines = self.run_one("portal-ui-ci-cd", overrides={"msg": "Fix: restore pay-now button id"})
        hits = [COMMIT_MSG.match(x).group("commit_msg") for x in msgs(lines) if COMMIT_MSG.match(x)]
        self.assertEqual(hits, ["Fix: restore pay-now button id"])

    def test_processor_statements_lex_cleanly(self):
        """Regression: a regex containing \\" closed the OTTL string early and Bindplane rejected the config.
        Lex each statement the way OTTL does: after the string literals are removed, no regex text may remain."""
        with open(PROCESSOR_YAML, encoding="utf-8") as f:
            stmts = [ln.strip()[3:-1] for ln in f if ln.strip().startswith("- '") and ln.strip().endswith("'")]
        self.assertGreater(len(stmts), 20)
        for stmt in stmts:
            outside, in_str, i = [], False, 0
            while i < len(stmt):
                c = stmt[i]
                if in_str and c == "\\":
                    i += 2
                    continue
                if c == '"':
                    in_str = not in_str
                elif not in_str:
                    outside.append(c)
                i += 1
            self.assertFalse(in_str, f"unterminated string literal in: {stmt}")
            rest = "".join(outside)
            self.assertNotIn("?P<", rest, f"regex text outside a string literal in: {stmt}")
            self.assertNotIn("(?", rest, f"regex text outside a string literal in: {stmt}")

    def test_build_numbers_are_unique(self):
        nums = {self.run_one("portal-ui-ci-cd")[0]["build"] for _ in range(4)}
        self.assertEqual(len(nums), 4)


if __name__ == "__main__":
    unittest.main()
