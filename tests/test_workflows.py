from __future__ import annotations

import unittest
from pathlib import Path
from shlex import split
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

SECRET_ENV = {
    "AIRTABLE_PAT": "${{ secrets.AIRTABLE_PAT }}",
    "AIRTABLE_BASE_ID": "${{ secrets.AIRTABLE_BASE_ID }}",
}
CORPUS_ENV = {"LPM_SCORED_OUTPUT_DIR": "${{ vars.LPM_SCORED_OUTPUT_DIR }}"}

# The gates that must run daily, in order, each with the env it is allowed to
# see. Pinned as one list so adding a gate to the workflow without declaring it
# here — or declaring one and never wiring it — fails rather than passing
# quietly, which is the failure mode the whole schedule exists to end.
EXPECTED_GATES = (
    (".venv/bin/python airtable_required_choices.py check", SECRET_ENV),
    (".venv/bin/python upload_airtable.py --schema-report", SECRET_ENV),
    (
        '.venv/bin/python evals/completeness.py --output-dir "$LPM_SCORED_OUTPUT_DIR"',
        {**SECRET_ENV, **CORPUS_ENV},
    ),
    (
        '.venv/bin/python evals/reproducibility.py --output-dir "$LPM_SCORED_OUTPUT_DIR"',
        {**SECRET_ENV, **CORPUS_ENV},
    ),
    # Corpus only: a gate that never reads the base is never handed the PAT.
    (
        '.venv/bin/python evals/gate_liveness.py --output-dir "$LPM_SCORED_OUTPUT_DIR"',
        CORPUS_ENV,
    ),
)


