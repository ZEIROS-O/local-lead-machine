from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from import_llm_score_bundle import REQUIRED_BUNDLE_SCORE_KEYS
from lead_machine.manual_scores import validate_manual_score
from lead_machine.sendability import evaluate_commercial_fit
from score_batch_subscription import (
    DEFAULT_SCHEMA,
    ProcessResult,
    append_ledger,
    build_claude_command,
    build_codex_command,
    discover_batches,
    main,
    parse_codex_usage,
    preflight_subscription_auth,
    queue_lock,
    receipt_matches,
    resolve_runner_effort,
    sanitized_environment,
    validate_bundle_identity,
)


def valid_score() -> dict[str, object]:
    return {
        "score_schema_version": 2,
        "subscores": {
            "icp_fit": 15,
            "reputation_demand_proof": 10,
            "web_conversion_pain": 20,
            "mockup_leverage": 10,
            "reachability": 8,
            "offer_angle_clarity": 8,
            "evidence_confidence": 4,
        },
        "total_score": 75,
        "grade": "B",
        "confidence": "high",
        "hard_disqualifiers": [],
        "best_angle": "Website Cleanup",
        "recommended_action": "Verify the issue before outreach.",
        "why_this_lead": ["The evidence shows a concrete conversion issue."],
        "why_this_might_be_wrong": ["The configuration may be intentional."],
        "opener": "I noticed a specific issue on the public site.",
        "evidence_refs": ["ev-1"],
        "safe_outreach_claims": [{"claim": "A public issue is visible.", "evidence_refs": ["ev-1"]}],
        "unsafe_claims": ["The issue is definitely losing revenue."],
        "airtable_fields": {},
        "commercial_fit_state": "minor-fix/nurture",
        "commercial_materiality": "",
        "recurring_consequence": "",
        "root_cause_confidence": "",
        "offer_component": "",
        "packageable_solution": "",
        "value_mechanism": "",
        "beyond_free_fix": "",
        "self_fix_risk": "",
        "intentionality": "ambiguous",
        "contact_eligibility": "hold-commercial-case",
        "asset_state": "none",
        "promise_state": "asset-free",
        "asset_id": "",
        "asset_verifier_verdict": "",
    }


def bundle(batch_id: str, files: list[str]) -> dict[str, object]:
    return {
        "batch_id": batch_id,
        "scores": [{"score_file": filename, "score": valid_score()} for filename in files],
    }


def write_queue(root: Path, batches: dict[str, list[str]]) -> Path:
    queue = root / "review_queue_test"
    batch_dir = queue / "batches"
    batch_dir.mkdir(parents=True)
    for batch_id, files in batches.items():
        (batch_dir / f"{batch_id}.md").write_text(f"Score {batch_id}\n", encoding="utf-8")
        (batch_dir / f"{batch_id}_expected_files.txt").write_text("\n".join(files) + "\n", encoding="utf-8")
    return queue


class FakeRunner:
    def __init__(self, model_outputs: list[ProcessResult], *, claude: bool = False) -> None:
        self.model_outputs = list(model_outputs)
        self.claude = claude
        self.calls: list[tuple[list[str], str, Path, dict[str, str], int]] = []

    def __call__(self, cmd, *, input_text, cwd, env, timeout_seconds):
        command = list(cmd)
        self.calls.append((command, input_text, Path(cwd), dict(env), timeout_seconds))
        if command[1:3] == ["login", "status"]:
            return ProcessResult(0, "Logged in using ChatGPT\n", "", 0.01)
        if command[1:3] == ["auth", "status"]:
            payload = {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "apiProvider": "firstParty",
                "subscriptionType": "pro",
            }
            return ProcessResult(0, json.dumps(payload), "", 0.01)
        if not self.model_outputs:
            raise AssertionError("unexpected model invocation")
        result = self.model_outputs.pop(0)
        if command[0] == "codex" and "--output-last-message" in command and result.stdout.startswith("MODEL:"):
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(result.stdout.removeprefix("MODEL:"), encoding="utf-8")
            return ProcessResult(result.returncode, "", result.stderr, result.duration_seconds, result.timed_out)
        return result


