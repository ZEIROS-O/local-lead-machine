"""Suppression must bite where leads actually flow.

The original version of these tests proved the enricher *set* a flag. That is
not the property that matters: a flag nothing reads is not a control. These
assert that a suppressed lead is stopped — absent from the LLM batch, absent
from the scored output, and No Packet if it is scored anyway — on both scoring
paths and through the entry point the pipeline really runs.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import enrich_leads
import make_search_batch
import score_leads
from lead_machine.compliance import (
    ComplianceError,
    has_suppression_match,
    is_suppressed,
    is_suppressed_record,
    load_configured_suppression_index,
    merge_suppression_disqualifiers,
    record_matches_suppression,
    record_suppression_identifiers,
    suppression_disqualifiers,
)
from lead_machine.scoring_v3 import _hard_disqualifiers, score_v3
from lead_machine.sendability import evaluate_sendability
from make_llm_batch import candidate_records


SUPPRESSED_PHONE = "(216) 555-0100"
SUPPRESSED_INDEX = {"phone:2165550100", "email:bad@test.com"}


def _enriched(*, suppressed: bool = False, **overrides):
    record = {
        "google": {
            "business_name": "Suppressed Plumbing",
            "phone": SUPPRESSED_PHONE,
            "rating": "4.9",
            "reviews": "60",
            "category": "plumber",
            "source_query": "plumber Cleveland OH",
            "place_id": "ChIJtest",
        },
        "website": {"site_status": "No Website", "site_issue_flags": [], "pages": {}},
        "screenshots": {},
        "derived": {
            "hard_icp_skip": suppressed,
            "hard_icp_reasons": ["dnc_suppressed"] if suppressed else [],
        },
    }
    record.update(overrides)
    return record


class SuppressionIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_matches_are_normalized_across_formatting(self) -> None:
        for phone in ("(216) 555-0100", "1-216-555-0100", "2165550100"):
            self.assertTrue(
                has_suppression_match(phones=[phone], suppression_index=SUPPRESSED_INDEX),
                phone,
            )
        self.assertTrue(
            has_suppression_match(emails=["BAD@TEST.COM "], suppression_index=SUPPRESSED_INDEX)
        )

    def test_blank_and_unmatched_identifiers_do_not_match(self) -> None:
        self.assertFalse(
            has_suppression_match(
                emails=[None, "", "  ", "good@test.com"],
                phones=[None, "", "  ", "999-999-9999"],
                suppression_index=SUPPRESSED_INDEX,
            )
        )

    def test_missing_source_fails_closed_unless_explicitly_bypassed(self) -> None:
        with self.assertRaises(ComplianceError) as caught:
            load_configured_suppression_index()
        self.assertIn("No suppression source configured", str(caught.exception))

        os.environ["ALLOW_MISSING_SUPPRESSION_INDEX"] = "1"
        self.assertEqual(load_configured_suppression_index(), set())

    def test_unreadable_source_fails_closed_rather_than_returning_empty(self) -> None:
        """An empty index silently disables every opt-out, so it must never be
        the fallback for a source that was configured but could not load."""
        os.environ["SUPPRESSION_SOURCE"] = "does_not_exist.jsonl"
        with self.assertRaises(ComplianceError):
            load_configured_suppression_index()

    def test_suppression_source_takes_precedence_over_outcomes_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            primary = Path(tmp) / "primary.jsonl"
            secondary = Path(tmp) / "secondary.jsonl"
            primary.write_text('{"event": "opt_out", "phone": "1111111111"}\n', encoding="utf-8")
            secondary.write_text('{"event": "opt_out", "phone": "2222222222"}\n', encoding="utf-8")
            os.environ["SUPPRESSION_SOURCE"] = str(primary)
            os.environ["LPM_OUTCOMES_JSONL"] = str(secondary)

            index = load_configured_suppression_index()

        self.assertIn("phone:1111111111", index)
        self.assertNotIn("phone:2222222222", index)

    def test_only_suppression_events_enter_the_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                '{"event": "opt_out", "phone": "1111111111"}\n'
                '{"event": "replied", "phone": "2222222222"}\n',
                encoding="utf-8",
            )
            os.environ["SUPPRESSION_SOURCE"] = str(path)

            index = load_configured_suppression_index()

        self.assertIn("phone:1111111111", index)
        self.assertNotIn("phone:2222222222", index)

    def test_a_ledger_written_opt_out_resolves_instead_of_failing_the_run(self) -> None:
        """operator-agent's STATE/outcomes.jsonl — which this file *is*, in
        production — spells the Airtable record id `lead_id`, not `record_id`
        (STATE/README.md). A row keyed that way used to raise "suppression event
        without an identifier", and because compliance fails closed that single
        opt_out would take search, enrichment and scoring down with it."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outcomes.jsonl"
            path.write_text(
                '{"ts": "2026-07-27T00:00:00Z", "business": "lpm", '
                '"lead_id": "recABC", "event": "opt_out", "phone": "(216) 555-0100"}\n',
                encoding="utf-8",
            )
            os.environ["SUPPRESSION_SOURCE"] = str(path)

            index = load_configured_suppression_index()

        self.assertIn("record_id:recabc", index)
        self.assertIn("phone:2165550100", index)
        self.assertTrue(is_suppressed({"record_id": "recABC"}, index))