class WorkflowContractTests(unittest.TestCase):
    def load_workflow(self, name: str) -> tuple[dict[str, Any], str]:
        path = WORKFLOWS_DIR / name
        self.assertTrue(path.is_file(), f"missing workflow: {path.relative_to(REPO_ROOT)}")
        source = path.read_text(encoding="utf-8")
        workflow = yaml.load(source, Loader=yaml.BaseLoader)
        self.assertIsInstance(workflow, dict)
        return workflow, source

    @staticmethod
    def only_job(workflow: dict[str, Any]) -> dict[str, Any]:
        jobs = workflow.get("jobs", {})
        if len(jobs) != 1:
            raise AssertionError(f"expected one job, found {list(jobs)}")
        return next(iter(jobs.values()))

    def assert_daily_schedule(self, workflow: dict[str, Any]) -> None:
        schedules = workflow["on"]["schedule"]
        self.assertEqual(len(schedules), 1)
        self.assertEqual(set(schedules[0]), {"cron"})
        fields = schedules[0]["cron"].split()
        self.assertEqual(len(fields), 5)
        minute, hour, day_of_month, month, day_of_week = fields
        self.assertTrue(minute.isdigit())
        self.assertTrue(hour.isdigit())
        self.assertIn(int(minute), range(60))
        self.assertIn(int(hour), range(24))
        self.assertEqual([day_of_month, month, day_of_week], ["*", "*", "*"])

    def test_tests_workflow_has_push_pull_request_and_daily_triggers(self) -> None:
        workflow, _ = self.load_workflow("tests.yml")

        triggers = workflow["on"]
        self.assertIn("push", triggers)
        self.assertIn("pull_request", triggers)
        self.assert_daily_schedule(workflow)

    def test_tests_workflow_runs_the_suite_without_application_secrets(self) -> None:
        workflow, source = self.load_workflow("tests.yml")
        job = self.only_job(workflow)
        steps = job["steps"]

        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(steps[0]["uses"], "actions/checkout@v6")
        self.assertEqual(steps[1]["uses"], "actions/setup-python@v6")
        self.assertEqual(steps[1]["with"]["python-version"], "3.12")
        test_commands = [
            line.strip()
            for step in steps
            for line in step.get("run", "").splitlines()
            if "unittest" in line
        ]
        self.assertEqual(
            test_commands,
            [".venv/bin/python -m unittest discover -s tests"],
        )
        install_step = next(step for step in steps if step["name"] == "Install test dependencies")
        install_tokens = set(split(install_step["run"]))
        self.assertTrue(
            {
                "requirements-dashboard.txt",
                "beautifulsoup4",
                "playwright",
                "python-dotenv",
                "PyYAML",
                "tldextract",
            }.issubset(install_tokens)
        )
        lowered = source.lower()
        self.assertNotIn("secrets.", lowered)
        self.assertNotIn("airtable_pat", lowered)
        self.assertNotIn("airtable_base_id", lowered)

    def test_gates_workflow_is_daily_and_manually_dispatchable(self) -> None:
        workflow, _ = self.load_workflow("gates.yml")
        job = self.only_job(workflow)
        steps = job["steps"]

        self.assertIn("schedule", workflow["on"])
        self.assert_daily_schedule(workflow)
        self.assertIn("workflow_dispatch", workflow["on"])
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(job["runs-on"], ["self-hosted", "linux", "lpm"])
        self.assertEqual(steps[0]["uses"], "actions/checkout@v6")
        self.assertEqual(steps[1]["uses"], "actions/setup-python@v6")
        self.assertEqual(steps[1]["with"]["python-version"], "3.12")

    def test_gates_workflow_preflights_self_hosted_corpus_before_eval(self) -> None:
        workflow, _ = self.load_workflow("gates.yml")
        steps = self.only_job(workflow)["steps"]
        corpus_env = {
            "LPM_SCORED_OUTPUT_DIR": "${{ vars.LPM_SCORED_OUTPUT_DIR }}",
        }
        preflight_indexes = [
            index
            for index, step in enumerate(steps)
            if step["name"] == "Preflight scored corpus"
        ]
        self.assertEqual(len(preflight_indexes), 1)
        preflight_index = preflight_indexes[0]
        eval_index = next(
            index
            for index, step in enumerate(steps)
            if step["name"] == "Check scored corpus completeness"
        )
        preflight = steps[preflight_index]

        self.assertLess(preflight_index, eval_index)
        self.assertEqual(preflight["env"], corpus_env)
        self.assertIn('if [ -z "${LPM_SCORED_OUTPUT_DIR:-}" ]; then', preflight["run"])
        self.assertIn('if [ ! -d "$LPM_SCORED_OUTPUT_DIR" ]; then', preflight["run"])
        self.assertNotIn("continue-on-error", preflight)

    def test_gates_workflow_wires_secrets_and_runs_gates_in_order(self) -> None:
        workflow, source = self.load_workflow("gates.yml")
        job = self.only_job(workflow)

        self.assertNotIn("env", job)
        gate_steps = [
            step
            for step in job["steps"]
            if ".venv/bin/python " in step.get("run", "")
        ]
        self.assertEqual(len(gate_steps), len(EXPECTED_GATES))
        for step, (_command, expected_env) in zip(gate_steps, EXPECTED_GATES):
            self.assertEqual(step.get("env"), expected_env)
        for step in job["steps"]:
            if step not in gate_steps and step["name"] != "Preflight scored corpus":
                self.assertNotIn("env", step)
        commands = [
            line.strip()
            for step in job["steps"]
            for line in step.get("run", "").splitlines()
            if line.strip().startswith(".venv/bin/python ")
        ]
        self.assertEqual(commands, [command for command, _env in EXPECTED_GATES])
        self.assertNotRegex(source, r"\.venv/bin/python[^\n]*secrets\.")

    def test_gate_summaries_preserve_command_failures(self) -> None:
        workflow, _ = self.load_workflow("gates.yml")
        gate_steps = [
            step
            for step in self.only_job(workflow)["steps"]
            if ".venv/bin/python " in step.get("run", "")
        ]

        self.assertEqual(len(gate_steps), len(EXPECTED_GATES))
        for step in gate_steps:
            run = step["run"]
            self.assertIn("trap report_failure ERR", run)
            self.assertIn('exit "$status"', run)
            self.assertIn('"$GITHUB_STEP_SUMMARY"', run)
            self.assertIn("PASS:", run)
            self.assertIn("FAIL:", run)
            self.assertNotIn("continue-on-error", step)


if __name__ == "__main__":
    unittest.main()