class SubscriptionScoringTests(unittest.TestCase):
    def test_commercial_fit_state_accepts_case_insensitive_model_output(self) -> None:
        score = valid_score()
        score["commercial_fit_state"] = "not-an-lpm-fit"

        validated = validate_manual_score(score, allowed_evidence_ids={"ev-1"})

        self.assertEqual(validated["commercial_fit_state"], "not-an-LPM-fit")
        evaluated = evaluate_commercial_fit(
            {
                "Commercial Fit State": validated["commercial_fit_state"],
                "Contact Eligibility": "hold-commercial-case",
                "Asset State": "none",
                "Promise State": "asset-free",
            }
        )
        self.assertEqual(evaluated["commercial_fit_state"], "not-an-LPM-fit")
        self.assertEqual(evaluated["state_errors"], [])

    def test_evidence_refs_canonicalize_separator_only_model_drift(self) -> None:
        score = valid_score()
        score["evidence_refs"] = ["ev-cta-strength-16"]

        validated = validate_manual_score(
            score,
            allowed_evidence_ids={"ev-cta_strength-16"},
        )

        self.assertEqual(validated["evidence_refs"], ["ev-cta_strength-16"])

        score["evidence_refs"] = ["invented-ref"]
        with self.assertRaisesRegex(ValueError, "Unknown evidence_refs: invented-ref"):
            validate_manual_score(score, allowed_evidence_ids={"ev-cta_strength-16"})

    def test_schema_required_keys_match_current_importer_contract(self) -> None:
        schema = json.loads(DEFAULT_SCHEMA.read_text(encoding="utf-8"))
        score_schema = schema["properties"]["scores"]["items"]["properties"]["score"]
        self.assertEqual(set(score_schema["required"]), REQUIRED_BUNDLE_SCORE_KEYS)
        self.assertEqual(set(score_schema["properties"]), REQUIRED_BUNDLE_SCORE_KEYS)
        self.assertEqual(len(REQUIRED_BUNDLE_SCORE_KEYS), 30)

    def test_discover_batches_requires_safe_unique_expected_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = write_queue(Path(tmp), {"batch_001": ["one.json", "one.json"]})
            with self.assertRaisesRegex(ValueError, "duplicates"):
                discover_batches(queue, [], queue / "scores")
            (queue / "batches/batch_001_expected_files.txt").write_text("../bad.json\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsafe"):
                discover_batches(queue, [], queue / "scores")

    def test_bundle_identity_requires_matching_id_and_exact_file_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = write_queue(Path(tmp), {"batch_001": ["one.json", "two.json"]})
            job = discover_batches(queue, [], queue / "scores")[0]
            with self.assertRaisesRegex(ValueError, "batch_id"):
                validate_bundle_identity(bundle("batch_999", ["one.json", "two.json"]), job)
            with self.assertRaisesRegex(ValueError, "missing=two.json"):
                validate_bundle_identity(bundle("batch_001", ["one.json"]), job)
            with self.assertRaisesRegex(ValueError, "extra=three.json"):
                validate_bundle_identity(bundle("batch_001", ["one.json", "two.json", "three.json"]), job)
            validated = validate_bundle_identity(bundle("batch_001", ["two.json", "one.json"]), job)
            self.assertEqual(validated["batch_id"], "batch_001")

    def test_sanitized_environment_removes_paid_provider_overrides(self) -> None:
        source = {
            "PATH": "/bin",
            "OPENAI_API_KEY": "paid",
            "ANTHROPIC_API_KEY": "paid",
            "ANTHROPIC_AUTH_TOKEN": "paid",
            "CLAUDE_CODE_USE_BEDROCK": "1",
        }
        cleaned = sanitized_environment(source)
        self.assertEqual(cleaned, {"PATH": "/bin"})

    def test_subscription_auth_preflights_fail_closed(self) -> None:
        def codex_api_auth(cmd, **kwargs):
            return ProcessResult(0, "Logged in using API key\n", "", 0.01)

        def claude_api_auth(cmd, **kwargs):
            return ProcessResult(
                0,
                json.dumps({"loggedIn": True, "authMethod": "apiKey", "apiProvider": "firstParty"}),
                "",
                0.01,
            )

        self.assertFalse(preflight_subscription_auth("codex", env={}, process_runner=codex_api_auth).ok)
        self.assertFalse(preflight_subscription_auth("claude", env={}, process_runner=claude_api_auth).ok)

    def test_runner_commands_are_isolated_and_use_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            output = cwd / "last.txt"
            codex = build_codex_command(cwd=cwd, last_message_path=output, model="gpt-test", use_output_schema=True)
            self.assertIn("--ignore-user-config", codex)
            self.assertIn("--ignore-rules", codex)
            self.assertIn("features.shell_tool=false", codex)
            self.assertIn("features.apps=false", codex)
            self.assertIn('web_search="disabled"', codex)
            self.assertIn("--sandbox", codex)
            self.assertIn("read-only", codex)
            self.assertIn("--ephemeral", codex)
            self.assertEqual(codex[-1], "-")
            self.assertTrue(Path(codex[codex.index("--output-schema") + 1]).is_absolute())
            self.assertTrue(Path(codex[codex.index("--output-last-message") + 1]).is_absolute())

            claude = build_claude_command(model="claude-test")
            self.assertIn("--safe-mode", claude)
            self.assertEqual(claude[claude.index("--tools") + 1], "")
            self.assertIn("--no-session-persistence", claude)
            self.assertIn("--strict-mcp-config", claude)

            # No effort configured -> neither CLI gets an effort argument.
            self.assertNotIn("--effort", claude)
            self.assertNotIn('model_reasoning_effort="high"', codex)

    def test_reasoning_effort_reaches_each_runner_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            codex = build_codex_command(
                cwd=cwd,
                last_message_path=cwd / "last.txt",
                model="gpt-test",
                use_output_schema=True,
                reasoning_effort="high",
            )
            # codex exec has no --effort flag; effort rides in as a config key.
            self.assertIn('model_reasoning_effort="high"', codex)
            self.assertEqual(codex[-1], "-")

            claude = build_claude_command(model="claude-test", reasoning_effort="xhigh")
            self.assertEqual(claude[claude.index("--effort") + 1], "xhigh")

    def test_cli_usage_counts_cache_tokens_as_input(self) -> None:
        """Real Claude CLI envelope: 14,873 prompt tokens reported as input_tokens=2.

        Everything else rides in the cache counters. Counting only input_tokens
        under-reports a subscription run's input by ~99%, which silently unbinds
        MAX_LLM_TOKENS_PER_RUN.
        """
        from lead_machine.llm_client import _normalize_cli_usage

        claude_envelope = {
            "input_tokens": 2,
            "cache_creation_input_tokens": 11584,
            "cache_read_input_tokens": 3289,
            "output_tokens": 4,
        }
        usage = _normalize_cli_usage(claude_envelope)
        self.assertEqual(usage["input_tokens"], 2 + 11584 + 3289)
        self.assertEqual(usage["output_tokens"], 4)
        self.assertEqual(usage["total_tokens"], 2 + 11584 + 3289 + 4)

        # Codex-shaped envelope (no cache counters) is unchanged.
        codex_envelope = {"input_tokens": 900, "output_tokens": 100, "total_tokens": 1000}
        self.assertEqual(
            _normalize_cli_usage(codex_envelope),
            {"input_tokens": 900, "output_tokens": 100, "total_tokens": 1000},
        )

        # A reported total that undercounts the parts must not win.
        self.assertEqual(
            _normalize_cli_usage({"input_tokens": 5, "cache_read_input_tokens": 500, "output_tokens": 10, "total_tokens": 15})["total_tokens"],
            515,
        )
        self.assertEqual(_normalize_cli_usage(None), {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})

    def test_runner_effort_resolves_per_runner_and_drops_unknown_levels(self) -> None:
        env = {
            "CODEX_REASONING_EFFORT": "HIGH",
            "CLAUDE_SUBSCRIPTION_EFFORT": "max",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(resolve_runner_effort("codex"), "high")
            self.assertEqual(resolve_runner_effort("claude"), "max")

        # An unsupported level would make the CLI reject the whole invocation,
        # silently killing that tier of the provider chain — drop it instead.
        with mock.patch.dict(os.environ, {"CODEX_REASONING_EFFORT": "ludicrous"}, clear=False):
            self.assertEqual(resolve_runner_effort("codex"), "")
        # "minimal" is codex-only; the claude CLI has no such level.
        with mock.patch.dict(os.environ, {"CLAUDE_SUBSCRIPTION_EFFORT": "minimal"}, clear=False):
            self.assertEqual(resolve_runner_effort("claude"), "")

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_runner_effort("codex"), "")
            self.assertEqual(resolve_runner_effort("claude"), "")

    def test_codex_happy_path_writes_receipt_and_rerun_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = write_queue(root, {"batch_001": ["one.json", "two.json"]})
            ledger = root / "ledger.jsonl"
            model = ProcessResult(0, "MODEL:" + json.dumps(bundle("batch_001", ["one.json", "two.json"])), "", 0.1)
            first_runner = FakeRunner([model])
            rc = main(["--queue-dir", str(queue), "--ledger", str(ledger)], process_runner=first_runner)
            self.assertEqual(rc, 0)
            self.assertTrue((queue / "scores/one.json").is_file())
            self.assertTrue((queue / "scores/two.json").is_file())
            receipt_path = queue / "scores/.subscription_receipts/batch_001.json"
            self.assertTrue(receipt_path.is_file())
            job = discover_batches(queue, [], queue / "scores")[0]
            self.assertTrue(receipt_matches(job, queue / "scores")[0])
            model_call = first_runner.calls[1]
            self.assertEqual(model_call[1], "Score batch_001\n")
            self.assertNotIn("Score batch_001", " ".join(model_call[0]))
            self.assertNotEqual(model_call[2], queue)

            second_runner = FakeRunner([])
            rerun = main(["--queue-dir", str(queue), "--ledger", str(ledger)], process_runner=second_runner)
            self.assertEqual(rerun, 0)
            self.assertEqual(len(second_runner.calls), 1)
            records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(records[-1]["validation_result"], "skipped_receipt_match")
            self.assertEqual(records[-1]["cost_basis"], "subscription_auth")

    def test_changed_prompt_fails_stale_without_spending_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = write_queue(root, {"batch_001": ["one.json"]})
            ledger = root / "ledger.jsonl"
            first = FakeRunner([ProcessResult(0, "MODEL:" + json.dumps(bundle("batch_001", ["one.json"])), "", 0.1)])
            self.assertEqual(main(["--queue-dir", str(queue), "--ledger", str(ledger)], process_runner=first), 0)
            (queue / "batches/batch_001.md").write_text("Changed evidence\n", encoding="utf-8")
            stale = FakeRunner([])
            self.assertEqual(main(["--queue-dir", str(queue), "--ledger", str(ledger)], process_runner=stale), 1)
            self.assertEqual(len(stale.calls), 1)
            record = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["validation_result"], "stale_existing")
            self.assertIn("prompt hash changed", record["failure"])

    def test_force_replaces_stale_scores_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = write_queue(root, {"batch_001": ["one.json"]})
            ledger = root / "ledger.jsonl"
            first = FakeRunner([ProcessResult(0, "MODEL:" + json.dumps(bundle("batch_001", ["one.json"])), "", 0.1)])
            self.assertEqual(main(["--queue-dir", str(queue), "--ledger", str(ledger)], process_runner=first), 0)
            (queue / "batches/batch_001.md").write_text("Changed evidence\n", encoding="utf-8")
            forced = FakeRunner([ProcessResult(0, "MODEL:" + json.dumps(bundle("batch_001", ["one.json"])), "", 0.1)])
            rc = main(["--queue-dir", str(queue), "--ledger", str(ledger), "--force"], process_runner=forced)
            self.assertEqual(rc, 0)
            self.assertEqual(len(forced.calls), 2)
            job = discover_batches(queue, [], queue / "scores")[0]
            self.assertTrue(receipt_matches(job, queue / "scores")[0])

    def test_invalid_json_retries_once_and_imports_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = write_queue(root, {"batch_001": ["one.json"]})
            ledger = root / "ledger.jsonl"
            runner = FakeRunner([
                ProcessResult(0, "MODEL:not json", "", 0.1),
                ProcessResult(0, "MODEL:still not json", "", 0.1),
            ])
            rc = main(["--queue-dir", str(queue), "--ledger", str(ledger)], process_runner=runner)
            self.assertEqual(rc, 1)
            self.assertEqual(len(runner.calls), 3)
            self.assertFalse((queue / "scores/one.json").exists())
            record = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["validation_result"], "invalid_json")
            self.assertEqual(len(record["attempts"]), 2)

    def test_partial_bundle_retries_then_fails_without_score_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = write_queue(root, {"batch_001": ["one.json", "two.json"]})
            ledger = root / "ledger.jsonl"
            partial = ProcessResult(0, "MODEL:" + json.dumps(bundle("batch_001", ["one.json"])), "", 0.1)
            runner = FakeRunner([partial, partial])
            rc = main(["--queue-dir", str(queue), "--ledger", str(ledger)], process_runner=runner)
            self.assertEqual(rc, 1)
            self.assertFalse((queue / "scores/one.json").exists())
            record = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["validation_result"], "validation_failed")

    def test_zero_exit_quota_event_hard_stops_remaining_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = write_queue(root, {"batch_001": ["one.json"], "batch_002": ["two.json"]})
            ledger = root / "ledger.jsonl"
            quota_event = json.dumps({"type": "error", "message": "usage limit reached"})
            runner = FakeRunner([ProcessResult(0, quota_event, "", 0.1)])
            rc = main(["--queue-dir", str(queue), "--ledger", str(ledger)], process_runner=runner)
            self.assertEqual(rc, 2)
            self.assertEqual(len(runner.calls), 2)
            records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["validation_result"], "quota_exhausted")

    def test_valid_bundle_mentioning_rate_limits_is_not_quota_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = write_queue(root, {"batch_001": ["one.json"]})
            ledger = root / "ledger.jsonl"
            scored_bundle = bundle("batch_001", ["one.json"])
            scored_bundle["scores"][0]["score"]["why_this_might_be_wrong"] = [
                "The website crawl returned rate limits, so the evidence is incomplete."
            ]
            runner = FakeRunner(
                [ProcessResult(0, "MODEL:" + json.dumps(scored_bundle), "", 0.1)]
            )

            rc = main(["--queue-dir", str(queue), "--ledger", str(ledger)], process_runner=runner)

            self.assertEqual(rc, 0)
            self.assertTrue((queue / "scores/one.json").is_file())
            record = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["validation_result"], "imported")

    def test_claude_happy_path_parses_json_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = write_queue(root, {"batch_001": ["one.json"]})
            ledger = root / "ledger.jsonl"
            envelope = {
                "is_error": False,
                "result": json.dumps(bundle("batch_001", ["one.json"])),
                "usage": {"input_tokens": 10, "output_tokens": 20},
            }
            runner = FakeRunner([ProcessResult(0, json.dumps(envelope), "", 0.1)], claude=True)
            rc = main(
                ["--queue-dir", str(queue), "--ledger", str(ledger), "--runner", "claude"],
                process_runner=runner,
            )
            self.assertEqual(rc, 0)
            self.assertTrue((queue / "scores/one.json").is_file())
            self.assertEqual(runner.calls[1][0][0], "claude")

    def test_claude_zero_exit_quota_envelope_is_a_hard_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = write_queue(root, {"batch_001": ["one.json"], "batch_002": ["two.json"]})
            ledger = root / "ledger.jsonl"
            envelope = {"is_error": True, "result": "rate limit reached"}
            runner = FakeRunner([ProcessResult(0, json.dumps(envelope), "", 0.1)], claude=True)
            rc = main(
                ["--queue-dir", str(queue), "--ledger", str(ledger), "--runner", "claude"],
                process_runner=runner,
            )
            self.assertEqual(rc, 2)
            self.assertEqual(len(runner.calls), 2)
            record = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["validation_result"], "quota_exhausted")

    def test_parse_codex_usage_is_best_effort(self) -> None:
        stdout = "\n".join([
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 4}}),
        ])
        self.assertEqual(parse_codex_usage(stdout), {"input_tokens": 12, "output_tokens": 4})
        self.assertIsNone(parse_codex_usage("garbage"))

    def test_ledger_append_produces_complete_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            append_ledger(path, {"event": "one"})
            append_ledger(path, {"event": "two"})
            self.assertEqual(
                [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines()],
                ["one", "two"],
            )

    def test_queue_lock_rejects_concurrent_holder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp)
            with queue_lock(queue):
                with self.assertRaisesRegex(RuntimeError, "another subscription scoring run"):
                    with queue_lock(queue):
                        pass


if __name__ == "__main__":
    unittest.main()