class SuppressionIdentityTests(unittest.TestCase):
    """One identifier set, so send-time and score-time recognise the same lead.

    Read these with the reachability split in mind: the record-id half of the
    set only ever populates for a record that already exists in Airtable, while
    the nested google/website half is what actually fires on the discovery path.
    `test_a_pipeline_enriched_record_has_no_record_id_to_match_on` pins that
    split, and every test here that supplies an id says where the id came from.
    """

    # Google's own documented example. Place IDs and Airtable record ids are
    # different namespaces that collide on the key name `id`.
    PLACE_ID = "ChIJN1t_tDeuEmsRUsoyG83frY4"

    def test_a_places_payload_id_never_becomes_a_record_id_key(self) -> None:
        """A Place ID in the record_id key space is wrong in both directions: it
        can match a ledger row that meant a different record, and it shadows the
        Airtable id the record should have been recognised by.

        Both inputs are constructed, not captured: today enrichment matches
        Places leads through `has_suppression_match` on contacts alone, so no
        caller hands this function a bare `id`. That is exactly why the
        narrowing needs a test — it is cheap to reintroduce and silent when
        wrong.
        """
        payload = {
            "id": self.PLACE_ID,
            "displayName": {"text": "Patio Pros"},
            "nationalPhoneNumber": SUPPRESSED_PHONE,
        }
        enriched = _enriched(id=self.PLACE_ID)
        place_id_key = f"record_id:{self.PLACE_ID.casefold()}"

        for record in (payload, enriched):
            identifiers = record_suppression_identifiers(record)
            self.assertEqual(
                [key for key in identifiers if key.startswith("record_id:")], [], record
            )
            self.assertFalse(record_matches_suppression(record, {place_id_key}), record)

    def test_an_airtable_envelope_id_is_still_a_record_id(self) -> None:
        """mark_sent identifies the record it is about to send to by the `{"id",
        "fields"}` envelope Airtable returns; narrowing `id` must not disarm it."""
        envelope = {"id": "rec42", "fields": {"Status": "New"}}

        self.assertIn("record_id:rec42", record_suppression_identifiers(envelope))
        self.assertTrue(is_suppressed(envelope, {"record_id:rec42"}))

    def test_an_airtable_sourced_record_is_matched_on_lead_id_and_nested_contacts(self) -> None:
        """A record that arrives already carrying an Airtable id — a re-scored
        export, not something enrich_leads produced — is matched on that id as
        well as on its nested contacts. The lead_id here is supplied by the
        test because only an Airtable-sourced record ever has one."""
        record = _enriched(lead_id="recLedger01")
        record["website"] = {**record["website"], "emails_found": ["bad@test.com"]}

        identifiers = record_suppression_identifiers(record)

        self.assertIn("record_id:recledger01", identifiers)
        self.assertIn("email:bad@test.com", identifiers)
        self.assertIn("phone:2165550100", identifiers)

    def test_a_pipeline_enriched_record_has_no_record_id_to_match_on(self) -> None:
        """What score-time matching can actually reach today, built by the real
        constructor rather than by hand.

        `enrich_row` writes google/website/screenshots/derived, where `google`
        is the make_search_batch row — place_id, never an Airtable id. A newly
        discovered lead has not been uploaded yet, so it genuinely has no record
        id, while the ledger's lead_id always names a record already in the
        base. Record-id matching is therefore only reachable for
        Airtable-sourced records (mark_sent envelopes, hermes queue rows,
        re-scored exports); on the discovery path the contact keys are the whole
        gate. If ids ever start appearing here, the lead_id tests above stop
        being hypothetical — and if the contact keys stop appearing, score-time
        suppression has quietly gone dark.
        """
        row = {"business_name": "Test", "phone": SUPPRESSED_PHONE, "website_url": "https://x.example"}

        with patch("enrich_leads.scrape_site") as scrape, patch("enrich_leads.build_buyer_intent"):
            scrape.return_value = {"emails_found": ["bad@test.com"], "phones_found": []}
            record = enrich_leads.enrich_row(row, Path("fake"), take_shots=False)

        for block in (record, record["google"], record["website"]):
            self.assertEqual(
                sorted(key for key in block if key in {"id", "record_id", "lead_id"}),
                [],
                sorted(block),
            )

        identifiers = record_suppression_identifiers(record)

        self.assertEqual([key for key in identifiers if key.startswith("record_id:")], [])
        self.assertIn("phone:2165550100", identifiers)
        self.assertIn("email:bad@test.com", identifiers)
        self.assertTrue(record_matches_suppression(record, SUPPRESSED_INDEX))

    def test_a_record_we_cannot_identify_is_refused_not_cleared(self) -> None:
        """Fail closed. An empty identifier set is read downstream as "clear" —
        hermes's morning queue keeps `not _is_suppressed(record, ...)` — so a
        malformed record that returned no identifiers would be queued for send
        instead of stopping the run."""
        for malformed in ("recABC", ["recABC"], 42, None):
            with self.assertRaises(ComplianceError, msg=malformed):
                record_suppression_identifiers(malformed)
            with self.assertRaises(ComplianceError, msg=malformed):
                is_suppressed(malformed, {"record_id:recabc"})
            with self.assertRaises(ComplianceError, msg=malformed):
                record_matches_suppression(malformed, {"record_id:recabc"})


class EnrichRowSuppressionTests(unittest.TestCase):
    def test_a_google_phone_match_skips_the_crawl_entirely(self) -> None:
        """The crawl costs page fetches and a browser launch. Google already
        gave us the phone, so a suppressed lead must cost neither."""
        row = {"business_name": "Test", "phone": SUPPRESSED_PHONE, "website_url": "https://x.example"}

        with patch("enrich_leads.scrape_site") as scrape, patch("enrich_leads.build_buyer_intent"):
            result = enrich_leads.enrich_row(
                row, Path("fake"), take_shots=False, suppression_index=SUPPRESSED_INDEX
            )

        scrape.assert_not_called()
        self.assertTrue(result["derived"]["hard_icp_skip"])
        self.assertIn("dnc_suppressed", result["derived"]["hard_icp_reasons"])

    def test_site_status_stays_in_the_existing_vocabulary(self) -> None:
        """Site Status is a typed Airtable select; an invented value would
        normalize to None and be dropped without anyone noticing."""
        row = {"business_name": "Test", "phone": SUPPRESSED_PHONE, "website_url": "https://x.example"}

        with patch("enrich_leads.scrape_site"), patch("enrich_leads.build_buyer_intent"):
            result = enrich_leads.enrich_row(
                row, Path("fake"), take_shots=False, suppression_index=SUPPRESSED_INDEX
            )

        self.assertEqual(result["website"]["site_status"], "Unknown")

    def test_an_email_found_only_by_the_crawl_still_suppresses(self) -> None:
        row = {"business_name": "Test", "phone": "(216) 555-9999", "website_url": "https://x.example"}

        with patch("enrich_leads.scrape_site") as scrape, patch("enrich_leads.build_buyer_intent"):
            scrape.return_value = {"emails_found": ["bad@test.com"], "phones_found": []}
            result = enrich_leads.enrich_row(
                row, Path("fake"), take_shots=False, suppression_index=SUPPRESSED_INDEX
            )

        scrape.assert_called_once()
        self.assertIn("dnc_suppressed", result["derived"]["hard_icp_reasons"])

    def test_unsuppressed_leads_keep_their_existing_gate_reasons(self) -> None:
        row = {"business_name": "Test", "phone": "(216) 555-9999", "rating": "3.0", "category": "plumber"}

        with patch("enrich_leads.scrape_site") as scrape, patch("enrich_leads.build_buyer_intent"):
            scrape.return_value = {"emails_found": [], "phones_found": []}
            result = enrich_leads.enrich_row(
                row, Path("fake"), take_shots=False, suppression_index=SUPPRESSED_INDEX
            )

        reasons = result["derived"]["hard_icp_reasons"]
        self.assertNotIn("dnc_suppressed", reasons)
        self.assertIn("weak_reputation", reasons)

    def test_the_reason_is_appended_once_and_after_existing_reasons(self) -> None:
        row = {"business_name": "Test", "phone": SUPPRESSED_PHONE, "rating": "3.0", "category": "plumber"}

        with patch("enrich_leads.scrape_site"), patch("enrich_leads.build_buyer_intent"):
            result = enrich_leads.enrich_row(
                row, Path("fake"), take_shots=False, suppression_index=SUPPRESSED_INDEX
            )

        reasons = result["derived"]["hard_icp_reasons"]
        self.assertEqual(reasons.count("dnc_suppressed"), 1)
        self.assertLess(reasons.index("weak_reputation"), reasons.index("dnc_suppressed"))


class SuppressionStopsTheLeadTests(unittest.TestCase):
    """The property that matters: a suppressed lead does not ship."""

    def test_deterministic_scoring_yields_no_packet_and_a_visible_reason(self) -> None:
        row = score_leads.score_record(_enriched(suppressed=True), "plumbing")
        result = evaluate_sendability(row)

        self.assertEqual(row["Packet Tier"], "No Packet")
        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Hard disqualifier present", result["send_blockers"])
        self.assertFalse(result["minimum_evidence_passed"])
        # Skip Reason is a synced Airtable column, so the operator sees why.
        self.assertIn("dnc_suppressed", row["Skip Reason"])

    def test_an_unsuppressed_lead_carries_no_disqualifier(self) -> None:
        row = score_leads.score_record(_enriched(suppressed=False), "plumbing")

        self.assertNotIn("dnc_suppressed", row.get("Skip Reason", ""))
        self.assertNotIn(
            "Hard disqualifier present", evaluate_sendability(row)["send_blockers"]
        )

    def test_the_llm_path_applies_the_same_disqualifier(self) -> None:
        """Grades come from the model on this path, so the gate is the only
        thing that can stop a suppressed lead."""
        row = merge_suppression_disqualifiers({"Lead Priority": "A"}, _enriched(suppressed=True))

        self.assertIn("dnc_suppressed", row["hard_disqualifiers"])
        self.assertIn("Hard disqualifier present", evaluate_sendability(row)["send_blockers"])

    def test_suppressed_records_never_reach_an_llm_batch(self) -> None:
        good = _enriched(suppressed=False)
        bad = _enriched(suppressed=True)

        candidates = candidate_records([good, bad], fallback_vertical="plumbing")

        self.assertEqual(len(candidates), 1)
        self.assertIs(candidates[0], good)

    def test_a_reason_without_the_skip_flag_still_excludes(self) -> None:
        """Records written before the enricher set hard_icp_skip still carry the
        reason; the reason is what suppression means."""
        record = _enriched()
        record["derived"] = {"hard_icp_reasons": ["dnc_suppressed"]}

        self.assertTrue(is_suppressed_record(record))
        self.assertEqual(candidate_records([record], fallback_vertical="plumbing"), [])

    def test_malformed_derived_blocks_are_tolerated_not_fatal(self) -> None:
        """A scorer that crashes on one odd record fails the whole batch open."""
        for derived in ("string", None, 42, []):
            self.assertEqual(suppression_disqualifiers({"derived": derived}), [])
        self.assertEqual(suppression_disqualifiers(None), [])

    def test_malformed_hard_icp_skip_values_are_still_rejected_loudly(self) -> None:
        for value in ("string", [1, 2], 0, 1, "false"):
            record = {"google": {"business_name": "ok"}, "derived": {"hard_icp_skip": value}}
            with self.assertRaises(ValueError):
                candidate_records([record], fallback_vertical="plumbing")


class ScoringV3SuppressionTests(unittest.TestCase):
    def test_disqualifier_is_reported_with_a_profile(self) -> None:
        profile = MagicMock()
        profile.exclusions = []

        reasons = _hard_disqualifiers(_enriched(suppressed=True), {}, profile)

        self.assertIn("dnc_suppressed", reasons)

    def test_disqualifier_survives_a_missing_scoring_profile(self) -> None:
        """The fallback branch used to report no disqualifiers at all, so a
        suppressed lead in a vertical with no profile came back clean."""
        result = score_v3(_enriched(suppressed=True), {}, fallback_vertical="not-a-real-vertical")

        self.assertIn("dnc_suppressed", result.hard_disqualifiers)


class EntryPointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        # Clearing os.environ is not enough: every entry point calls
        # load_dotenv() inside main(), which reads the developer's real .env
        # back in and re-supplies SUPPRESSION_SOURCE. The fail-closed branch
        # then never runs and these tests pass locally only when .env happens
        # to be absent. Stub it the same way test_send_queue_scripts.py does.
        for module in (make_search_batch, enrich_leads, score_leads):
            patcher = patch.object(module, "load_dotenv", lambda: False)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_make_search_batch_passes_the_index_to_enrichment(self) -> None:
        """The regression this whole batch exists for: the production entry
        point called enrich_row without a suppression index, so the DNC gate
        never ran on the path that actually produces leads."""
        received = []

        def fake_enrich(
            row,
            screenshots_dir,
            take_shots,
            rendered_site_check=False,
            suppression_index=None,
            review_snapshot_store=None,
            per_lead_timeout=None,
        ):
            received.append(suppression_index)
            return {"google": row, "website": {"site_status": "Loaded"}, "derived": {}, "screenshots": {}}

        details = {
            "id": "place-1",
            "displayName": {"text": "Patio Pros"},
            "formattedAddress": "123 Main St, Cleveland, OH 44101, USA",
            "nationalPhoneNumber": SUPPRESSED_PHONE,
            "websiteUri": "https://patiopros.example",
            "googleMapsUri": "https://maps.google.com/?cid=1",
            "rating": 4.8,
            "userRatingCount": 28,
            "primaryType": "landscaper",
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "search_configs/test_hardscaping_001.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                Path("search_configs/test_hardscaping_001.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            with patch("make_search_batch.text_search_paged", return_value=([{"id": "place-1"}], 1)), patch(
                "make_search_batch.place_details", return_value=details
            ), patch("make_search_batch.enrich_row", side_effect=fake_enrich), contextlib.redirect_stdout(
                io.StringIO()
            ):
                make_search_batch.run_search_batch(
                    config_path=config_path,
                    batch="test_hardscaping_001",
                    root=root,
                    api_key="test-key",
                    suppression_index=SUPPRESSED_INDEX,
                )

        self.assertEqual(received, [SUPPRESSED_INDEX])

    def test_make_search_batch_main_fails_closed_before_paying_for_search(self) -> None:
        argv = ["make_search_batch.py", "--config", "search_configs/test_hardscaping_001.json"]
        with patch("sys.argv", argv), patch("make_search_batch.run_search_batch") as run:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                code = make_search_batch.main()

        self.assertEqual(code, 1)
        run.assert_not_called()
        self.assertIn("No suppression source configured", stderr.getvalue())

    def test_enrich_leads_help_does_not_require_a_suppression_source(self) -> None:
        with patch("sys.argv", ["enrich_leads.py", "--help"]):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit) as caught:
                    enrich_leads.main()

        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--input", out.getvalue())

    def test_enrich_leads_reports_a_missing_source_as_an_error_not_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "in.csv"
            source.write_text("business_name,phone,website_url\nTest,5555550100,\n", encoding="utf-8")
            argv = ["enrich_leads.py", "--input", str(source), "--output", str(Path(tmp) / "out.jsonl")]
            with patch("sys.argv", argv):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                    code = enrich_leads.main()

        self.assertEqual(code, 1)
        self.assertIn("[ERROR]", stderr.getvalue())
        self.assertIn("No suppression source configured", stderr.getvalue())

    def test_score_leads_rechecks_the_live_index_not_just_the_stored_flag(self) -> None:
        """The case the stored flag structurally cannot cover.

        Opt-outs arrive after enrichment almost by definition, so a record
        enriched before the business opted out carries a clean flag forever.
        Re-scoring that JSONL must still drop it.
        """
        stale = _enriched(suppressed=False)  # enriched before the opt-out existed
        self.assertFalse(is_suppressed_record(stale))

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "enriched.jsonl"
            output = Path(tmp) / "scored.csv"
            suppression = Path(tmp) / "suppress.jsonl"
            source.write_text(json.dumps(stale) + "\n", encoding="utf-8")
            suppression.write_text(
                json.dumps({"event": "opt_out", "phone": SUPPRESSED_PHONE}) + "\n", encoding="utf-8"
            )
            os.environ["SUPPRESSION_SOURCE"] = str(suppression)

            argv = ["score_leads.py", "--input", str(source), "--output", str(output), "--vertical", "plumbing"]
            with patch("sys.argv", argv):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = score_leads.main()

            rows = output.read_text(encoding="utf-8").strip().splitlines()

        self.assertEqual(code, 0)
        self.assertIn("[SKIP] Suppressed leads dropped before scoring: 1", stdout.getvalue())
        self.assertLessEqual(len(rows), 1)  # header only

    def test_score_leads_drops_an_airtable_sourced_record_by_its_lead_id(self) -> None:
        """Covers an input that arrives already carrying an Airtable id — NOT
        the pipeline's own output.

        enrich_leads writes google/website/screenshots/derived and no id
        anywhere (see
        SuppressionIdentityTests.test_a_pipeline_enriched_record_has_no_record_id_to_match_on),
        so a freshly discovered lead has nothing for this branch to match and is
        stopped on contacts instead. What this pins is the entry point for the
        inputs that *do* carry an id: a re-scored Airtable export, where
        operator-agent's ledger can log an opt_out with a lead_id and no phone
        or email. Before scoring shared mark_sent's identifier set, such a row
        bit at send time and nowhere earlier.

        The lead_id below is therefore supplied by the test, on purpose.
        """
        record = _enriched(suppressed=False, lead_id="recLedger01")
        # A clean phone, so the lead_id is the only thing that can match.
        record["google"] = {**record["google"], "phone": "(216) 555-9999"}
        self.assertFalse(is_suppressed_record(record))

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "enriched.jsonl"
            output = Path(tmp) / "scored.csv"
            suppression = Path(tmp) / "suppress.jsonl"
            source.write_text(json.dumps(record) + "\n", encoding="utf-8")
            suppression.write_text(
                json.dumps({"event": "opt_out", "lead_id": "recLedger01"}) + "\n", encoding="utf-8"
            )
            os.environ["SUPPRESSION_SOURCE"] = str(suppression)

            argv = ["score_leads.py", "--input", str(source), "--output", str(output), "--vertical", "plumbing"]
            with patch("sys.argv", argv):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = score_leads.main()

            rows = output.read_text(encoding="utf-8").strip().splitlines()

        self.assertEqual(code, 0)
        self.assertIn("[SKIP] Suppressed leads dropped before scoring: 1", stdout.getvalue())
        self.assertLessEqual(len(rows), 1)  # header only

    def test_score_leads_does_not_read_a_place_id_as_an_opted_out_record_id(self) -> None:
        """The other half of sharing one identifier set: Google keys the Place ID
        as `id`, so a ledger row holding that string must not drop a good lead.

        The top-level `id` is injected here — enrich_leads never writes one —
        precisely so the narrowing is proved for any record that ever does.
        """
        record = _enriched(suppressed=False, id=SuppressionIdentityTests.PLACE_ID)
        record["google"] = {**record["google"], "phone": "(216) 555-9999"}

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "enriched.jsonl"
            output = Path(tmp) / "scored.csv"
            suppression = Path(tmp) / "suppress.jsonl"
            source.write_text(json.dumps(record) + "\n", encoding="utf-8")
            suppression.write_text(
                json.dumps({"event": "opt_out", "record_id": SuppressionIdentityTests.PLACE_ID}) + "\n",
                encoding="utf-8",
            )
            os.environ["SUPPRESSION_SOURCE"] = str(suppression)

            argv = ["score_leads.py", "--input", str(source), "--output", str(output), "--vertical", "plumbing"]
            with patch("sys.argv", argv):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = score_leads.main()

            rows = output.read_text(encoding="utf-8").strip().splitlines()

        self.assertEqual(code, 0)
        self.assertNotIn("Suppressed leads dropped before scoring", stdout.getvalue())
        self.assertEqual(len(rows), 2)  # header plus the lead that was never opted out

    def test_a_scraped_email_in_the_index_also_suppresses_at_scoring_time(self) -> None:
        record = _enriched(suppressed=False)
        record["website"] = {**record["website"], "emails_found": ["bad@test.com"]}

        self.assertTrue(record_matches_suppression(record, SUPPRESSED_INDEX))
        self.assertFalse(record_matches_suppression(record, set()))

    def test_score_leads_drops_suppressed_records_before_spending_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "enriched.jsonl"
            output = Path(tmp) / "scored.csv"
            with source.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(_enriched(suppressed=True)) + "\n")
                handle.write(json.dumps(_enriched(suppressed=False)) + "\n")

            # No live index here on purpose: this pins the stored-flag path.
            os.environ["ALLOW_MISSING_SUPPRESSION_INDEX"] = "1"
            argv = ["score_leads.py", "--input", str(source), "--output", str(output), "--vertical", "plumbing"]
            with patch("sys.argv", argv):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = score_leads.main()

            written = output.read_text(encoding="utf-8")

        self.assertEqual(code, 0)
        self.assertIn("[SKIP] Suppressed leads dropped before scoring: 1", stdout.getvalue())
        self.assertNotIn("dnc_suppressed", written)

    def test_score_leads_fails_closed_without_a_suppression_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "enriched.jsonl"
            source.write_text(json.dumps(_enriched()) + "\n", encoding="utf-8")
            argv = [
                "score_leads.py", "--input", str(source),
                "--output", str(Path(tmp) / "scored.csv"), "--vertical", "plumbing",
            ]
            with patch("sys.argv", argv):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                    code = score_leads.main()

        self.assertEqual(code, 1)
        self.assertIn("No suppression source configured", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
