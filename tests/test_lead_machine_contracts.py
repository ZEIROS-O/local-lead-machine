from __future__ import annotations

import copy
import csv
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import upload_airtable
from enrich_leads import detect_form_signals, scrape_site
from lead_machine.airtable_sync import (
    build_airtable_payload,
    normalize_airtable_value,
    plan_airtable_sync,
)
from lead_machine.buyer_signals import BUYER_INTENT_LOGIC_VERSION
from lead_machine.evidence import build_evidence_profile
from lead_machine.feedback import compute_feedback_metrics
from lead_machine.icp import hard_icp_gate
from lead_machine.lead_card import build_lead_card
from lead_machine.llm_queue import build_llm_prompt, write_review_queue
from lead_machine.manual_scores import load_manual_score, merge_manual_score, validate_manual_score
from lead_machine.schema import normalize_lead, row_dedupe_keys
from lead_machine.sendability import apply_sendability, flatten_safe_outreach_claims, normalize_evidence_refs
from lead_machine.verticals import VERTICAL_PROFILES
from make_packets import is_eligible, main as make_packets_main, packet_markdown
from make_llm_batch import build_batch_prompt, write_llm_batches
from import_llm_score_bundle import import_score_bundle
from refresh_packet_tiers import SchemaMigrationError, refresh_row
from score_leads import main as score_leads_main
from score_leads import governed_opener, read_csv_rows, score_record, score_records_manual, write_csv


AIRTABLE_ENV_VARS = (
    "AIRTABLE_API_KEY",
    "AIRTABLE_TOKEN",
    "AIRTABLE_PAT",
    "AIRTABLE_BASE_ID",
    "AIRTABLE_TABLE_NAME",
    "AIRTABLE_TABLE",
    "AIRTABLE_PROSPECTS_TABLE",
)


class LeadMachineContractsTest(unittest.TestCase):
    def setUp(self) -> None:
        # score_leads fails closed without a suppression source, the same as the
        # search and enrichment entry points. This suite exercises scoring and
        # schema contracts rather than compliance, so it opts out explicitly
        # instead of the gate being softened for everyone. Compliance behaviour
        # is pinned in tests/test_dnc_enrichment.py.
        patcher = patch.dict(os.environ, {"ALLOW_MISSING_SUPPRESSION_INDEX": "1"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_governed_openers_render_actual_copy_across_channels_and_asset_states(self) -> None:
        record = {
            "google": {"business_name": "Davis Roofing"},
            "evidence": {"send_trigger": "the mobile estimate CTA is buried below the service copy"},
        }
        forbidden = ("you don't know me", "this isn't a customer call", "bad time")

        sms = governed_opener(record, "Estimate Funnel", channel="sms", asset_state="none")
        email = governed_opener(record, "Estimate Funnel", channel="email", asset_state="planned")
        phone = governed_opener(record, "Estimate Funnel", channel="phone", asset_state="none")
        existing = governed_opener(
            record,
            "Estimate Funnel",
            channel="sms",
            asset_state="verified-existing",
            asset_id="asset-davis-1",
            asset_verifier_verdict="PASS",
        )

        for draft in (sms, email, phone, existing):
            self.assertIn("Davis Roofing", draft)
            self.assertIn("mobile estimate CTA", draft)
            self.assertFalse(any(phrase in draft.lower() for phrase in forbidden))
        self.assertIn("I can show you what I mean", sms)
        self.assertNotIn("I built", sms)
        self.assertIn("I can build the example", email)
        self.assertNotIn("I built", email)
        self.assertNotIn("I can show", phone)
        self.assertIn("I built a verified example (asset-davis-1)", existing)

        with self.assertRaisesRegex(ValueError, "requires asset_id"):
            governed_opener(record, "Estimate Funnel", asset_state="verified-existing")

    def _airtable_csv_row(self, **overrides: str) -> dict[str, str]:
        row = {
            "Business Name": "A+ Landscaping LLC",
            "Owner Name": "Alex Owner",
            "Vertical": "hardscaping",
            "City": "Kirtland",
            "Phone": "(440) 637-5848",
            "Email": "owner@example.com",
            "Website URL": "https://example.com",
            "Google Maps URL": "https://maps.google.com/?cid=123",
            "Reviews": "130",
            "Rating": "4.9",
            "Current Website Quality": "Weak",
            "Best Service Angle": "Website Rebuild",
            "Specific Observation": "The site hides estimate requests.",
            "Lead Priority": "A",
            "Score Total": "86",
            "Contact Form Detected": "No",
            "Estimate Form Detected": "No",
            "Form Strength": "None",
            "Form Detection Method": "none",
            "CTA Strength": "Weak",
            "Visible Pain Score": "35",
            "Visible Pain Reasons": "No estimate form detected; No clear estimate CTA",
            "Send Trigger": "No estimate form detected",
            "AI Confidence": "medium",
            "Recommended Action": "Build mockup then SMS owner",
            "Opener": "Want me to send a cleaner estimate-page mockup?",
            "Evidence Refs": "ev-google-rating, ev-site",
            "Safe Outreach Claims": "Strong Google review proof.",
            "Unsafe Claims": "Do not claim they are losing jobs.",
            "Packet Eligible": "Yes",
            "Packet Eligibility Reason": "A lead with Website Rebuild angle.",
            "Sendable": "Yes",
            "Packet Tier": "Sendable Packet",
            "Commercial Fit State": "commercially-sendable",
            "Contact Eligibility": "contact-now",
            "Minimum Evidence Passed": "Yes",
            "Send Blockers": "",
            "Place Key": "ChIJ123",
        }
        row.update(overrides)
        return row

    def _write_airtable_csv(self, path: Path, rows: list[dict[str, str]]) -> None:
        fieldnames: list[str] = []
        for row in rows:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def _run_upload(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("sys.argv", argv), redirect_stdout(stdout), redirect_stderr(stderr):
            code = upload_airtable.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def _run_make_packets(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("sys.argv", argv), redirect_stdout(stdout), redirect_stderr(stderr):
            code = make_packets_main()
        return code, stdout.getvalue(), stderr.getvalue()

    def _dry_run_plan(self, stdout: str) -> list[dict[str, object]]:
        lines = stdout.splitlines()
        self.assertTrue(lines and lines[0].startswith("[DRY-RUN]"))
        return json.loads("\n".join(lines[1:]))

    def _write_packet_index(self, packet_dir: Path, rows: list[dict[str, str]]) -> None:
        packet_dir.mkdir(parents=True, exist_ok=True)
        fieldnames = ["Packet Path", "Business Name", "City", "Place Key", "Phone", "Lead Priority", "Offer Fit"]
        with (packet_dir / "packet_index.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    @contextmanager
    def _without_airtable_env(self):
        with patch.dict(os.environ, {}, clear=False), patch("upload_airtable.load_dotenv", return_value=False):
            for key in AIRTABLE_ENV_VARS:
                os.environ.pop(key, None)
            yield

    def test_airtable_csv_dry_run_builds_payload_for_sendable_row_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            self._write_airtable_csv(csv_path, [self._airtable_csv_row(**{"Outreach Channel": ""})])

            with patch("upload_airtable.requests.get") as get:
                code, stdout, stderr = self._run_upload(["upload_airtable.py", "--input", str(csv_path)])

        self.assertEqual(code, 0, stderr)
        get.assert_not_called()
        planned = self._dry_run_plan(stdout)
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["method"], "POST")
        self.assertEqual(planned[0]["upsert_key"], "place:ChIJ123")
        fields = planned[0]["payload"]["fields"]  # type: ignore[index]
        self.assertEqual(fields["Business Name"], "A+ Landscaping LLC")
        self.assertEqual(fields["Status"], "New")
        self.assertEqual(fields["Outreach Channel"], "Phone")
        # Both were dropped from the schema: Best Service Angle duplicated
        # Offer Fit, Next Action restated Status.
        self.assertNotIn("Best Service Angle", fields)
        self.assertNotIn("Next Action", fields)
        self.assertEqual(fields["Verdict"], "send")
        # Form detail stays in the scored CSV and packet; the CRM keeps only
        # the surviving visible-pain summary fields.
        self.assertNotIn("Form Strength", fields)
        for demoted in (
            "Contact Form Detected",
            "Estimate Form Detected",
            "Form Detection Method",
            "CTA Strength",
            "Current Website Quality",
        ):
            self.assertNotIn(demoted, fields)
        self.assertEqual(fields["Visible Pain Score"], 35)
        self.assertEqual(fields["Visible Pain Reasons"], "No estimate form detected; No clear estimate CTA")
        self.assertEqual(fields["Send Trigger"], "No estimate form detected")

    def test_scored_csv_suppresses_placeholder_emails_and_prefers_business_domain(self) -> None:
        record = {
            "google": {
                "business_name": "Fresh Shine Detail",
                "city": "Cleveland",
                "phone": "(216) 555-1212",
                "website_url": "https://freshshinedetail.com",
                "google_maps_url": "https://maps.google.com/?cid=detail123",
                "reviews": 48,
                "rating": 4.8,
                "source_query": "ceramic coating cleveland",
            },
            "website": {
                "site_status": "Poor",
                "domain": "freshshinedetail.com",
                "final_url": "https://freshshinedetail.com",
                "emails_found": [
                    "user@domain.com",
                    "team@gmail.com",
                    "hello@freshshinedetail.com",
                    "test@example.com",
                    "noreply@freshshinedetail.com",
                ],
                "phones_found": ["2165551212"],
                "site_issue_flags": ["weak_basic_form", "only_phone_email_path"],
                "contact_form_detected": "No",
                "estimate_form_detected": "No",
                "form_strength": "None",
                "form_detection_method": "none",
                "cta_strength": "Weak",
                "high_ticket_terms": ["ceramic coating"],
            },
            "evidence": {
                "visible_pain_score": 35,
                "visible_pain_reasons": ["Only phone/email estimate path"],
                "visible_pain_categories": ["estimate_path_pain"],
                "send_trigger": "Only phone/email estimate path",
                "evidence_items": [],
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            write_csv(csv_path, [score_record(record, "detailing")])
            with csv_path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Email"], "hello@freshshinedetail.com, team@gmail.com")
        self.assertNotIn("user@domain.com", rows[0]["Raw Evidence"])

    def test_make_packets_suppresses_placeholder_emails_from_packet_artifacts(self) -> None:
        raw_evidence = {
            "website": {
                "domain": "freshshinedetail.com",
                "final_url": "https://freshshinedetail.com",
                "emails_found": [
                    "user@domain.com",
                    "hello@freshshinedetail.com",
                    "team@gmail.com",
                    "donotreply@freshshinedetail.com",
                ],
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            packet_dir = Path(tmp) / "packets"
            self._write_airtable_csv(
                csv_path,
                [
                    self._airtable_csv_row(
                        **{
                            "Business Name": "Fresh Shine Detail",
                            "Email": "user@domain.com, hello@freshshinedetail.com, team@gmail.com",
                            "Website URL": "https://freshshinedetail.com",
                            "Raw Evidence": json.dumps(raw_evidence),
                        }
                    )
                ],
            )

            code, _stdout, stderr = self._run_make_packets(
                ["make_packets.py", "--inputs", str(csv_path), "--output-dir", str(packet_dir)]
            )
            packet_text = next(packet_dir.glob("*/*.md")).read_text(encoding="utf-8")
            index_text = (packet_dir / "packet_index.csv").read_text(encoding="utf-8")

        self.assertEqual(code, 0, stderr)
        self.assertIn("hello@freshshinedetail.com, team@gmail.com", packet_text)
        self.assertIn("hello@freshshinedetail.com, team@gmail.com", index_text)
        self.assertNotIn("user@domain.com", packet_text)
        self.assertNotIn("donotreply@freshshinedetail.com", packet_text)
        self.assertNotIn("user@domain.com", index_text)

    def test_airtable_csv_dry_run_normalizes_lane_labels_for_select_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            self._write_airtable_csv(
                csv_path,
                [
                    self._airtable_csv_row(
                        **{
                            "Best Service Angle": "Social/Jobber Escape",
                            "Offer Fit": "social_jobber_escape",
                            "Send Trigger": "social_jobber_escape lane: no owned website",
                        }
                    )
                ],
            )

            code, stdout, stderr = self._run_upload(["upload_airtable.py", "--input", str(csv_path)])

        self.assertEqual(code, 0, stderr)
        planned = self._dry_run_plan(stdout)
        fields = planned[0]["payload"]["fields"]  # type: ignore[index]
        self.assertEqual(fields["Offer Fit"], "No-Site Launch")
        self.assertNotIn("Best Service Angle", fields)
        self.assertEqual(fields["Send Trigger"], "social_jobber_escape lane: no owned website")

    def test_airtable_payload_normalizes_all_internal_lane_labels_for_select_fields(self) -> None:
        cases = {
            "Social/Jobber Escape": "No-Site Launch",
            "no_site_launch": "No-Site Launch",
            "social_jobber_escape": "No-Site Launch",
            "owned_weak_site_rebuild": "Website Rebuild",
            "premium_perception_gap": "Website Rebuild",
            "portfolio_presentation_gap": "Website Rebuild",
            "estimate_funnel": "Estimate Funnel",
            "optimization_only": "Automation / Follow-Up",
        }
        supported_select_values = set(cases.values())

        for lane, expected in cases.items():
            with self.subTest(lane=lane):
                card = {
                    "place_id_or_unique_key": "ChIJ123",
                    "best_angle": lane,
                    "airtable_fields": {
                        "Business Name": "A+ Landscaping LLC",
                        "Best Service Angle": lane,
                        "Offer Fit": lane,
                        "Specific Observation": f"Internal lane: {lane}",
                        "Visible Pain Reasons": f"Internal lane: {lane}",
                        "Send Trigger": f"Internal lane: {lane}",
                        "Opener": f"Internal lane: {lane}",
                    },
                }

                fields = build_airtable_payload(card)["fields"]

                self.assertEqual(fields["Offer Fit"], expected)
                self.assertIn(fields["Offer Fit"], supported_select_values)
                # Offer Fit is now the only service-angle column that syncs.
                self.assertNotIn("Best Service Angle", fields)
                self.assertEqual(fields["Specific Observation"], f"Internal lane: {lane}")
                self.assertEqual(fields["Visible Pain Reasons"], f"Internal lane: {lane}")
                self.assertEqual(fields["Send Trigger"], f"Internal lane: {lane}")
                self.assertEqual(fields["Opener"], f"Internal lane: {lane}")

    def test_airtable_payload_omits_unsupported_vertical_select_values(self) -> None:
        unsupported_card = {
            "place_id_or_unique_key": "ChIJ123",
            "airtable_fields": {
                "Business Name": "Tint Shop",
                "Vertical": "window tinting",
                "Phone": "(614) 555-0101",
            },
        }

        fields = build_airtable_payload(unsupported_card)["fields"]

        self.assertNotIn("Vertical", fields)
        self.assertEqual(fields["Business Name"], "Tint Shop")

        for vertical in ("water restoration", "water_restoration"):
            card = {
                "place_id_or_unique_key": "ChIJ456",
                "airtable_fields": {
                    "Business Name": "Columbus Restoration",
                    "Vertical": vertical,
                    "Phone": "(614) 555-0102",
                },
            }

            fields = build_airtable_payload(card)["fields"]

            self.assertEqual(fields["Vertical"], "water_restoration")

    def test_airtable_plan_reports_dropped_unsupported_vertical_values(self) -> None:
        card = {
            "place_id_or_unique_key": "ChIJ123",
            "airtable_fields": {
                "Business Name": "Tint Shop",
                "Vertical": "window tinting",
                "Phone": "(614) 555-0101",
            },
        }

        planned = plan_airtable_sync([card])

        self.assertEqual(planned[0]["dropped_fields"], [{"field": "Vertical", "value": "window tinting"}])
        self.assertNotIn("Vertical", planned[0]["payload"]["fields"])

    def test_airtable_csv_dry_run_accepts_sparse_scored_row_and_upserts_by_phone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            self._write_airtable_csv(
                csv_path,
                [
                    {
                        "Business Name": "Phone Only Landscaping",
                        "Phone": "(440) 637-5848",
                        "Lead Priority": "A",
                        "Score Total": "86",
                        "Sendable": "Yes",
                        "Packet Eligible": "Yes",
                    }
                ],
            )

            code, stdout, stderr = self._run_upload(["upload_airtable.py", "--input", str(csv_path)])

        self.assertEqual(code, 0, stderr)
        planned = self._dry_run_plan(stdout)
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["method"], "POST")
        self.assertEqual(planned[0]["upsert_key"], "phone:4406375848")
        fields = planned[0]["payload"]["fields"]  # type: ignore[index]
        self.assertEqual(fields["Business Name"], "Phone Only Landscaping")
        self.assertEqual(fields["Phone"], "(440) 637-5848")
        self.assertEqual(fields["Lead Priority"], "A")
        self.assertEqual(fields["Score Total"], 86)
        # No Packet Tier column in this legacy-shaped CSV, so it is derived. The
        # row carries Sendable=Yes but no commercial fit or contact eligibility,
        # so it fails the readiness contract and lands as a manual-review packet
        # rather than inheriting the bare Sendable string it used to sync.
        self.assertEqual(fields["Verdict"], "review")
        self.assertNotIn("Sendable", fields)
        self.assertNotIn("Packet Eligible", fields)
        self.assertNotIn("Recommended Action", fields)
        self.assertNotIn("Place Key", fields)

    def test_airtable_csv_dry_run_derives_no_packet_tier_rather_than_leaving_it_blank(self) -> None:
        """A computed 'no packet' answer must not look like 'never computed'.

        Blank Packet Tier is the state that silently emptied the send queue, so
        it has to keep meaning exactly one thing: the pipeline never wrote a
        tier for this record. A row the pipeline *did* judge, and judged as
        unsendable, gets the explicit No Packet option instead.
        """
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            self._write_airtable_csv(
                csv_path,
                [
                    {
                        "Business Name": "No Packet Landscaping",
                        "Phone": "(440) 637-5849",
                        "Lead Priority": "C",
                        "Score Total": "41",
                        "Sendable": "No",
                        "Packet Eligible": "",
                    }
                ],
            )

            code, stdout, stderr = self._run_upload(["upload_airtable.py", "--input", str(csv_path)])

        self.assertEqual(code, 0, stderr)
        planned = self._dry_run_plan(stdout)
        self.assertEqual(len(planned), 1)
        fields = planned[0]["payload"]["fields"]  # type: ignore[index]
        self.assertEqual(fields["Verdict"], "reject")

    def test_a_derived_verdict_never_clobbers_an_existing_one(self) -> None:
        """A derived verdict is a fallback for new records, not an overwrite.

        The derivation only sees the CSV, so on a record that already exists it
        could downgrade a verdict the base got from a fuller scored run. Marking
        it defaulted keeps it out of the PATCH payload while still seeding a new
        row on POST. This used to be spelled with `Packet Tier`, which no longer
        reaches Airtable at all; `Verdict` is the field the mechanism protects
        now, and the Call Queue reads it alone.
        """
        card = {
            "place_id_or_unique_key": "ChIJ-no-packet",
            "airtable_fields": {
                "Business Name": "No Packet Landscaping",
                "Verdict": "reject",
            },
            "defaulted_airtable_fields": ["Verdict"],
        }

        posted = plan_airtable_sync([card])
        patched = plan_airtable_sync([card], existing_records_by_key={"place:ChIJ-no-packet": "recExisting"})

        self.assertEqual(posted[0]["method"], "POST")
        self.assertEqual(posted[0]["payload"]["fields"]["Verdict"], "reject")
        self.assertEqual(patched[0]["method"], "PATCH")
        self.assertNotIn("Verdict", patched[0]["payload"]["fields"])

    def test_the_seven_superseded_columns_are_never_written(self) -> None:
        """`Verdict` replaced them, and step 4 of the cutover deletes them.

        A payload naming a field the base does not have is a 422, not a blank,
        so the write path has to stop naming them *before* the columns go. The
        allowlist is the one chokepoint every payload passes through, which is
        why this asserts on both the list and a built payload.
        """
        from lead_machine.airtable_sync import AIRTABLE_ALLOWED_FIELDS

        superseded = [
            "Packet Tier",
            "Commercial Fit State",
            "Contact Eligibility",
            "Asset State",
            "Promise State",
            "Send Blockers",
            "Skip Reason",
        ]
        for field in superseded:
            self.assertNotIn(field, AIRTABLE_ALLOWED_FIELDS, field)

        with tempfile.TemporaryDirectory() as tmp:
            card = upload_airtable.csv_row_to_record(
                {
                    "Business Name": "Everything Set Co",
                    "Place Key": "ChIJ-everything",
                    "Packet Tier": "Sendable Packet",
                    "Sendable": "Yes",
                    "Packet Eligible": "Yes",
                    "Commercial Fit State": "commercially-sendable",
                    "Contact Eligibility": "contact-now",
                    "Asset State": "ready",
                    "Promise State": "kept",
                    "Send Blockers": "something",
                    "Skip Reason": "something",
                },
                Path(tmp),
            )

        posted = plan_airtable_sync([card])[0]["payload"]["fields"]
        for field in superseded:
            self.assertNotIn(field, posted, field)
        # The tier still drives the verdict internally; only the column is gone.
        self.assertEqual(posted["Verdict"], "send")

    def test_airtable_derived_verdict_travels_with_the_derived_tier(self) -> None:
        """A Verdict minted from a defaulted tier is just as much a guess.

        `Verdict` is computed from `Packet Tier`, so when the tier had to be
        derived from the booleans the verdict inherits that uncertainty. If only
        the tier is held back from the PATCH, the base keeps its real tier and
        gets the guessed verdict beside it — the two disagree, and because the
        Call Queue now filters on `Verdict` alone, a live lead silently leaves
        the dial list.
        """
        with tempfile.TemporaryDirectory() as tmp:
            card = upload_airtable.csv_row_to_record(
                {
                    "Business Name": "Legacy CSV Landscaping",
                    "Place Key": "ChIJ-legacy-csv",
                    "Sendable": "No",
                    "Packet Eligible": "No",
                },
                Path(tmp),
            )

        self.assertEqual(card["airtable_fields"]["Packet Tier"], "No Packet")
        self.assertEqual(card["airtable_fields"]["Verdict"], "reject")
        self.assertIn("Verdict", card["defaulted_airtable_fields"])

        patched = plan_airtable_sync(
            [card], existing_records_by_key={"place:ChIJ-legacy-csv": "recExisting"}
        )
        self.assertEqual(patched[0]["method"], "PATCH")
        self.assertNotIn("Packet Tier", patched[0]["payload"]["fields"])
        self.assertNotIn("Verdict", patched[0]["payload"]["fields"])

    def test_airtable_patch_never_carries_human_owned_operational_fields(self) -> None:
        """Re-uploading a scored CSV must not reset a lead the rep already moved.

        `score_leads` writes `Status: "New"` on every scored row, so the CSV
        always supplies a non-blank Status. Copying it into a PATCH overwrote
        whatever the human set: a lead that was dialled and moved to `Follow-Up` went
        back to `New`, which is exactly `queue_state.QUEUE_FILTER_FORMULA`, so
        the morning queue served it again and the prospect was contacted twice.
        The same PATCH reverted `Won` / `Lost` / `Not Fit`.

        `lead_machine/verdict.py` already documents the contract: `Status` is
        human-owned and never machine-written except by mark_sent / mark_outcome.
        """
        # Deliberately a row no readiness branch rewrites, so what reaches the
        # payload is the CSV's own `Status` and nothing else. A sendable or
        # manual-review row is covered by the same rule — those branches choose a
        # Status themselves and mark it defaulted — but they would mask which
        # value is under test here.
        scored_row = {
            "Business Name": "Twice Dialled Detailing",
            "Place Key": "ChIJ-twice-dialled",
            "Phone": "+1 216 555 0134",
            # What score_leads.py actually writes on every scored row.
            "Status": "New",
            "Outreach Channel": "Phone",
        }
        with tempfile.TemporaryDirectory() as tmp:
            card = upload_airtable.csv_row_to_record(scored_row, Path(tmp))

        # Creating the record still seeds both — a new lead has no human state
        # to protect, and a blank Status would never match the queue filter.
        posted = plan_airtable_sync([card])[0]
        self.assertEqual(posted["method"], "POST")
        self.assertEqual(posted["payload"]["fields"]["Status"], "New")
        self.assertEqual(posted["payload"]["fields"]["Outreach Channel"], "Phone")

        patched = plan_airtable_sync(
            [card], existing_records_by_key={"place:ChIJ-twice-dialled": "recDialled"}
        )[0]
        self.assertEqual(patched["method"], "PATCH")
        self.assertNotIn("Status", patched["payload"]["fields"])
        self.assertNotIn("Outreach Channel", patched["payload"]["fields"])
        # The machine-owned half of the payload still travels; this is a
        # narrowing of who owns two columns, not a stop on updating the record.
        self.assertEqual(patched["payload"]["fields"]["Business Name"], "Twice Dialled Detailing")

    def test_airtable_patch_holds_back_a_machine_chosen_status_too(self) -> None:
        """The manual-review branch picks a Status; it is still not the owner.

        `csv_row_to_record` overwrites the CSV's `New` with `Manual Review` for a
        manual-review packet. That is a machine decision about a lead the rep has
        not seen yet, so it belongs on the POST — and must still never land on a
        PATCH, where it would drag a lead the rep had already moved back into the
        review pile.
        """
        manual_row = {
            "Business Name": "Needs A Look Landscaping",
            "Place Key": "ChIJ-needs-a-look",
            "Packet Eligible": "Yes",
            "Status": "New",
        }
        with tempfile.TemporaryDirectory() as tmp:
            card = upload_airtable.csv_row_to_record(manual_row, Path(tmp))

        posted = plan_airtable_sync([card])[0]
        self.assertEqual(posted["payload"]["fields"]["Status"], "Manual Review")

        patched = plan_airtable_sync(
            [card], existing_records_by_key={"place:ChIJ-needs-a-look": "recLooked"}
        )[0]
        self.assertEqual(patched["method"], "PATCH")
        self.assertNotIn("Status", patched["payload"]["fields"])

    def test_airtable_patch_clears_verdict_reasons_when_the_verdict_is_send(self) -> None:
        """An empty reason list is an answer, not a missing value.

        Airtable leaves unmentioned fields alone on PATCH, so dropping the empty
        string would leave reject-era prose sitting next to `Verdict = send`.
        `Verdict Reasons` is machine-owned, so it is written on every row the
        verdict is written on.
        """
        with tempfile.TemporaryDirectory() as tmp:
            card = upload_airtable.csv_row_to_record(
                {
                    "Business Name": "Fresh Sendable Detailing",
                    "Place Key": "ChIJ-fresh-send",
                    "Packet Tier": "Sendable Packet",
                    "Sendable": "Yes",
                    "Packet Eligible": "Yes",
                    "Commercial Fit State": "commercially-sendable",
                    "Contact Eligibility": "contact-now",
                    "Verdict Reasons": "",
                    "Send Blockers": "",
                },
                Path(tmp),
            )

        patched = plan_airtable_sync(
            [card], existing_records_by_key={"place:ChIJ-fresh-send": "recExisting"}
        )
        fields = patched[0]["payload"]["fields"]
        self.assertEqual(patched[0]["method"], "PATCH")
        self.assertEqual(fields["Verdict"], "send")
        self.assertEqual(fields["Verdict Reasons"], "")

    def test_airtable_csv_dry_run_preserves_explicit_sendable_operational_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            self._write_airtable_csv(
                csv_path,
                [
                    self._airtable_csv_row(
                        **{
                            "Status": "Contacted",
                            "Outreach Channel": "Email",
                        }
                    )
                ],
            )

            code, stdout, stderr = self._run_upload(["upload_airtable.py", "--input", str(csv_path)])

        self.assertEqual(code, 0, stderr)
        planned = self._dry_run_plan(stdout)
        self.assertEqual(len(planned), 1)
        fields = planned[0]["payload"]["fields"]  # type: ignore[index]
        self.assertEqual(fields["Status"], "Contacted")
        self.assertEqual(fields["Outreach Channel"], "Email")

    def test_airtable_patch_does_not_send_defaulted_operational_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            self._write_airtable_csv(csv_path, [self._airtable_csv_row(**{"Outreach Channel": ""})])

            records = upload_airtable.read_input_records(csv_path, packet_dir=Path(tmp))
            planned = plan_airtable_sync(records, existing_records_by_key={"place:ChIJ123": "rec-existing"})

        self.assertEqual(planned[0]["method"], "PATCH")
        fields = planned[0]["payload"]["fields"]  # type: ignore[index]
        self.assertNotIn("Status", fields)
        self.assertNotIn("Outreach Channel", fields)

    def test_make_packets_updates_scored_csv_with_current_batch_packet_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                csv_path = Path("output/scored_current_batch_manual.csv")
                csv_path.parent.mkdir(parents=True)
                stale_path = "packets_manual_existing_manual/A_no-site-launch/003-m-m-masonry-and-landscape-llc.md"
                self._write_airtable_csv(
                    csv_path,
                    [
                        self._airtable_csv_row(
                            **{
                                "Business Name": "M & M Masonry and Landscape LLC",
                                "Phone": "(440) 622-0083",
                                "Place Key": "ChIJe1e2bAl1MYgRmYxkuR04D40",
                                "Lead Priority": "A",
                                "Offer Fit": "No-Site Launch",
                                "Packet Path": stale_path,
                            }
                        )
                    ],
                )

                code, _stdout, stderr = self._run_make_packets(
                    [
                        "make_packets.py",
                        "--inputs",
                        str(csv_path),
                        "--output-dir",
                        "packets_current_batch_manual",
                    ]
                )

                with csv_path.open(newline="", encoding="utf-8") as f:
                    rows = list(csv.DictReader(f))
                tiered_mtime = csv_path.stat().st_mtime_ns
                index_mtime = Path("packets_current_batch_manual/packet_index.csv").stat().st_mtime_ns
            finally:
                os.chdir(old_cwd)

        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            rows[0]["Packet Path"],
            "packets_current_batch_manual/A_no-site-launch/001-m-m-masonry-and-landscape-llc.md",
        )
        self.assertNotIn("packets_manual_existing_manual", rows[0]["Packet Path"])
        self.assertGreaterEqual(index_mtime, tiered_mtime)

    def test_airtable_uses_current_batch_packet_index_instead_of_stale_packet_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                csv_path = Path("output/scored_current_batch_manual.csv")
                csv_path.parent.mkdir(parents=True)
                current_path = "packets_current_batch_manual/A_no-site-launch/004-m-m-masonry-and-landscape-llc.md"
                stale_path = "packets_manual_existing_manual/A_no-site-launch/003-m-m-masonry-and-landscape-llc.md"
                Path(current_path).parent.mkdir(parents=True)
                Path(current_path).write_text("current", encoding="utf-8")
                Path(stale_path).parent.mkdir(parents=True)
                Path(stale_path).write_text("stale", encoding="utf-8")
                self._write_packet_index(
                    Path("packets_current_batch_manual"),
                    [
                        {
                            "Packet Path": current_path,
                            "Business Name": "M & M Masonry and Landscape LLC",
                            "Place Key": "ChIJe1e2bAl1MYgRmYxkuR04D40",
                            "Phone": "(440) 622-0083",
                            "Lead Priority": "A",
                            "Offer Fit": "No-Site Launch",
                        }
                    ],
                )
                self._write_airtable_csv(
                    csv_path,
                    [
                        self._airtable_csv_row(
                            **{
                                "Business Name": "M & M Masonry and Landscape LLC",
                                "Phone": "(440) 622-0083",
                                "Place Key": "ChIJe1e2bAl1MYgRmYxkuR04D40",
                                "Packet Path": stale_path,
                            }
                        )
                    ],
                )

                code, stdout, stderr = self._run_upload(
                    ["upload_airtable.py", "--input", str(csv_path)]
                )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(code, 0, stderr)
        planned = self._dry_run_plan(stdout)
        fields = planned[0]["payload"]["fields"]  # type: ignore[index]
        self.assertEqual(fields["Packet Path"], current_path)
        self.assertNotIn("packets_manual_existing_manual", fields["Packet Path"])

    def test_packet_index_legacy_fallback_requires_unique_business_and_city(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                packet_dir = Path("packets_legacy")
                cleveland_path = packet_dir / "cleveland.md"
                columbus_path = packet_dir / "columbus.md"
                packet_dir.mkdir()
                cleveland_path.write_text("cleveland", encoding="utf-8")
                columbus_path.write_text("columbus", encoding="utf-8")
                self._write_packet_index(
                    packet_dir,
                    [
                        {"Packet Path": str(cleveland_path), "Business Name": "Same Name LLC", "City": "Cleveland"},
                        {"Packet Path": str(columbus_path), "Business Name": "Same Name LLC", "City": "Columbus"},
                    ],
                )
                matched = upload_airtable.existing_packet_path(
                    {"Business Name": "Same Name LLC", "City": "Cleveland", "Place Key": ""}, packet_dir
                )
            finally:
                os.chdir(old_cwd)
        self.assertEqual(matched, str(cleveland_path))

    def test_packet_index_refuses_duplicate_place_and_legacy_identities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                packet_dir = Path("packets_ambiguous")
                packet_dir.mkdir()
                first = packet_dir / "first.md"
                second = packet_dir / "second.md"
                first.write_text("first", encoding="utf-8")
                second.write_text("second", encoding="utf-8")
                self._write_packet_index(
                    packet_dir,
                    [
                        {"Packet Path": str(first), "Place Key": "ChIJdup", "Business Name": "Duplicate Co", "City": "Cleveland"},
                        {"Packet Path": str(second), "Place Key": "ChIJdup", "Business Name": "Duplicate Co", "City": "Cleveland"},
                    ],
                )
                with self.assertRaisesRegex(ValueError, "duplicate Place Key"):
                    upload_airtable.existing_packet_path(
                        {"Business Name": "Duplicate Co", "City": "Cleveland", "Place Key": "ChIJdup"}, packet_dir
                    )
                with self.assertRaisesRegex(ValueError, "ambiguous legacy identity"):
                    upload_airtable.existing_packet_path(
                        {"Business Name": "Duplicate Co", "City": "Cleveland", "Place Key": ""}, packet_dir
                    )
            finally:
                os.chdir(old_cwd)

    def test_packet_index_counts_missing_packet_rows_before_identity_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                packet_dir = Path("packets_ambiguous_missing")
                packet_dir.mkdir()
                existing = packet_dir / "existing.md"
                missing = packet_dir / "missing.md"
                existing.write_text("existing", encoding="utf-8")
                self._write_packet_index(
                    packet_dir,
                    [
                        {"Packet Path": str(existing), "Place Key": "ChIJdup", "Business Name": "Duplicate Co", "City": "Cleveland"},
                        {"Packet Path": str(missing), "Place Key": "ChIJdup", "Business Name": "Duplicate Co", "City": "Cleveland"},
                    ],
                )
                with self.assertRaisesRegex(ValueError, "duplicate Place Key"):
                    upload_airtable.existing_packet_path(
                        {"Business Name": "Duplicate Co", "City": "Cleveland", "Place Key": "ChIJdup"},
                        packet_dir,
                    )
                with self.assertRaisesRegex(ValueError, "ambiguous legacy identity"):
                    upload_airtable.existing_packet_path(
                        {"Business Name": "Duplicate Co", "City": "Cleveland", "Place Key": ""},
                        packet_dir,
                    )
            finally:
                os.chdir(old_cwd)

    def test_packet_index_refuses_unique_match_with_missing_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                packet_dir = Path("packets_missing")
                packet_dir.mkdir()
                missing = packet_dir / "missing.md"
                self._write_packet_index(
                    packet_dir,
                    [
                        {"Packet Path": str(missing), "Place Key": "ChIJmissing", "Business Name": "Missing Co", "City": "Cleveland"},
                    ],
                )
                with self.assertRaisesRegex(ValueError, "points to a missing packet"):
                    upload_airtable.existing_packet_path(
                        {"Business Name": "Missing Co", "City": "Cleveland", "Place Key": "ChIJmissing"},
                        packet_dir,
                    )
            finally:
                os.chdir(old_cwd)

    def test_packet_lookup_does_not_fall_back_to_phone_name_or_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                packet_dir = Path("packets_no_heuristics")
                packet_dir.mkdir()
                packet = packet_dir / "001-filename-only-co.md"
                packet.write_text("packet", encoding="utf-8")
                self._write_packet_index(
                    packet_dir,
                    [{"Packet Path": str(packet), "Business Name": "Filename Only Co", "City": "", "Phone": "2165550101"}],
                )
                matched = upload_airtable.existing_packet_path(
                    {"Business Name": "Filename Only Co", "City": "", "Phone": "2165550101", "Place Key": ""},
                    packet_dir,
                )
            finally:
                os.chdir(old_cwd)
        self.assertEqual(matched, "")

    def test_airtable_payload_uses_current_packet_path_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                csv_path = Path("output/scored_current_batch_manual.csv")
                csv_path.parent.mkdir(parents=True)
                current_path = "packets_current_batch_manual/A_website-rebuild/001-a-landscaping-llc.md"
                Path(current_path).parent.mkdir(parents=True)
                Path(current_path).write_text("current", encoding="utf-8")
                self._write_airtable_csv(
                    csv_path,
                    [self._airtable_csv_row(**{"Offer Fit": "Website Rebuild", "Packet Path": current_path})],
                )

                records = upload_airtable.read_input_records(
                    csv_path,
                    packet_dir=Path("packets_current_batch_manual"),
                )
                planned = plan_airtable_sync(records, existing_records_by_key={})
            finally:
                os.chdir(old_cwd)

        fields = planned[0]["payload"]["fields"]  # type: ignore[index]
        self.assertEqual(fields["Packet Path"], current_path)

    def test_airtable_csv_reader_plans_every_scored_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            rows = [
                self._airtable_csv_row(
                    **{"Business Name": "Sendable Co", "Packet Tier": "Sendable Packet", "Place Key": "ChIJ1"}
                ),
                self._airtable_csv_row(
                    **{
                        "Business Name": "Manual Co",
                        "Sendable": "No",
                        "Packet Tier": "Manual Review Packet",
                        "Place Key": "ChIJ2",
                    }
                ),
                self._airtable_csv_row(
                    **{
                        "Business Name": "Watchlist Co",
                        "Sendable": "No",
                        "Packet Eligible": "",
                        "Packet Tier": "Watchlist",
                        "Place Key": "ChIJ3",
                    }
                ),
                self._airtable_csv_row(
                    **{
                        "Business Name": "No Packet Co",
                        "Sendable": "No",
                        "Packet Eligible": "No",
                        "Packet Tier": "No Packet",
                        "Place Key": "ChIJ4",
                    }
                ),
            ]
            self._write_airtable_csv(csv_path, rows)
            records = upload_airtable.read_csv_records(csv_path, packet_dir=Path(tmp))
            planned = plan_airtable_sync(records, existing_records_by_key={})

        names = {p["payload"]["fields"]["Business Name"] for p in planned}
        self.assertEqual(
            names,
            {"Sendable Co", "Manual Co", "Watchlist Co", "No Packet Co"},
        )

    def test_airtable_plan_tightens_stale_sendable_tier_to_manual_when_packet_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            stale = self._airtable_csv_row(
                **{
                    "Raw Evidence": json.dumps(
                        {
                            "derived": {
                                "hard_icp_skip": True,
                                "hard_icp_reasons": ["too_few_reviews"],
                            }
                        }
                    ),
                    "Sendable": "Yes",
                    "Packet Eligible": "Yes",
                    "Packet Tier": "Sendable Packet",
                }
            )
            self._write_airtable_csv(csv_path, [stale])

            records = upload_airtable.read_csv_records(csv_path, packet_dir=Path(tmp))
            planned = plan_airtable_sync(records, existing_records_by_key={})

        fields = planned[0]["payload"]["fields"]
        self.assertEqual(fields["Verdict"], "review")
        self.assertEqual(fields["Status"], "Manual Review")
        self.assertNotEqual(fields["Verdict"], "send")

    def test_airtable_plan_tightens_stale_sendable_tier_to_none_when_not_packet_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            stale = self._airtable_csv_row(
                **{
                    "Sendable": "No",
                    "Packet Eligible": "No",
                    "Packet Tier": "Sendable Packet",
                }
            )
            self._write_airtable_csv(csv_path, [stale])

            records = upload_airtable.read_csv_records(csv_path, packet_dir=Path(tmp))
            planned = plan_airtable_sync(records, existing_records_by_key={})

        fields = planned[0]["payload"]["fields"]
        self.assertEqual(fields["Verdict"], "reject")
        self.assertNotEqual(fields.get("Status"), "Manual Review")

    def test_airtable_retired_subset_flags_fail_in_parser_before_network(self) -> None:
        for retired_flag in ("--only-sendable", "--include-manual-review"):
            with self.subTest(flag=retired_flag):
                stderr = io.StringIO()
                with (
                    patch("sys.argv", ["upload_airtable.py", "--input", "missing.csv", retired_flag]),
                    patch("upload_airtable.requests.get") as get,
                    patch("upload_airtable.requests.post") as post,
                    patch("upload_airtable.requests.patch") as patch_request,
                    redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as raised,
                ):
                    upload_airtable.main()
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("unrecognized arguments", stderr.getvalue())
                get.assert_not_called()
                post.assert_not_called()
                patch_request.assert_not_called()

    def test_make_airtable_targets_upload_all_rows_without_subset_flags(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        for target in ("airtable-dry", "airtable-live"):
            with self.subTest(target=target):
                result = subprocess.run(
                    ["make", "-n", target],
                    cwd=repo_root,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("upload_airtable.py", result.stdout)
                self.assertNotIn("--only-sendable", result.stdout)
                self.assertNotIn("--include-manual-review", result.stdout)

    def test_run_lead_batch_skill_retrieves_total_upload_command(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        skill = (repo_root / ".claude/skills/run-lead-batch/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("upload_airtable.py --dry-run", skill)
        self.assertNotIn("--only-sendable", skill)
        self.assertNotIn("--include-manual-review", skill)

    def test_airtable_manual_review_packet_gets_status_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            rows = [
                self._airtable_csv_row(**{"Business Name": "Manual Co", "Sendable": "No", "Packet Tier": "Manual Review Packet", "Place Key": "ChIJ2"}),
            ]
            self._write_airtable_csv(csv_path, rows)
            code, stdout, stderr = self._run_upload(["upload_airtable.py", "--input", str(csv_path)])
        self.assertEqual(code, 0, stderr)
        planned = self._dry_run_plan(stdout)
        self.assertEqual(len(planned), 1)
        fields = planned[0]["payload"]["fields"]
        self.assertEqual(fields.get("Status"), "Manual Review")

    def test_airtable_manual_review_packet_overrides_csv_status_new(self) -> None:
        """Manual Review Packet rows must get Status='Manual Review' even if CSV has Status='New'."""
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            rows = [
                self._airtable_csv_row(**{
                    "Business Name": "Manual Co With Status",
                    "Sendable": "No",
                    "Packet Tier": "Manual Review Packet",
                    "Place Key": "ChIJ9",
                    "Status": "New",
                }),
            ]
            self._write_airtable_csv(csv_path, rows)
            code, stdout, stderr = self._run_upload(["upload_airtable.py", "--input", str(csv_path)])
        self.assertEqual(code, 0, stderr)
        planned = self._dry_run_plan(stdout)
        self.assertEqual(len(planned), 1)
        fields = planned[0]["payload"]["fields"]
        self.assertEqual(fields.get("Status"), "Manual Review")

    def test_airtable_platform_emails_removed_real_gmail_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            rows = [
                self._airtable_csv_row(**{
                    "Business Name": "Zimon Detailing",
                    "Email": "errors@sentry.io, zimon@gmail.com",
                    "Place Key": "ChIJ1",
                }),
                self._airtable_csv_row(**{
                    "Business Name": "Haynes Auto",
                    "Email": "support@webador.com",
                    "Place Key": "ChIJ2",
                }),
            ]
            self._write_airtable_csv(csv_path, rows)
            records = upload_airtable.read_input_records(csv_path, packet_dir=Path(tmp))
        zimon = next(r for r in records if r["business_name"] == "Zimon Detailing")
        haynes = next(r for r in records if r["business_name"] == "Haynes Auto")
        self.assertEqual(zimon["airtable_fields"].get("Email"), "zimon@gmail.com")
        self.assertNotIn("Email", haynes["airtable_fields"])

    def test_airtable_csv_upsert_key_prefers_place_key_then_normalized_phone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            rows = [
                self._airtable_csv_row(),
                self._airtable_csv_row(**{"Business Name": "Phone Only LLC", "Place Key": "", "Phone": "+1 440.555.0101"}),
            ]
            self._write_airtable_csv(csv_path, rows)

            records = upload_airtable.read_input_records(csv_path, packet_dir=Path(tmp))
            planned = plan_airtable_sync(
                records,
                existing_records_by_key={"place:ChIJ123": "rec-place", "phone:4405550101": "rec-phone"},
            )

        self.assertEqual(planned[0]["method"], "PATCH")
        self.assertEqual(planned[0]["record_id"], "rec-place")
        self.assertEqual(planned[0]["upsert_key"], "place:ChIJ123")
        self.assertEqual(planned[1]["method"], "PATCH")
        self.assertEqual(planned[1]["record_id"], "rec-phone")
        self.assertEqual(planned[1]["upsert_key"], "phone:4405550101")

    def test_airtable_sync_rejects_duplicate_input_upsert_keys(self) -> None:
        first = {
            "upsert_key": "phone:4405550101",
            "airtable_fields": {"Business Name": "First Patio Co", "Phone": "(440) 555-0101"},
        }
        duplicate = {
            "upsert_key": "phone:4405550101",
            "airtable_fields": {"Business Name": "Duplicate Patio Co", "Phone": "(440) 555-0101"},
        }

        with self.assertRaises(ValueError) as caught:
            plan_airtable_sync([first, duplicate], existing_records_by_key={})

        self.assertIn("Duplicate Airtable upsert key in input: phone:4405550101", str(caught.exception))

    def test_airtable_existing_phone_key_matches_incoming_place_key(self) -> None:
        record = {
            "upsert_key": "place:ChIJ-new",
            "place_id_or_unique_key": "ChIJ-new",
            "phone": "(440) 555-0101",
            "airtable_fields": {
                "Business Name": "Flawless Coating and Detailing",
                "Place Key": "ChIJ-new",
                "Phone": "(440) 555-0101",
            },
        }

        planned = plan_airtable_sync([record], existing_records_by_key={"phone:4405550101": "rec-phone"})

        self.assertEqual(planned[0]["method"], "PATCH")
        self.assertEqual(planned[0]["record_id"], "rec-phone")
        self.assertEqual(planned[0]["upsert_key"], "place:ChIJ-new")
        self.assertEqual(planned[0]["match_reason"], "phone")
        self.assertEqual(planned[0]["payload"]["fields"]["Place Key"], "ChIJ-new")

    def test_airtable_existing_domain_key_matches_incoming_place_key(self) -> None:
        record = {
            "upsert_key": "place:ChIJ-new",
            "place_id_or_unique_key": "ChIJ-new",
            "airtable_fields": {
                "Business Name": "Fresh Shine Detail",
                "Place Key": "ChIJ-new",
                "Website URL": "https://www.freshshinedetail.com/services",
            },
        }

        planned = plan_airtable_sync([record], existing_records_by_key={"domain:freshshinedetail.com": "rec-domain"})

        self.assertEqual(planned[0]["method"], "PATCH")
        self.assertEqual(planned[0]["record_id"], "rec-domain")
        self.assertEqual(planned[0]["match_reason"], "domain")

    def test_airtable_generic_social_domain_does_not_match_existing_record(self) -> None:
        record = {
            "upsert_key": "place:ChIJ-new",
            "place_id_or_unique_key": "ChIJ-new",
            "airtable_fields": {
                "Business Name": "Peach Drive Auto",
                "Place Key": "ChIJ-new",
                "Website URL": "https://www.instagram.com/peachdriveauto",
            },
        }

        planned = plan_airtable_sync([record], existing_records_by_key={"domain:instagram.com": "rec-action-tint"})

        self.assertEqual(planned[0]["method"], "POST")
        self.assertNotIn("match_reason", planned[0])

    def test_airtable_existing_business_city_key_matches_when_unique(self) -> None:
        record = {
            "upsert_key": "place:ChIJ-new",
            "place_id_or_unique_key": "ChIJ-new",
            "airtable_fields": {
                "Business Name": "Fresh Shine Detail LLC",
                "Place Key": "ChIJ-new",
                "City": "Cleveland",
            },
        }

        planned = plan_airtable_sync(
            [record],
            existing_records_by_key={"business_city:freshshinedetailllc|cleveland": "rec-name-city"},
        )

        self.assertEqual(planned[0]["method"], "PATCH")
        self.assertEqual(planned[0]["record_id"], "rec-name-city")
        self.assertEqual(planned[0]["match_reason"], "business_city")

    def test_airtable_ambiguous_existing_fallback_match_skips(self) -> None:
        record = {
            "upsert_key": "place:ChIJ-new",
            "place_id_or_unique_key": "ChIJ-new",
            "phone": "(440) 555-0101",
            "airtable_fields": {
                "Business Name": "Fresh Shine Detail",
                "Place Key": "ChIJ-new",
                "Phone": "(440) 555-0101",
                "Website URL": "freshshinedetail.com",
            },
        }

        planned = plan_airtable_sync(
            [record],
            existing_records_by_key={"phone:4405550101": ["rec-one", "rec-two"]},
        )

        self.assertEqual(planned[0]["method"], "SKIP")
        self.assertEqual(planned[0]["record_id"], "")
        self.assertEqual(planned[0]["match_reason"], "phone")
        self.assertIn("Ambiguous Airtable existing match", planned[0]["warning"])

    def test_airtable_true_new_record_posts(self) -> None:
        record = {
            "upsert_key": "place:ChIJ-new",
            "place_id_or_unique_key": "ChIJ-new",
            "phone": "(440) 555-0101",
            "airtable_fields": {
                "Business Name": "Fresh Shine Detail",
                "Place Key": "ChIJ-new",
                "Phone": "(440) 555-0101",
                "Website URL": "freshshinedetail.com",
                "City": "Cleveland",
            },
        }

        planned = plan_airtable_sync([record], existing_records_by_key={})

        self.assertEqual(planned[0]["method"], "POST")
        self.assertEqual(planned[0]["record_id"], "")
        self.assertNotIn("match_reason", planned[0])

    def test_airtable_dry_run_fetches_existing_records_when_env_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            self._write_airtable_csv(csv_path, [self._airtable_csv_row()])

            with (
                patch.dict(
                    os.environ,
                    {
                        "AIRTABLE_BASE_ID": "base123",
                        "AIRTABLE_PROSPECTS_TABLE": "Prospects",
                        "AIRTABLE_PAT": "token123",
                    },
                    clear=False,
                ),
                patch("upload_airtable.load_dotenv", return_value=False),
                patch("upload_airtable.requests.get") as get,
                patch("upload_airtable.requests.post") as post,
                patch("upload_airtable.requests.patch") as patch_request,
            ):
                get.return_value.status_code = 200
                get.return_value.json.return_value = {
                    "records": [
                        {
                            "id": "rec-phone",
                            "fields": {
                                "Business Name": "A+ Landscaping LLC",
                                "Phone": "(440) 637-5848",
                            },
                        }
                    ]
                }
                code, stdout, stderr = self._run_upload(["upload_airtable.py", "--input", str(csv_path), "--dry-run"])

        self.assertEqual(code, 0, stderr)
        self.assertFalse(post.called)
        self.assertFalse(patch_request.called)
        planned = self._dry_run_plan(stdout)
        self.assertEqual(planned[0]["method"], "PATCH")
        self.assertEqual(planned[0]["record_id"], "rec-phone")
        self.assertEqual(planned[0]["match_reason"], "phone")
        self.assertIn("[MATCH] place:ChIJ123 -> PATCH rec-phone via phone", stderr)

    def test_airtable_live_mode_requires_env_vars_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            self._write_airtable_csv(csv_path, [self._airtable_csv_row()])

            with (
                self._without_airtable_env(),
                patch("upload_airtable.requests.get") as get,
                patch("upload_airtable.requests.post") as post,
                patch("upload_airtable.requests.patch") as patch_request,
                patch("upload_airtable.validate_airtable_fields") as validate_fields,
            ):
                code, _stdout, stderr = self._run_upload(["upload_airtable.py", "--input", str(csv_path), "--live"])

        self.assertEqual(code, 1)
        get.assert_not_called()
        post.assert_not_called()
        patch_request.assert_not_called()
        validate_fields.assert_not_called()
        self.assertIn("AIRTABLE_BASE_ID", stderr)
        self.assertIn("AIRTABLE_PROSPECTS_TABLE", stderr)
        self.assertIn("AIRTABLE_PAT", stderr)

    def test_airtable_dry_run_does_not_require_env_vars_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            self._write_airtable_csv(csv_path, [self._airtable_csv_row()])

            with (
                self._without_airtable_env(),
                patch("upload_airtable.requests.get") as get,
                patch("upload_airtable.requests.post") as post,
                patch("upload_airtable.requests.patch") as patch_request,
                patch("upload_airtable.validate_airtable_fields") as validate_fields,
            ):
                code, stdout, stderr = self._run_upload(["upload_airtable.py", "--input", str(csv_path)])

        self.assertEqual(code, 0, stderr)
        get.assert_not_called()
        post.assert_not_called()
        patch_request.assert_not_called()
        validate_fields.assert_not_called()
        planned = self._dry_run_plan(stdout)
        self.assertEqual(len(planned), 1)

    def test_normalizes_google_places_row_and_dedupe_keys_in_precedence_order(self) -> None:
        row = {
            "business_name": "  A+ Landscaping LLC  ",
            "category": "general_contractor",
            "city": "Kirtland",
            "address": "7955 Euclid Chardon Rd, Kirtland, OH 44094, USA",
            "phone": "(440) 637-5848",
            "website_url": "https://www.example.com/services",
            "google_maps_url": "https://maps.google.com/?cid=123",
            "rating": "4.9",
            "reviews": "130",
            "place_id": "ChIJ123",
            "source_query": "hardscaping contractor Cleveland OH",
        }

        lead = normalize_lead(row, fallback_vertical="hardscaping", source_batch="round1")

        self.assertEqual(lead.business_name, "A+ Landscaping LLC")
        self.assertEqual(lead.vertical, "hardscaping")
        self.assertEqual(lead.website, "https://www.example.com/services")
        self.assertEqual(lead.google_rating, 4.9)
        self.assertEqual(lead.google_review_count, 130)
        self.assertEqual(lead.source_batch, "round1")
        self.assertEqual(lead.place_id_or_unique_key, "ChIJ123")
        self.assertEqual(
            row_dedupe_keys(lead),
            [
                "place:ChIJ123",
                "phone:4406375848",
                "domain:example.com",
                "name_city:a-landscaping-llc:kirtland",
            ],
        )

    def test_hard_icp_gate_skips_franchise_but_keeps_strong_no_site_lead(self) -> None:
        franchise = normalize_lead(
            {
                "business_name": "Mr Roof Cleveland",
                "city": "Cleveland",
                "rating": "4.8",
                "reviews": "120",
                "website_url": "https://example.com",
            },
            fallback_vertical="roofing",
            source_batch="round1",
        )
        no_site = normalize_lead(
            {
                "business_name": "Ron & Sons Landscaping",
                "city": "East Cleveland",
                "rating": "4.9",
                "reviews": "19",
                "website_url": "",
            },
            fallback_vertical="hardscaping",
            source_batch="round1",
        )

        self.assertTrue(hard_icp_gate(franchise).should_skip)
        self.assertIn("franchise_or_chain", hard_icp_gate(franchise).reasons)
        self.assertFalse(hard_icp_gate(no_site).should_skip)

    def test_hard_icp_gate_has_a_floor_and_no_ceiling(self) -> None:
        low_review_with_site = normalize_lead(
            {
                "business_name": "Five Star Detailing",
                "city": "Cleveland",
                "rating": "4.9",
                "reviews": "5",
                "website_url": "https://example.com",
            },
            fallback_vertical="detailing",
            source_batch="round1",
        )
        low_review_no_site = normalize_lead(
            {
                "business_name": "No Site Detailing",
                "city": "Cleveland",
                "rating": "4.9",
                "reviews": "5",
                "website_url": "",
            },
            fallback_vertical="detailing",
            source_batch="round1",
        )
        mature_garage_doors = normalize_lead(
            {
                "business_name": "Lake County Garage Door Repair",
                "city": "Cleveland",
                "rating": "4.9",
                "reviews": "200",
                "website_url": "https://example.com",
                "source_query": "garage door repair Cleveland OH",
            },
            fallback_vertical="garage_doors",
            source_batch="round1",
        )
        mature_detailing = normalize_lead(
            {
                "business_name": "Cleveland Ceramic Coating",
                "city": "Cleveland",
                "rating": "4.9",
                "reviews": "200",
                "website_url": "https://example.com",
            },
            fallback_vertical="detailing",
            source_batch="round1",
        )

        with_site_gate = hard_icp_gate(low_review_with_site)
        self.assertTrue(with_site_gate.should_skip)
        self.assertIn("too_few_reviews", with_site_gate.reasons)
        self.assertFalse(hard_icp_gate(low_review_no_site).should_skip)
        self.assertFalse(hard_icp_gate(mature_garage_doors).should_skip)

        # detailing used to skip this one at max_reviews=130. There is no upper
        # bound any more, in any vertical.
        self.assertFalse(hard_icp_gate(mature_detailing).should_skip)

    def test_hard_icp_gate_admits_the_review_count_that_booked_a_call(self) -> None:
        # 2026-07-28: the only booked call to date came from a 222-review
        # garage-door business pitched on missed-call text-back. Under the old
        # detailing ceiling of 130 a lead like this was skipped as
        # `high_review_count`, so the one profile that has ever converted was the
        # profile the gate threw away. Reviews are demand, not disqualification.
        booked = normalize_lead(
            {
                "business_name": "Northcoast Overhead Door",
                "city": "Cleveland",
                "rating": "4.9",
                "reviews": "222",
                "website_url": "https://example.com",
                "source_query": "garage door repair Cleveland OH",
            },
            fallback_vertical="garage_doors",
            source_batch="round1",
        )
        detailing_twin = normalize_lead(
            {
                "business_name": "Cleveland Ceramic Coating",
                "city": "Cleveland",
                "rating": "4.9",
                "reviews": "222",
                "website_url": "https://example.com",
            },
            fallback_vertical="detailing",
            source_batch="round1",
        )

        for lead in (booked, detailing_twin):
            with self.subTest(vertical=lead.vertical):
                gate = hard_icp_gate(lead)
                self.assertFalse(gate.should_skip)
                self.assertNotIn("high_review_count", gate.reasons)

    def test_the_review_floor_bites_at_seven_and_admits_at_eight(self) -> None:
        # One floor, 8, for every vertical — plumbing included, which sat at 5.
        for vertical in sorted(VERTICAL_PROFILES):
            for reviews, expected_skip in ((7, True), (8, False)):
                with self.subTest(vertical=vertical, reviews=reviews):
                    lead = normalize_lead(
                        {
                            "business_name": "Buckeye Service Co",
                            "city": "Cleveland",
                            "rating": "4.9",
                            "reviews": str(reviews),
                            "website_url": "https://example.com",
                        },
                        fallback_vertical=vertical,
                        source_batch="round1",
                    )
                    gate = hard_icp_gate(lead)
                    self.assertEqual("too_few_reviews" in gate.reasons, expected_skip)

    def test_airtable_vertical_select_accepts_hyphenated_slugs(self) -> None:
        from lead_machine.airtable_sync import normalize_airtable_value, resolve_vertical_label

        for value, expected in (
            ("garage-doors", "garage_doors"),
            ("water-restoration", "water_restoration"),
        ):
            self.assertEqual(normalize_airtable_value("Vertical", value), expected)

        self.assertEqual(resolve_vertical_label("water-restoration"), "water_restoration")

    def test_manual_score_bands_reviews_with_vertical_thresholds(self) -> None:
        enriched = {
            "google": {
                "business_name": "Shine Auto Spa",
                "city": "Cleveland",
                "phone": "(216) 555-0100",
                "website_url": "https://example.com",
                "rating": "4.8",
                "reviews": "100",
                "place_id": "ChIJ999",
            },
            "evidence": {
                "site_status": "okay",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.8 stars / 100 reviews"},
                ],
            },
        }
        raw_score = self._manual_score(
            total_score=75,
            grade="B",
            evidence_refs=["E1"],
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong reviews.", "evidence_refs": ["E1"]}
            ],
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="detailing")

        # 100 reviews is ideal_growth under detailing thresholds (15-125) but
        # established under default thresholds (76+): the manual path must band
        # with the lead's vertical like the API path does.
        self.assertEqual(row["Review Demand Band"], "ideal_growth")
        self.assertEqual(row["Review Fit Band"], "ideal_growth")

    def test_prompts_treat_review_velocity_as_traction_not_money_intent(self) -> None:
        from score_leads import build_api_llm_scoring_prompt, build_llm_scoring_record

        rule = (
            "Live review velocity is traction evidence: it shows the business is active and "
            "supports demand/reputation fit, but it does not count as money intent."
        )

        compact = build_llm_scoring_record(self._huge_detailing_record(), fallback_vertical="detailing")
        self.assertIn(rule, build_api_llm_scoring_prompt([compact], batch_id="batch_001"))
        self.assertIn(rule, build_batch_prompt([], batch_id="batch_test"))
        self.assertIn(
            rule,
            build_llm_prompt({"google": self._google_fixture()}, fallback_vertical="detailing"),
        )

    def test_evidence_profile_exposes_required_fields_and_evidence_items(self) -> None:
        enriched = {
            "google": {
                "business_name": "A+ Landscaping LLC",
                "phone": "(440) 637-5848",
                "rating": "4.9",
                "reviews": "130",
                "website_url": "https://clienthub.getjobber.com/public/request",
            },
            "website": {
                "site_status": "Loads",
                "site_issue_flags": ["non_standard_site_or_booking_url", "no_email_detected"],
                "final_url": "https://clienthub.getjobber.com/public/request",
                "homepage": {"url": "https://clienthub.getjobber.com/public/request", "text": "Client Hub"},
                "pages": {},
                "emails_found": [],
                "phones_found": [],
                "social_links": [],
                "forms_found": True,
                "cta_terms": [],
                "high_ticket_terms": [],
                "combined_text": "Client Hub",
            },
            "screenshots": {"desktop": "screenshots/a-desktop.png", "mobile": "screenshots/a-mobile.png"},
            "derived": {"phone_consistency": "google_only", "non_standard_site_or_booking": True},
        }

        profile = build_evidence_profile(enriched)

        self.assertEqual(profile["site_status"], "jobber_only")
        self.assertEqual(profile["mobile_status"], "unknown")
        self.assertTrue(profile["has_contact_form"])
        self.assertFalse(profile["email_visible"])
        self.assertIn("Jobber", profile["third_party_tools"])
        self.assertGreaterEqual(len(profile["evidence_items"]), 3)
        self.assertTrue(any(item["field"] == "third_party_tools" for item in profile["evidence_items"]))

    def test_static_iosue_like_estimate_form_is_classified_strong(self) -> None:
        html = """
        <html>
          <body>
            <h1>Let's Discuss Your Property</h1>
            <p>Contact us to schedule your estimate appointment.</p>
            <form action="/request-estimate" method="post">
              <label>First Name <input name="first_name"></label>
              <label>Last Name <input name="last_name"></label>
              <label>Company <input name="company"></label>
              <label>Email <input type="email" name="email"></label>
              <label>Phone <input type="tel" name="phone"></label>
              <label>Property Address <input name="address"></label>
              <label>Service Details <textarea name="service_details"></textarea></label>
              <label>Upload project photos <input type="file" name="images"></label>
              <div class="g-recaptcha" data-sitekey="abc123"></div>
              <button type="submit">Submit Request</button>
            </form>
            <a href="https://facebook.com/example">Facebook</a>
          </body>
        </html>
        """

        signals = detect_form_signals(html, page_url="https://example.com/contact")

        self.assertEqual(signals["contact_form_detected"], "Yes")
        self.assertEqual(signals["estimate_form_detected"], "Yes")
        self.assertEqual(signals["form_strength"], "Strong")
        self.assertEqual(signals["form_detection_method"], "static_html")
        self.assertIn("address", signals["form_fields"])
        self.assertIn("details", signals["form_fields"])
        self.assertTrue(signals["has_recaptcha"])

    def test_iframe_provider_form_is_detected(self) -> None:
        html = """
        <html><body>
          <h1>Request a Free Estimate</h1>
          <iframe src="https://form.jotform.com/123456789"></iframe>
          <p>This Jotform request form is protected by captcha.</p>
        </body></html>
        """

        signals = detect_form_signals(html, page_url="https://example.com/free-estimate")

        self.assertEqual(signals["contact_form_detected"], "Yes")
        self.assertEqual(signals["estimate_form_detected"], "Yes")
        self.assertEqual(signals["form_detection_method"], "embedded_provider")
        self.assertGreaterEqual(signals["form_strength_rank"], 1)
        self.assertIn("jotform", signals["provider_terms"])

    def test_scrape_site_checks_candidate_estimate_contact_pages(self) -> None:
        homepage = """
        <html><body>
          <a href="/services">Services</a>
          <p>Schedule an Estimate for patios and retaining walls.</p>
        </body></html>
        """
        estimate_page = """
        <html><body>
          <h1>Let's Discuss Your Property</h1>
          <p>Complete the request form below to schedule your estimate appointment.</p>
          <form>
            <input name="first_name">
            <input name="last_name">
            <input type="email" name="email">
            <input type="tel" name="phone">
            <input name="address">
            <textarea name="service_details"></textarea>
            <input type="file" name="image_upload">
            <div class="recaptcha"></div>
            <button type="submit">Submit</button>
          </form>
        </body></html>
        """

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            if url == "https://example.com":
                return {"ok": True, "status_code": 200, "final_url": url, "html": homepage, "error": ""}
            if url == "https://example.com/request-estimate":
                return {"ok": True, "status_code": 200, "final_url": url, "html": estimate_page, "error": ""}
            return {"ok": False, "status_code": 404, "final_url": url, "html": "", "error": ""}

        with patch("enrich_leads.fetch_url", side_effect=fake_fetch):
            site = scrape_site("https://example.com")

        self.assertEqual(site["contact_form_detected"], "Yes")
        self.assertEqual(site["estimate_form_detected"], "Yes")
        self.assertEqual(site["form_strength"], "Strong")
        self.assertEqual(site["form_detection_method"], "candidate_page")
        self.assertNotIn("no_form_detected", site["site_issue_flags"])
        self.assertIn("estimate", site["pages"])
        snapshots = site["verification_snapshots"]
        self.assertEqual(snapshots[0]["page_role"], "homepage")
        self.assertEqual(snapshots[0]["response_status"], 200)
        self.assertEqual(snapshots[0]["render_outcome"], "loaded")
        estimate_snapshot = next(value for value in snapshots if value["requested_url"].endswith("request-estimate"))
        self.assertEqual(estimate_snapshot["form_count"], 1)
        self.assertGreaterEqual(estimate_snapshot["control_count"], 8)
        self.assertEqual(estimate_snapshot["render_outcome"], "loaded")

    def test_scrape_site_stops_deeper_probing_after_per_lead_ceiling(self) -> None:
        homepage = """
        <html><body>
          <a href="/request-estimate">Request Estimate</a>
          <p>Paint correction ceramic coating request estimate gallery reviews.</p>
        </body></html>
        """
        clock_values = [0.0, 46.0]
        calls = []

        def fake_monotonic() -> float:
            if len(clock_values) > 1:
                return clock_values.pop(0)
            return clock_values[0]

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            calls.append((url, timeout))
            return {"ok": True, "status_code": 200, "final_url": url, "html": homepage, "error": ""}

        with patch("enrich_leads.fetch_url", side_effect=fake_fetch), patch(
            "enrich_leads.time.monotonic", side_effect=fake_monotonic
        ):
            site = scrape_site("https://example.com")

        self.assertEqual(calls, [("https://example.com", 12)])
        self.assertEqual(site["pages"], {})
        self.assertIn("per_lead_enrichment_timeout", site["site_issue_flags"])
        self.assertIn("rendered_check_needed", site["site_issue_flags"])
        self.assertEqual(site["visual_review_needed"], "Yes")
        self.assertGreaterEqual(site["enrichment_elapsed_seconds"], 45)

    def test_candidate_page_fetch_uses_short_timeout(self) -> None:
        homepage = """
        <html><body>
          <a href="/request-estimate">Request Estimate</a>
          <p>Paint correction ceramic coating request estimate gallery reviews.</p>
        </body></html>
        """

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            if url == "https://example.com":
                return {"ok": True, "status_code": 200, "final_url": url, "html": homepage, "error": ""}
            return {"ok": False, "status_code": 404, "final_url": url, "html": "", "error": ""}

        with patch("enrich_leads.fetch_url", side_effect=fake_fetch) as fetch:
            scrape_site("https://example.com")

        self.assertEqual(fetch.call_args_list[0].kwargs.get("timeout"), 12)
        self.assertEqual(fetch.call_args_list[1].kwargs.get("timeout"), 8)

    def test_strong_estimate_form_blocks_no_pain_estimate_funnel_sendability(self) -> None:
        enriched = {
            "google": self._google_fixture(),
            "evidence": {
                "site_status": "okay",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "form_detection_method": "candidate_page",
                "has_contact_form": True,
                "has_estimate_cta": True,
                "raw_flags": [],
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 130 reviews"},
                    {"id": "E2", "field": "estimate_form_detected", "value": "Yes"},
                    {"id": "E3", "field": "form_strength", "value": "Strong"},
                ],
            },
        }
        raw_score = self._manual_score(
            subscores={
                "icp_fit": 18,
                "reputation_demand_proof": 14,
                "web_conversion_pain": 22,
                "mockup_leverage": 12,
                "reachability": 8,
                "offer_angle_clarity": 9,
                "evidence_confidence": 5,
            },
            total_score=88,
            grade="A",
            evidence_refs=["E1", "E2", "E3"],
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong reviews.", "evidence_refs": ["E1"]},
                {"claim": "Your site already has a strong estimate form.", "evidence_refs": ["E2", "E3"]},
            ],
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Lead Priority"], "A")
        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Missing concrete conversion issue", row["Send Blockers"])
        self.assertEqual(row["Packet Eligible"], "")
        self.assertIn("Blocked by sendability", row["Packet Eligibility Reason"])
        self.assertNotIn("missing contact form", row["Safe Outreach Claims"].lower())
        self.assertNotIn("no contact form", row["Safe Outreach Claims"].lower())

    def test_strong_estimate_form_with_premium_perception_gap_can_be_sendable(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "reviews": "18",
            },
            "evidence": {
                "site_status": "okay",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "form_detection_method": "candidate_page",
                "cta_strength": "Strong",
                "visible_pain_score": 36,
                "visible_pain_reasons": ["Premium hardscape work is not reflected by the owned website"],
                "visible_pain_categories": ["premium_perception_gap"],
                "send_trigger": "Premium hardscape work is not reflected by the owned website",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 18 reviews"},
                    {"id": "E2", "field": "form_strength", "value": "Strong"},
                    {"id": "E3", "field": "visible_pain_categories", "value": "premium_perception_gap"},
                    {
                        "id": "E4",
                        "field": "send_trigger",
                        "value": "Premium hardscape work is not reflected by the owned website",
                    },
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Premium Perception Gap",
            evidence_refs=["E1", "E3", "E4"],
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong review proof.", "evidence_refs": ["E1"]},
                {
                    "claim": "The owned website does not reflect the premium hardscape work.",
                    "evidence_refs": ["E3", "E4"],
                },
            ],
            opener=(
                "Your reviews look strong, but the site does not make the premium hardscape work obvious. "
                "Want me to send a cleaner direction?"
            ),
            visible_pain_score=36,
            visible_pain_reasons=["Premium hardscape work is not reflected by the owned website"],
            send_trigger="Premium hardscape work is not reflected by the owned website",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Offer Fit"], "Premium Perception Gap")
        self.assertEqual(row["Sendable"], "Yes")
        self.assertNotIn("Strong estimate form exists", row["Send Blockers"])
        self.assertNotIn("Missing concrete conversion issue", row["Send Blockers"])

    def test_strong_estimate_form_with_weak_project_gallery_can_be_sendable(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "reviews": "22",
            },
            "evidence": {
                "site_status": "okay",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "form_detection_method": "candidate_page",
                "visible_pain_score": 34,
                "visible_pain_reasons": ["Project photos are buried or weak for high-ticket hardscaping"],
                "visible_pain_categories": ["portfolio_presentation_pain"],
                "send_trigger": "Project photos are buried or weak for high-ticket hardscaping",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 22 reviews"},
                    {"id": "E2", "field": "form_strength", "value": "Strong"},
                    {"id": "E3", "field": "visible_pain_categories", "value": "portfolio_presentation_pain"},
                    {
                        "id": "E4",
                        "field": "send_trigger",
                        "value": "Project photos are buried or weak for high-ticket hardscaping",
                    },
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Portfolio Rebuild",
            evidence_refs=["E1", "E3", "E4"],
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong review proof.", "evidence_refs": ["E1"]},
                {"claim": "The project gallery is weak for high-ticket work.", "evidence_refs": ["E3", "E4"]},
            ],
            opener="Your reviews look strong, but the project gallery undersells the hardscape work.",
            visible_pain_score=34,
            visible_pain_reasons=["Project photos are buried or weak for high-ticket hardscaping"],
            send_trigger="Project photos are buried or weak for high-ticket hardscaping",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Offer Fit"], "Portfolio Rebuild")
        self.assertEqual(row["Sendable"], "Yes")
        self.assertNotIn("Strong estimate form exists", row["Send Blockers"])

    def test_strong_estimate_form_cannot_use_missing_form_claim(self) -> None:
        enriched = {
            "google": self._google_fixture(),
            "evidence": {
                "site_status": "okay",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "visible_pain_score": 35,
                "visible_pain_reasons": ["No estimate form detected"],
                "visible_pain_categories": ["estimate_path_pain"],
                "send_trigger": "No estimate form detected",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 130 reviews"},
                    {"id": "E2", "field": "form_strength", "value": "Strong"},
                    {"id": "E3", "field": "send_trigger", "value": "No estimate form detected"},
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Website Cleanup",
            evidence_refs=["E1", "E2", "E3"],
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong reviews.", "evidence_refs": ["E1"]},
                {"claim": "There is no estimate form on the site.", "evidence_refs": ["E3"]},
            ],
            opener="Your reviews are strong, but there is no estimate form on the site.",
            visible_pain_score=35,
            visible_pain_reasons=["No estimate form detected"],
            send_trigger="No estimate form detected",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Strong estimate form conflicts with missing-form claim", row["Send Blockers"])

    def test_estimate_funnel_blocked_when_strong_form_has_only_non_estimate_pain(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "reviews": "18",
            },
            "evidence": {
                "site_status": "okay",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "visible_pain_score": 36,
                "visible_pain_reasons": ["Premium hardscape work is not reflected by the owned website"],
                "visible_pain_categories": ["premium_perception_gap"],
                "send_trigger": "Premium hardscape work is not reflected by the owned website",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 18 reviews"},
                    {"id": "E2", "field": "form_strength", "value": "Strong"},
                    {"id": "E3", "field": "visible_pain_categories", "value": "premium_perception_gap"},
                    {
                        "id": "E4",
                        "field": "send_trigger",
                        "value": "Premium hardscape work is not reflected by the owned website",
                    },
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Estimate Funnel",
            evidence_refs=["E1", "E3", "E4"],
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong review proof.", "evidence_refs": ["E1"]},
                {
                    "claim": "The owned website does not reflect the premium hardscape work.",
                    "evidence_refs": ["E3", "E4"],
                },
            ],
            opener="Your reviews look strong, but the site does not make the premium hardscape work obvious.",
            visible_pain_score=36,
            visible_pain_reasons=["Premium hardscape work is not reflected by the owned website"],
            send_trigger="Premium hardscape work is not reflected by the owned website",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Estimate Funnel blocked by strong estimate form without estimate-path weakness", row["Send Blockers"])

    def test_evidence_profile_derives_visible_pain_fields_and_send_trigger(self) -> None:
        profile = build_evidence_profile(
            {
                "google": self._google_fixture(),
                "website": {
                    "site_status": "Loads",
                    "final_url": "https://example.com",
                    "homepage": {"url": "https://example.com", "text": "Call us for patio estimates."},
                    "site_issue_flags": ["no_form_detected", "no_clear_cta_detected"],
                    "contact_form_detected": "No",
                    "estimate_form_detected": "No",
                    "form_strength": "None",
                    "cta_strength": "Weak",
                    "emails_found": ["office@example.com"],
                    "phones_found": ["(440) 637-5848"],
                },
            }
        )

        self.assertGreater(profile["visible_pain_score"], 0)
        self.assertIn("No estimate form detected", profile["visible_pain_reasons"])
        self.assertEqual(profile["send_trigger"], "No estimate form detected")
        self.assertTrue(any(item["field"] == "send_trigger" for item in profile["evidence_items"]))

    def test_send_trigger_is_the_strongest_leak_not_the_first_rule_to_fire(self) -> None:
        """The trigger is the line the packet leads with and the one
        `sendability` requires, so it has to be the worst problem found. It used
        to be `reasons[0]` — whichever rule the function happened to evaluate
        first, which is an implementation detail of the rule ordering."""
        profile = build_evidence_profile(
            {
                "google": self._google_fixture(),
                "website": {
                    # "Phone number hard to find" (8) is added before the
                    # broken-form rule in source order via the flag loop, and a
                    # weak CTA (14) before it too, but a broken estimate form
                    # (30) is the real reason to call.
                    "site_status": "Loads",
                    "final_url": "https://example.com",
                    "homepage": {"url": "https://example.com", "text": "Patios."},
                    "site_issue_flags": ["no_clear_cta_detected", "broken_form"],
                    "contact_form_detected": "Yes",
                    "estimate_form_detected": "Yes",
                    "form_strength": "Weak",
                    "cta_strength": "Weak",
                    "emails_found": [],
                    "phones_found": [],
                },
            }
        )

        self.assertIn("No clear estimate CTA", profile["visible_pain_reasons"])
        self.assertIn("Estimate form appears broken", profile["visible_pain_reasons"])
        self.assertEqual(profile["send_trigger"], "Estimate form appears broken")

    def test_send_trigger_ties_keep_the_earlier_reason(self) -> None:
        # The status-derived triggers all score 40, so a tie must not reorder
        # them; "No owned website found" stays the headline for a missing site.
        profile = build_evidence_profile(
            {
                "google": self._google_fixture(),
                "website": {
                    "site_status": "No Website",
                    "site_issue_flags": ["http_error"],
                    "emails_found": [],
                    "phones_found": [],
                },
            }
        )

        self.assertEqual(profile["send_trigger"], "No owned website found")

    def test_evidence_profile_classifies_non_form_visible_pain_categories(self) -> None:
        profile = build_evidence_profile(
            {
                "google": self._google_fixture(),
                "website": {
                    "site_status": "Loads",
                    "final_url": "https://example.com",
                    "homepage": {
                        "url": "https://example.com",
                        "text": "Schedule an Estimate hardscape patio retaining wall landscape design",
                    },
                    "site_issue_flags": [
                        "premium_perception_gap",
                        "weak_project_gallery",
                        "low_visible_trust",
                    ],
                    "contact_form_detected": "Yes",
                    "estimate_form_detected": "Yes",
                    "form_strength": "Strong",
                    "cta_strength": "Strong",
                    "emails_found": ["office@example.com"],
                    "phones_found": ["(440) 637-5848"],
                    "high_ticket_terms": ["hardscape", "patio", "retaining wall"],
                },
            }
        )

        self.assertIn("premium_perception_gap", profile["visible_pain_categories"])
        self.assertIn("portfolio_presentation_pain", profile["visible_pain_categories"])
        self.assertIn("trust_proof_pain", profile["visible_pain_categories"])
        self.assertNotIn("No estimate form detected", profile["visible_pain_reasons"])
        self.assertEqual(profile["send_trigger"], "Premium work is undersold by the owned website")

    def test_deterministic_scoring_does_not_make_strong_form_site_a_sendable_estimate_funnel(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "business_name": "Iosue Style Landscape Concepts",
                "rating": "4.7",
                "reviews": "39",
            },
            "website": {
                "site_status": "Loads",
                "site_issue_flags": [],
                "final_url": "https://example.com",
                "homepage": {"url": "https://example.com", "text": "Schedule an Estimate hardscape patio retaining wall"},
                "pages": {},
                "emails_found": ["office@example.com"],
                "phones_found": ["(440) 637-5848"],
                "social_links": ["https://facebook.com/example"],
                "forms_found": True,
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "form_detection_method": "candidate_page",
                "cta_terms": ["schedule", "request estimate"],
                "cta_strength": "Strong",
                "high_ticket_terms": ["hardscape", "patio", "retaining wall", "landscape design"],
                "generic_copy_terms": [],
                "combined_text": "Schedule an Estimate hardscape patio retaining wall landscape design gallery portfolio",
            },
            "derived": {
                "phone_consistency": "matched",
                "has_email": True,
                "has_form": True,
                "has_cta": True,
                "has_social_links": True,
                "has_high_ticket_terms": True,
                "non_standard_site_or_booking": False,
            },
            "screenshots": {},
        }

        row = score_record(enriched, "hardscaping")

        self.assertNotEqual(row["Lead Priority"], "A")
        self.assertNotEqual(row["Offer Fit"], "Estimate Funnel")
        self.assertEqual(row["Sendable"], "No")
        self.assertEqual(row["Packet Eligible"], "")
        self.assertEqual(row["Visible Pain Score"], 0)
        self.assertEqual(row["Send Trigger"], "")
        self.assertNotIn("no form detected", row["Evidence Notes"].lower())

    def test_no_site_broken_and_social_only_can_be_sendable_with_demand_proof(self) -> None:
        for status, trigger in [
            ("No Website", "No owned website found"),
            ("Broken Website", "Website appears broken"),
            ("Social Only", "Social-only web presence"),
        ]:
            with self.subTest(status=status):
                row = score_record(
                    {
                        "google": {
                            **self._google_fixture(),
                            "business_name": f"{status} Hardscapes",
                            "rating": "4.8",
                            "reviews": "18",
                        },
                        "website": {
                            "site_status": status,
                            "site_issue_flags": [],
                            "pages": {},
                            "emails_found": [],
                            "phones_found": [],
                            "forms_found": False,
                            "cta_terms": [],
                            "combined_text": "hardscape patio retaining wall outdoor living",
                            "high_ticket_terms": ["hardscape", "patio", "retaining wall"],
                        },
                        "derived": {"has_high_ticket_terms": True},
                        "screenshots": {},
                        "buyer_intent": self._buyer_intent_fixture(),
                    },
                    "hardscaping",
                )

                self.assertEqual(row["Lead Priority"], "A")
                self.assertEqual(row["Sendable"], "Yes")
                self.assertEqual(row["Packet Eligible"], "Yes")
                self.assertGreaterEqual(int(row["Visible Pain Score"]), 30)
                self.assertEqual(row["Send Trigger"], trigger)

    def test_manual_score_validation_requires_evidence_references_and_merges_card_fields(self) -> None:
        enriched = {
            "google": {
                "business_name": "A+ Landscaping LLC",
                "city": "Kirtland",
                "phone": "(440) 637-5848",
                "website_url": "https://clienthub.getjobber.com/public/request",
                "google_maps_url": "https://maps.google.com/?cid=123",
                "rating": "4.9",
                "reviews": "130",
                "place_id": "ChIJ123",
            },
            "evidence": {
                "site_status": "jobber_only",
                "screenshots": {"desktop": "desktop.png", "mobile": "mobile.png"},
                "evidence_items": [
                    {"id": "ev-google-rating", "field": "google_rating", "value": "4.9 stars / 130 reviews"},
                    {"id": "ev-third-party-jobber", "field": "third_party_tools", "value": "Jobber"},
                ],
            },
        }
        raw_score = {
            "score_schema_version": 2,
            "subscores": {
                "icp_fit": 18,
                "reputation_demand_proof": 14,
                "web_conversion_pain": 24,
                "mockup_leverage": 14,
                "reachability": 7,
                "offer_angle_clarity": 9,
                "evidence_confidence": 4,
            },
            "confidence": "medium",
            "hard_disqualifiers": [],
            "best_angle": "Social/Jobber Escape",
            "commercial_fit_state": "commercially-sendable",
            "commercial_materiality": "The owned conversion path is absent behind the visible Jobber route.",
            "recurring_consequence": "Each premium visitor reaches the same constrained path.",
            "root_cause_confidence": "Fresh structured evidence resolves the visible route through Jobber.",
            "offer_component": "Get the Lead",
            "packageable_solution": "Build an owned estimate path that routes into the existing workflow.",
            "value_mechanism": "Reduce friction on existing demand without replacing the operating system.",
            "beyond_free_fix": "The owned page, trust proof, CTA, and routing are a coordinated system.",
            "self_fix_risk": "The missing owned path is broader than changing the visible Jobber link.",
            "intentionality": "The premium service and strong demand evidence support an owned conversion path.",
            "contact_eligibility": "contact-now",
            "asset_state": "none",
            "promise_state": "asset-free",
            "asset_id": "",
            "asset_verifier_verdict": "",
            "recommended_action": "Build mockup then SMS owner",
            "why_this_lead": ["Strong reviews", "Jobber-only presence"],
            "why_this_might_be_wrong": ["Owner may be referral-driven"],
            "opener": "Want me to send a cleaner estimate-page mockup?",
            "evidence_refs": ["ev-google-rating", "ev-third-party-jobber"],
            "safe_outreach_claims": [
                {"claim": "Your Google profile shows strong review proof.", "evidence_refs": ["ev-google-rating"]},
                {"claim": "Your visible web presence routes through Jobber.", "evidence_refs": ["ev-third-party-jobber"]},
            ],
            "unsafe_claims": ["Do not claim they are losing jobs."],
            "airtable_fields": {"Status": "New"},
        }

        merged = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")
        card = build_lead_card(merged)

        self.assertEqual(merged["Lead Priority"], "A")
        self.assertEqual(merged["AI Confidence"], "medium")
        self.assertEqual(merged["Recommended Action"], "Build mockup then SMS owner")
        self.assertEqual(merged["Send Trigger"], "Jobber-only web presence")
        self.assertGreater(int(merged["Visible Pain Score"]), 0)
        self.assertIn("Jobber-only web presence", merged["Visible Pain Reasons"])
        self.assertEqual(card["grade"], "A")
        self.assertEqual(card["best_angle"], "Social/Jobber Escape")
        self.assertEqual(card["airtable_fields"]["Lead Priority"], "A")
        self.assertEqual(card["airtable_fields"]["Send Trigger"], "Jobber-only web presence")
        self.assertTrue(card["sendable"])
        self.assertTrue(card["minimum_evidence_passed"])
        self.assertEqual(card["evidence_refs"], ["ev-google-rating", "ev-third-party-jobber"])

    def test_manual_score_file_loader_rejects_missing_evidence_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            score_path = Path(tmp) / "score.json"
            score_path.write_text(
                json.dumps(
                    {
                        "subscores": {
                            "icp_fit": 10,
                            "reputation_demand_proof": 10,
                            "web_conversion_pain": 10,
                            "mockup_leverage": 10,
                            "reachability": 10,
                            "offer_angle_clarity": 10,
                            "evidence_confidence": 5,
                        },
                        "confidence": "high",
                        "hard_disqualifiers": [],
                        "best_angle": "Website Rebuild",
                        "recommended_action": "human review",
                        "why_this_lead": ["Looks weak"],
                        "why_this_might_be_wrong": [],
                        "opener": "",
                        "evidence_refs": ["missing-id"],
                        "safe_outreach_claims": [{"claim": "Missing claim.", "evidence_refs": ["missing-id"]}],
                        "unsafe_claims": ["Do not use unsupported claims."],
                        "airtable_fields": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_manual_score(score_path, allowed_evidence_ids={"ev-known"})

    def test_manual_score_normalizes_bounded_model_format_drift(self) -> None:
        score = self._manual_score(
            commercial_fit_state="not-an-lpm-fit",
            contact_eligibility="hold-commercial-case",
            evidence_refs=["ev-cta-strength-16"],
        )

        validated = validate_manual_score(
            score,
            allowed_evidence_ids={"ev-cta_strength-16", "E2"},
        )

        self.assertEqual(validated["commercial_fit_state"], "not-an-LPM-fit")
        self.assertEqual(validated["evidence_refs"], ["ev-cta_strength-16"])

        governed = apply_sendability(
            {
                "Commercial Fit State": validated["commercial_fit_state"],
                "Contact Eligibility": "hold-commercial-case",
                "Asset State": "none",
                "Promise State": "asset-free",
            }
        )
        self.assertNotIn("invalid commercial_fit_state", governed["Sendability Blockers"])

        score["evidence_refs"] = ["invented-ref"]
        with self.assertRaisesRegex(ValueError, "Unknown evidence_refs: invented-ref"):
            validate_manual_score(score, allowed_evidence_ids={"ev-cta_strength-16", "E2"})

    def test_llm_prompt_and_queue_use_manual_web_workflow(self) -> None:
        enriched = {
            "google": {
                "business_name": "A+ Landscaping LLC",
                "place_id": "ChIJ123",
                "vertical": "service",
                "source_query": "hardscaping contractor Cleveland OH",
            },
            "evidence": {
                "site_status": "jobber_only",
                "screenshots": {"desktop": "desktop.png", "mobile": "mobile.png"},
                "evidence_items": [{"id": "ev-google-rating", "field": "google_rating", "value": "4.9 / 130"}],
            },
        }

        prompt = build_llm_prompt(enriched, fallback_vertical="hardscaping")
        self.assertIn("Response must be one valid JSON object only.", prompt)
        self.assertIn("First character must be { and last character must be }.", prompt)
        self.assertIn("No markdown fences.", prompt)
        self.assertIn("No commentary.", prompt)
        self.assertIn("No reasoning outside JSON.", prompt)
        self.assertIn("No \"Thought for X seconds\" text.", prompt)
        self.assertIn("ICP Fit / 20", prompt)
        self.assertIn('"total_score": 0', prompt)
        self.assertIn('"grade": "A|B|C|Skip"', prompt)
        self.assertIn("total_score must equal the sum of all subscores.", prompt)
        self.assertIn("A = 80-100", prompt)
        self.assertIn("confidence must be one of low, medium, high.", prompt)
        self.assertIn(
            "Missing site plus strong reviews can be an A lead only when there is a concrete conversion gap and enough evidence.",
            prompt,
        )
        self.assertIn("No specific observation = no send.", prompt)
        self.assertIn("Do not score A/sendable just because the business is reputable.", prompt)
        self.assertIn(
            "mature plus an obvious broken/missing estimate path can still be high value when backed by money intent or very strong conversion-pain evidence.",
            prompt,
        )
        self.assertIn("A strong estimate form does not globally disqualify a lead.", prompt)
        self.assertIn("It only blocks missing-form, no-contact-path, and no-estimate-path claims.", prompt)
        self.assertIn("Do not use Estimate Funnel if the form is strong unless the actual estimate path is still visibly weak.", prompt)
        self.assertIn("Static fetch failure alone does not prove the website is broken.", prompt)
        self.assertIn("If rendered_site_status is Loaded, do not claim the website is broken.", prompt)
        self.assertIn("No screenshot/rendered evidence means no strong visual claims.", prompt)
        self.assertIn("Premium Perception Gap", prompt)
        self.assertIn("Portfolio Rebuild", prompt)
        self.assertIn("Mobile Quote Path Cleanup", prompt)
        self.assertIn('"send_trigger": ""', prompt)
        self.assertIn('"visible_pain_score": 0', prompt)
        self.assertIn('"visible_pain_reasons": []', prompt)
        self.assertIn('"safe_outreach_claims"', prompt)
        self.assertIn('"unsafe_claims"', prompt)
        self.assertIn("Build/send a no-site launch mockup", prompt)
        self.assertIn("Must mention at least one specific evidence-backed observation.", prompt)
        self.assertIn('"vertical": "hardscaping"', prompt)
        self.assertIn('"review_demand_band":', prompt)
        self.assertIn("ev-google-rating", prompt)

        with tempfile.TemporaryDirectory() as tmp:
            paths = write_review_queue([enriched], Path(tmp), fallback_vertical="hardscaping")
            self.assertEqual(len(paths), 1)
            self.assertTrue(paths[0].read_text(encoding="utf-8").startswith("# Manual LLM Review"))

    def test_llm_prompt_infers_vertical_when_cli_vertical_is_generic(self) -> None:
        enriched = {
            "google": {
                "business_name": "Barski Hardscaping",
                "place_id": "ChIJ456",
                "vertical": "service",
                "category": "landscaper",
                "source_query": "patio paver contractor Cleveland OH",
            },
            "evidence": {"evidence_items": [{"id": "E1", "field": "google_rating", "value": "4.9 / 24"}]},
        }

        prompt = build_llm_prompt(enriched, fallback_vertical="service")

        self.assertIn('"vertical": "hardscaping"', prompt)

    def test_llm_batch_prompt_contains_multiple_leads_and_score_files(self) -> None:
        first = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
            "website": {"combined_text": "Homepage text for A+ Landscaping."},
        }
        second = {
            "google": {
                **self._google_fixture(),
                "business_name": "Barski Hardscaping",
                "phone": "(440) 555-0101",
                "place_id": "ChIJ456",
            },
            "evidence": self._evidence_fixture(site_status="jobber_only"),
            "website": {"combined_text": "Homepage text for Barski Hardscaping."},
        }

        with tempfile.TemporaryDirectory() as tmp:
            batch_paths = write_llm_batches(
                [first, second],
                Path(tmp),
                fallback_vertical="hardscaping",
                batch_size=5,
            )
            prompt = batch_paths[0].prompt_path.read_text(encoding="utf-8")
            expected_files = batch_paths[0].expected_files_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(batch_paths), 1)
        self.assertIn("Response must be one valid JSON object only.", prompt)
        self.assertIn('"batch_id": "batch_001"', prompt)
        self.assertIn('"scores"', prompt)
        self.assertIn('"score_file": "chij123.json"', prompt)
        self.assertIn('"score_file": "chij456.json"', prompt)
        self.assertIn("A+ Landscaping LLC", prompt)
        self.assertIn("Barski Hardscaping", prompt)
        self.assertIn("ICP Fit / 20", prompt)
        self.assertIn(
            "Missing site plus strong reviews can be an A lead only when there is a concrete conversion gap and enough evidence.",
            prompt,
        )
        self.assertIn("No specific observation = no send.", prompt)
        self.assertIn("Do not score A/sendable just because the business is reputable.", prompt)
        self.assertIn(
            "mature plus an obvious broken/missing estimate path can still be high value when backed by money intent or very strong conversion-pain evidence.",
            prompt,
        )
        self.assertIn("A strong estimate form does not globally disqualify a lead.", prompt)
        self.assertIn("Static fetch failure alone does not prove the website is broken.", prompt)
        self.assertIn("If rendered_site_status is Loaded, do not claim the website is broken.", prompt)
        self.assertIn("No screenshot/rendered evidence means no strong visual claims.", prompt)
        self.assertIn("Premium Perception Gap", prompt)
        self.assertIn("Portfolio Rebuild", prompt)
        self.assertIn('"send_trigger": ""', prompt)
        self.assertEqual(expected_files, ["chij123.json", "chij456.json"])

    def test_llm_batch_omits_raw_verification_snapshots_but_keeps_evidence_items(self) -> None:
        evidence = self._evidence_fixture(site_status="weak")
        evidence["verification_snapshot"] = {
            "visible_text": "RAW-SNAPSHOT-TEXT" * 100,
            "visible_links": ["https://example.com/raw-snapshot-link"],
        }
        evidence["verification_snapshots"] = [
            {"visible_text": "RAW-SNAPSHOTS-TEXT" * 100}
        ]
        enriched = {
            "google": self._google_fixture(),
            "evidence": evidence,
            "website": {"combined_text": "Bounded website text."},
        }

        with tempfile.TemporaryDirectory() as tmp:
            batch_paths = write_llm_batches(
                [enriched],
                Path(tmp),
                fallback_vertical="hardscaping",
                batch_size=1,
            )
            prompt = batch_paths[0].prompt_path.read_text(encoding="utf-8")

        self.assertNotIn("RAW-SNAPSHOT-TEXT", prompt)
        self.assertNotIn("RAW-SNAPSHOTS-TEXT", prompt)
        self.assertNotIn("raw-snapshot-link", prompt)
        self.assertIn('"evidence_items"', prompt)
        self.assertIn('"website_text": "Bounded website text."', prompt)

    def test_llm_batch_only_candidates_ranks_visible_pain_before_high_review_decent_site(self) -> None:
        high_review_decent = {
            "google": {
                **self._google_fixture(),
                "business_name": "Polished High Review Hardscapes",
                "reviews": "85",
                "place_id": "ChIJHIGH",
            },
            "website": {
                "site_status": "Loads",
                "site_issue_flags": [],
                "combined_text": "hardscape patio retaining wall request an estimate gallery reviews",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "form_detection_method": "static_html",
                "cta_strength": "Strong",
                "forms_found": True,
                "emails_found": ["office@example.com"],
                "phones_found": ["(440) 637-5848"],
                "high_ticket_terms": ["hardscape", "patio"],
            },
            "derived": {"has_high_ticket_terms": True},
        }
        visible_pain = {
            "google": {
                **self._google_fixture(),
                "business_name": "Old School Patio Co",
                "reviews": "12",
                "place_id": "ChIJPAIN",
            },
            "website": {
                "site_status": "No Website",
                "site_issue_flags": [],
                "combined_text": "hardscape patio retaining wall",
                "high_ticket_terms": ["hardscape", "patio"],
            },
            "derived": {"has_high_ticket_terms": True},
        }

        with tempfile.TemporaryDirectory() as tmp:
            batch_paths = write_llm_batches(
                [high_review_decent, visible_pain],
                Path(tmp),
                fallback_vertical="hardscaping",
                batch_size=2,
                only_candidates=True,
            )
            expected_files = batch_paths[0].expected_files_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(expected_files, ["chijpain.json", "chijhigh.json"])

    def test_llm_batch_ranking_does_not_penalize_strong_form_when_non_form_pain_exists(self) -> None:
        weak_form_estimate_pain = {
            "google": {
                **self._google_fixture(),
                "business_name": "Weak Form Patio Co",
                "reviews": "12",
                "place_id": "ChIJWEAKFORM",
            },
            "website": {
                "site_status": "Loads",
                "site_issue_flags": ["no_form_detected", "no_clear_cta_detected"],
                "combined_text": "hardscape patio retaining wall",
                "contact_form_detected": "No",
                "estimate_form_detected": "No",
                "form_strength": "None",
                "cta_strength": "Weak",
                "emails_found": ["office@example.com"],
                "phones_found": ["(440) 637-5848"],
                "high_ticket_terms": ["hardscape", "patio"],
            },
            "derived": {"has_high_ticket_terms": True},
        }
        strong_form_premium_pain = {
            "google": {
                **self._google_fixture(),
                "business_name": "Premium Patio Co",
                "reviews": "12",
                "place_id": "ChIJPREMIUM",
            },
            "website": {
                "site_status": "Loads",
                "site_issue_flags": ["premium_perception_gap"],
                "combined_text": "request an estimate hardscape patio retaining wall outdoor living",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "form_detection_method": "candidate_page",
                "cta_strength": "Strong",
                "forms_found": True,
                "emails_found": ["office@example.com"],
                "phones_found": ["(440) 637-5848"],
                "high_ticket_terms": ["hardscape", "patio", "retaining wall"],
            },
            "derived": {"has_high_ticket_terms": True},
        }

        with tempfile.TemporaryDirectory() as tmp:
            batch_paths = write_llm_batches(
                [weak_form_estimate_pain, strong_form_premium_pain],
                Path(tmp),
                fallback_vertical="hardscaping",
                batch_size=2,
                only_candidates=True,
            )
            expected_files = batch_paths[0].expected_files_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(expected_files, ["chijpremium.json", "chijweakform.json"])

    def test_llm_score_bundle_importer_writes_individual_score_json_files(self) -> None:
        score_one = self._manual_score(total_score=75, grade="B")
        score_two = self._manual_score(
            total_score=86,
            grade="A",
            subscores={
                "icp_fit": 18,
                "reputation_demand_proof": 14,
                "web_conversion_pain": 22,
                "mockup_leverage": 12,
                "reachability": 8,
                "offer_angle_clarity": 8,
                "evidence_confidence": 4,
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_path = tmp_path / "bundle.json"
            scores_dir = tmp_path / "scores"
            bundle_path.write_text(
                json.dumps(
                    {
                        "batch_id": "batch_001",
                        "scores": [
                            {"score_file": "chij123.json", "score": score_one},
                            {"score_file": "chij456.json", "score": score_two},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = import_score_bundle(bundle_path, scores_dir)

            first = json.loads((scores_dir / "chij123.json").read_text(encoding="utf-8"))
            second = json.loads((scores_dir / "chij456.json").read_text(encoding="utf-8"))

        self.assertEqual(result.scores_imported, 2)
        self.assertEqual(result.files_written, 2)
        self.assertEqual(result.validation_failures, [])
        self.assertEqual(first["grade"], "B")
        self.assertEqual(first["total_score"], 75)
        self.assertEqual(second["grade"], "A")
        self.assertEqual(second["total_score"], 86)

    def test_llm_score_bundle_importer_rejects_invalid_json_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_path = tmp_path / "bundle.json"
            scores_dir = tmp_path / "scores"
            bundle_path.write_text("{bad json", encoding="utf-8")

            with self.assertRaises(ValueError) as caught:
                import_score_bundle(bundle_path, scores_dir)

        self.assertIn("Invalid JSON bundle", str(caught.exception))
        self.assertFalse(scores_dir.exists())

    def test_llm_score_bundle_importer_rejects_empty_scores_array(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_path = tmp_path / "bundle.json"
            scores_dir = tmp_path / "scores"
            bundle_path.write_text(json.dumps({"batch_id": "batch_001", "scores": []}), encoding="utf-8")

            with self.assertRaises(ValueError) as caught:
                import_score_bundle(bundle_path, scores_dir)

        self.assertIn("scores array must contain at least one score", str(caught.exception))
        self.assertFalse(scores_dir.exists())

    def test_llm_score_bundle_importer_rejects_score_without_safe_outreach_claims(self) -> None:
        score = self._manual_score(total_score=75, grade="B")
        score.pop("safe_outreach_claims")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_path = tmp_path / "bundle.json"
            scores_dir = tmp_path / "scores"
            bundle_path.write_text(
                json.dumps({"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": score}]}),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as caught:
                import_score_bundle(bundle_path, scores_dir)

        self.assertIn("safe_outreach_claims", str(caught.exception))
        self.assertFalse((scores_dir / "chij123.json").exists())

    def test_llm_score_bundle_importer_rejects_score_file_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_path = tmp_path / "bundle.json"
            scores_dir = tmp_path / "scores"
            bundle_path.write_text(
                json.dumps(
                    {
                        "batch_id": "batch_001",
                        "scores": [
                            {"score_file": "../../bad.json", "score": self._manual_score(total_score=75, grade="B")}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as caught:
                import_score_bundle(bundle_path, scores_dir)

        self.assertIn("unsafe score_file", str(caught.exception))
        self.assertFalse((scores_dir / "bad.json").exists())

    def test_llm_client_posts_anthropic_message_and_parses_score_bundle(self) -> None:
        from lead_machine.llm_client import score_batch

        score = self._manual_score(total_score=75, grade="B")
        response = {
            "content": [{"type": "text", "text": json.dumps({"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": score}]})}],
            "usage": {"input_tokens": 123, "output_tokens": 45},
        }

        class FakeResponse:
            status_code = 200
            text = json.dumps(response)

            def json(self) -> dict:
                return response

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "LLM_SCORING_PROVIDER_CHAIN": "anthropic"}, clear=False):
            with patch("lead_machine.llm_client._post", return_value=FakeResponse()) as post:
                result = score_batch("score this batch", model="claude-sonnet-5", max_tokens=4096)

        self.assertEqual(result["bundle"]["batch_id"], "batch_001")
        self.assertEqual(result["bundle"]["scores"][0]["score_file"], "chij123.json")
        self.assertEqual(result["usage"]["input_tokens"], 123)
        self.assertEqual(result["usage"]["output_tokens"], 45)
        self.assertEqual(result["usage"]["total_tokens"], 168)
        _, kwargs = post.call_args
        self.assertEqual(kwargs["headers"]["x-api-key"], "test-key")
        self.assertEqual(kwargs["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(kwargs["json"]["model"], "claude-sonnet-5")
        self.assertEqual(kwargs["json"]["max_tokens"], 4096)
        self.assertEqual(kwargs["json"]["messages"][0]["content"], "score this batch")

    def test_llm_client_uses_configured_connect_and_read_timeout_tuple(self) -> None:
        from lead_machine.llm_client import score_batch

        score = self._manual_score(total_score=75, grade="B")
        response = {
            "content": [{"type": "text", "text": json.dumps({"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": score}]})}],
            "usage": {"input_tokens": 123, "output_tokens": 45},
        }

        class FakeResponse:
            status_code = 200
            text = json.dumps(response)

            def json(self) -> dict:
                return response

        env = {
            "ANTHROPIC_API_KEY": "test-key",
            "ANTHROPIC_CONNECT_TIMEOUT_SECONDS": "7",
            "ANTHROPIC_READ_TIMEOUT_SECONDS": "88",
            "LLM_SCORING_PROVIDER_CHAIN": "anthropic",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch("lead_machine.llm_client._post", return_value=FakeResponse()) as post:
                score_batch("score this batch", model="claude-sonnet-5", max_tokens=4096)

        _, kwargs = post.call_args
        self.assertEqual(kwargs["timeout"], (7, 88))

    def test_llm_client_retries_timeout_up_to_max_retries(self) -> None:
        import requests
        from lead_machine.llm_client import score_batch

        score = self._manual_score(total_score=75, grade="B")
        response = {
            "content": [{"type": "text", "text": json.dumps({"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": score}]})}],
            "usage": {"input_tokens": 123, "output_tokens": 45},
        }

        class FakeResponse:
            status_code = 200
            text = json.dumps(response)

            def json(self) -> dict:
                return response

        env = {
            "ANTHROPIC_API_KEY": "test-key",
            "ANTHROPIC_MAX_RETRIES": "2",
            "ANTHROPIC_RETRY_BACKOFF_SECONDS": "2",
            "LLM_SCORING_PROVIDER_CHAIN": "anthropic",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "lead_machine.llm_client._post",
                side_effect=[requests.exceptions.ReadTimeout("read timed out"), requests.exceptions.Timeout("timed out"), FakeResponse()],
            ) as post:
                with patch("time.sleep") as sleep:
                    result = score_batch("score this batch", model="claude-sonnet-5", max_tokens=4096)

        self.assertEqual(result["bundle"]["batch_id"], "batch_001")
        self.assertEqual(post.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    def test_llm_client_retries_500_and_503_responses(self) -> None:
        from lead_machine.llm_client import score_batch

        score = self._manual_score(total_score=75, grade="B")
        success = {
            "content": [{"type": "text", "text": json.dumps({"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": score}]})}],
            "usage": {"input_tokens": 123, "output_tokens": 45},
        }

        class FakeResponse:
            def __init__(self, status_code: int, payload: dict | None = None) -> None:
                self.status_code = status_code
                self._payload = payload or {}
                self.text = json.dumps(payload or {"error": "temporary"})

            def json(self) -> dict:
                return self._payload

        env = {
            "ANTHROPIC_API_KEY": "test-key",
            "ANTHROPIC_MAX_RETRIES": "2",
            "ANTHROPIC_RETRY_BACKOFF_SECONDS": "2",
            "LLM_SCORING_PROVIDER_CHAIN": "anthropic",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "lead_machine.llm_client._post",
                side_effect=[FakeResponse(500), FakeResponse(503), FakeResponse(200, success)],
            ) as post:
                with patch("time.sleep") as sleep:
                    result = score_batch("score this batch", model="claude-sonnet-5", max_tokens=4096)

        self.assertEqual(result["bundle"]["batch_id"], "batch_001")
        self.assertEqual(post.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    def test_llm_client_does_not_retry_deterministic_400_response(self) -> None:
        from lead_machine.llm_client import score_batch

        class FakeResponse:
            status_code = 400
            text = "bad request"

        with patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "test-key", "ANTHROPIC_MAX_RETRIES": "2", "LLM_SCORING_PROVIDER_CHAIN": "anthropic"},
            clear=False,
        ):
            with patch("lead_machine.llm_client._post", return_value=FakeResponse()) as post:
                with patch("time.sleep") as sleep:
                    with self.assertRaises(RuntimeError) as caught:
                        score_batch("score this batch", model="claude-sonnet-5", max_tokens=4096)

        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()
        self.assertIn("Anthropic API error 400", str(caught.exception))

    def test_llm_client_final_retry_error_includes_timeout_settings_and_attempts(self) -> None:
        import requests
        from lead_machine.llm_client import score_batch

        env = {
            "ANTHROPIC_API_KEY": "test-key",
            "ANTHROPIC_CONNECT_TIMEOUT_SECONDS": "15",
            "ANTHROPIC_READ_TIMEOUT_SECONDS": "180",
            "ANTHROPIC_MAX_RETRIES": "2",
            "ANTHROPIC_RETRY_BACKOFF_SECONDS": "2",
            "LLM_SCORING_PROVIDER_CHAIN": "anthropic",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "lead_machine.llm_client._post",
                side_effect=requests.exceptions.ReadTimeout("read timed out"),
            ) as post:
                with patch("time.sleep"):
                    with self.assertRaises(RuntimeError) as caught:
                        score_batch("score this batch", model="claude-sonnet-5", max_tokens=4096)

        message = str(caught.exception)
        self.assertEqual(post.call_count, 3)
        self.assertIn("model=claude-sonnet-5", message)
        self.assertIn("connect_timeout=15", message)
        self.assertIn("read_timeout=180", message)
        self.assertIn("attempts=3", message)
        self.assertIn("read timed out", message)

    def test_llm_client_chain_skips_unknown_provider_names(self) -> None:
        from lead_machine import llm_client

        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "kimi,bogus,claude"}, clear=False):
            chain = llm_client._provider_chain()

        self.assertEqual(chain, ["kimi", "claude"])

    def test_llm_client_chain_defaults_to_codex_then_claude(self) -> None:
        # Was kimi,codex,claude. Reordered to subscription-only, codex first:
        # kimi-first sent prospect PII to Moonshot on any unmodified run, and a
        # paid member after a $0 runner is the unbounded-spend hazard.
        from lead_machine import llm_client

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLM_SCORING_PROVIDER_CHAIN", None)
            chain = llm_client._provider_chain()

        self.assertEqual(chain, ["codex", "claude"])

    def test_kimi_and_anthropic_remain_selectable_by_env(self) -> None:
        # Dropping them from the default removes them from the unattended path,
        # not from the tool. An explicit chain must still resolve them.
        from lead_machine import llm_client

        with patch.dict(
            os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "kimi,anthropic"}, clear=False
        ):
            self.assertEqual(llm_client._provider_chain(), ["kimi", "anthropic"])

    def test_llm_client_falls_back_to_next_provider_on_unavailable(self) -> None:
        from lead_machine import llm_client

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}

        def fake_down(prompt, *, model, max_tokens, **_):
            raise llm_client.ProviderUnavailable("no api key")

        def fake_up(prompt, *, model, max_tokens, **_):
            return {"bundle": bundle, "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}, "model": "m"}

        providers = {"kimi": fake_down, "codex": fake_up, "claude": fake_down}
        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "kimi,codex"}, clear=False):
            with patch.dict(llm_client._PROVIDERS, providers, clear=True):
                result = llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        self.assertEqual(result["provider"], "codex")
        self.assertEqual(result["bundle"]["batch_id"], "batch_001")

    def test_llm_client_forwards_explicit_model_only_to_the_first_provider(self) -> None:
        from lead_machine import llm_client

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}
        seen: list = []

        def fake_down(prompt, *, model, max_tokens, **_):
            seen.append(model)
            raise llm_client.ProviderUnavailable("down")

        def fake_up(prompt, *, model, max_tokens, **_):
            seen.append(model)
            return {"bundle": bundle, "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, "model": "m"}

        providers = {"kimi": fake_down, "anthropic": fake_up}
        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "kimi,anthropic"}, clear=False):
            with patch.dict(llm_client._PROVIDERS, providers, clear=True):
                llm_client.score_batch("score this batch", model="kimi-k3", max_tokens=4096)

        # The caller's model belongs to the provider it was chosen for. Handing
        # "kimi-k3" to the next API provider would 400 the whole chain.
        self.assertEqual(seen, ["kimi-k3", None])

    def test_llm_client_does_not_fall_back_on_invalid_llm_response(self) -> None:
        from lead_machine import llm_client

        def fake_bad_json(prompt, *, model, max_tokens, **_):
            raise llm_client.InvalidLlmResponse("bad json", raw_text="{nope")

        def fake_up(prompt, *, model, max_tokens, **_):
            raise AssertionError("next provider must not be tried")

        providers = {"kimi": fake_bad_json, "codex": fake_up}
        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "kimi,codex"}, clear=False):
            with patch.dict(llm_client._PROVIDERS, providers, clear=True):
                with self.assertRaises(llm_client.InvalidLlmResponse):
                    llm_client.score_batch("score this batch", model=None, max_tokens=4096)

    def test_llm_client_chain_exhaustion_lists_provider_reasons(self) -> None:
        from lead_machine import llm_client

        def fake_down(reason):
            def _fn(prompt, *, model, max_tokens, **_):
                raise llm_client.ProviderUnavailable(reason)
            return _fn

        providers = {"kimi": fake_down("missing KIMI_API_KEY"), "codex": fake_down("quota_exhausted"), "claude": fake_down("auth fail")}
        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "kimi,codex,claude"}, clear=False):
            with patch.dict(llm_client._PROVIDERS, providers, clear=True):
                with self.assertRaises(RuntimeError) as caught:
                    llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        message = str(caught.exception)
        self.assertIn("missing KIMI_API_KEY", message)
        self.assertIn("quota_exhausted", message)
        self.assertIn("auth fail", message)

    def test_kimi_provider_posts_chat_completion_and_parses_bundle(self) -> None:
        from lead_machine import llm_client

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}
        response = {
            "choices": [{"message": {"role": "assistant", "content": json.dumps(bundle)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }

        class FakeResponse:
            status_code = 200
            text = json.dumps(response)

            def json(self) -> dict:
                return response

        env = {"KIMI_API_KEY": "kimi-key", "LLM_SCORING_PROVIDER_CHAIN": "kimi"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("KIMI_SCORING_MODEL", None)
            os.environ.pop("LLM_SCORING_MODEL", None)
            with patch("lead_machine.llm_client.load_dotenv"):
                with patch("lead_machine.llm_client._post", return_value=FakeResponse()) as post:
                    result = llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        self.assertEqual(result["provider"], "kimi")
        self.assertEqual(result["model"], "kimi-k3")
        self.assertEqual(result["usage"], {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150})
        url, kwargs = post.call_args
        self.assertEqual(url[0], "https://api.moonshot.ai/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer kimi-key")
        self.assertEqual(kwargs["json"]["model"], "kimi-k3")
        self.assertEqual(kwargs["json"]["max_tokens"], 4096)
        self.assertEqual(kwargs["json"]["reasoning_effort"], "low")
        self.assertEqual(kwargs["json"]["messages"][0]["content"], "score this batch")
        self.assertEqual(kwargs["timeout"], (15, 180))

    def test_kimi_provider_reasoning_effort_is_configurable(self) -> None:
        from lead_machine import llm_client

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}
        response = {
            "choices": [{"message": {"role": "assistant", "content": json.dumps(bundle)}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

        class FakeResponse:
            status_code = 200
            text = json.dumps(response)

            def json(self) -> dict:
                return response

        env = {"KIMI_API_KEY": "kimi-key", "LLM_SCORING_PROVIDER_CHAIN": "kimi", "KIMI_REASONING_EFFORT": "high"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("KIMI_SCORING_MODEL", None)
            os.environ.pop("LLM_SCORING_MODEL", None)
            with patch("lead_machine.llm_client.load_dotenv"):
                with patch("lead_machine.llm_client._post", return_value=FakeResponse()) as post:
                    llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["reasoning_effort"], "high")

    def test_kimi_provider_missing_key_is_unavailable_and_falls_back(self) -> None:
        from lead_machine import llm_client

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}

        def fake_up(prompt, *, model, max_tokens, **_):
            return {"bundle": bundle, "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, "model": "m"}

        env = {"LLM_SCORING_PROVIDER_CHAIN": "kimi,codex"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("KIMI_API_KEY", None)
            os.environ.pop("MOONSHOT_API_KEY", None)
            with patch("lead_machine.llm_client.load_dotenv"):
                with patch.dict(llm_client._PROVIDERS, {"codex": fake_up}, clear=False):
                    result = llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        self.assertEqual(result["provider"], "codex")

    def test_kimi_provider_retries_429_then_serves(self) -> None:
        import requests  # noqa: F401  (parity with existing tests)
        from lead_machine import llm_client

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}
        success = {
            "choices": [{"message": {"role": "assistant", "content": json.dumps(bundle)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

        class FakeResponse:
            def __init__(self, status_code: int, payload: dict | None = None) -> None:
                self.status_code = status_code
                self._payload = payload or {}
                self.text = json.dumps(payload or {"error": "rate limited"})

            def json(self) -> dict:
                return self._payload

        env = {"KIMI_API_KEY": "kimi-key", "LLM_SCORING_PROVIDER_CHAIN": "kimi", "KIMI_RETRY_BACKOFF_SECONDS": "2"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("KIMI_SCORING_MODEL", None)
            os.environ.pop("LLM_SCORING_MODEL", None)
            with patch("lead_machine.llm_client.load_dotenv"):
                with patch("lead_machine.llm_client._post", side_effect=[FakeResponse(429), FakeResponse(200, success)]) as post:
                    with patch("time.sleep") as sleep:
                        result = llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        self.assertEqual(result["bundle"]["batch_id"], "batch_001")
        self.assertEqual(post.call_count, 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2])

    def test_kimi_provider_persistent_429_falls_back_to_next_provider(self) -> None:
        from lead_machine import llm_client

        class FakeResponse:
            status_code = 429
            text = '{"error": "rate limited"}'

            def json(self) -> dict:
                return {"error": "rate limited"}

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}

        def fake_up(prompt, *, model, max_tokens, **_):
            return {"bundle": bundle, "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, "model": "m"}

        env = {"KIMI_API_KEY": "kimi-key", "LLM_SCORING_PROVIDER_CHAIN": "kimi,codex", "KIMI_MAX_RETRIES": "1"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("KIMI_SCORING_MODEL", None)
            os.environ.pop("LLM_SCORING_MODEL", None)
            with patch("lead_machine.llm_client.load_dotenv"):
                with patch("lead_machine.llm_client._post", return_value=FakeResponse()) as post:
                    with patch("time.sleep"):
                        with patch.dict(llm_client._PROVIDERS, {"codex": fake_up}, clear=False):
                            result = llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        self.assertEqual(result["provider"], "codex")
        self.assertEqual(post.call_count, 2)

    def test_kimi_provider_uses_legacy_llm_scoring_model_alias(self) -> None:
        from lead_machine import llm_client

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}
        response = {
            "choices": [{"message": {"role": "assistant", "content": json.dumps(bundle)}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

        class FakeResponse:
            status_code = 200
            text = json.dumps(response)

            def json(self) -> dict:
                return response

        env = {"KIMI_API_KEY": "kimi-key", "LLM_SCORING_PROVIDER_CHAIN": "kimi", "LLM_SCORING_MODEL": "kimi-k2.6"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("KIMI_SCORING_MODEL", None)
            with patch("lead_machine.llm_client.load_dotenv"):
                with patch("lead_machine.llm_client._post", return_value=FakeResponse()) as post:
                    result = llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        self.assertEqual(result["model"], "kimi-k2.6")
        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["model"], "kimi-k2.6")

    def test_codex_cli_provider_serves_bundle(self) -> None:
        from lead_machine import llm_client
        import score_batch_subscription as subs

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}
        runner_result = subs.RunnerResult(
            result="ok",
            raw_text=json.dumps(bundle),
            usage={"input_tokens": 10, "output_tokens": 5},
            stderr_tail="",
            duration_seconds=1.0,
        )

        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "codex"}, clear=False):
            with patch("score_batch_subscription.preflight_subscription_auth", return_value=subs.AuthResult(True, "chatgpt_subscription", "ok")) as auth:
                with patch("score_batch_subscription.run_runner_attempt", return_value=runner_result) as attempt:
                    result = llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        self.assertEqual(result["provider"], "codex")
        self.assertEqual(result["bundle"]["batch_id"], "batch_001")
        self.assertEqual(result["usage"]["total_tokens"], 15)
        self.assertEqual(auth.call_args.args[0], "codex")
        _, kwargs = attempt.call_args
        self.assertEqual(kwargs["prompt_text"], "score this batch")
        self.assertTrue(kwargs["use_output_schema"])

    def test_codex_cli_auth_failure_falls_back_to_claude(self) -> None:
        from lead_machine import llm_client
        import score_batch_subscription as subs

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}
        runner_result = subs.RunnerResult(result="ok", raw_text=json.dumps(bundle), usage=None, stderr_tail="", duration_seconds=1.0)

        def fake_auth(runner, *, env, process_runner=subs.run_process):
            if runner == "codex":
                return subs.AuthResult(False, "unverified", "not logged in")
            return subs.AuthResult(True, "claude_subscription:max", "ok")

        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "codex,claude"}, clear=False):
            with patch("score_batch_subscription.preflight_subscription_auth", side_effect=fake_auth):
                with patch("score_batch_subscription.run_runner_attempt", return_value=runner_result) as attempt:
                    result = llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        self.assertEqual(result["provider"], "claude")
        self.assertEqual(result["usage"], {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
        self.assertEqual(attempt.call_args.args[0], "claude")

    def test_cli_runner_quota_result_falls_back(self) -> None:
        from lead_machine import llm_client
        import score_batch_subscription as subs

        bundle = {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}]}
        quota = subs.RunnerResult(result="quota_exhausted", raw_text="", usage=None, stderr_tail="usage limit", duration_seconds=0.5)
        ok = subs.RunnerResult(result="ok", raw_text=json.dumps(bundle), usage=None, stderr_tail="", duration_seconds=1.0)

        def fake_attempt(runner, **kwargs):
            return quota if runner == "codex" else ok

        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "codex,claude"}, clear=False):
            with patch("score_batch_subscription.preflight_subscription_auth", return_value=subs.AuthResult(True, "sub", "ok")):
                with patch("score_batch_subscription.run_runner_attempt", side_effect=fake_attempt):
                    result = llm_client.score_batch("score this batch", model=None, max_tokens=4096)

        self.assertEqual(result["provider"], "claude")

    def test_cli_provider_invalid_json_does_not_fall_back(self) -> None:
        from lead_machine import llm_client
        import score_batch_subscription as subs

        bad = subs.RunnerResult(result="ok", raw_text="{not json", usage=None, stderr_tail="", duration_seconds=1.0)

        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "codex,claude"}, clear=False):
            with patch("score_batch_subscription.preflight_subscription_auth", return_value=subs.AuthResult(True, "sub", "ok")):
                with patch("score_batch_subscription.run_runner_attempt", return_value=bad):
                    with self.assertRaises(llm_client.InvalidLlmResponse):
                        llm_client.score_batch("score this batch", model=None, max_tokens=4096)

    def test_resolve_scoring_model_follows_the_first_provider_in_the_chain(self) -> None:
        from lead_machine import llm_client

        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "kimi,codex"}, clear=False):
            os.environ.pop("KIMI_SCORING_MODEL", None)
            os.environ.pop("LLM_SCORING_MODEL", None)
            # resolve_scoring_model() calls load_dotenv(); the real repo .env one
            # directory up (outside this worktree) sets LLM_SCORING_MODEL, and
            # dotenv's override=False only protects keys still present in
            # os.environ — popped keys get repopulated from disk. Patch it out
            # here, same as the kimi provider tests above, so the assertions
            # below reflect the env this test actually set, not ambient .env.
            with patch("lead_machine.llm_client.load_dotenv"):
                self.assertEqual(llm_client.resolve_scoring_model(), llm_client.DEFAULT_KIMI_SCORING_MODEL)

        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "codex,kimi"}, clear=False):
            with patch("lead_machine.llm_client.load_dotenv"):
                self.assertEqual(llm_client.resolve_scoring_model(), "subscription:codex")

        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "anthropic"}, clear=False):
            os.environ.pop("LLM_SCORING_MODEL", None)
            with patch("lead_machine.llm_client.load_dotenv"):
                self.assertEqual(llm_client.resolve_scoring_model(), llm_client.DEFAULT_LLM_SCORING_MODEL)

    def test_api_llm_caps_stop_before_next_batch_call(self) -> None:
        from score_leads import score_records_api_run

        first_score = self._manual_score(total_score=75, grade="B")
        first_enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        second_enriched = {
            "google": {
                **self._google_fixture(),
                "business_name": "Second Landscaping",
                "place_id": "ChIJ456",
            },
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int, **_: object) -> dict:
            return {
                "bundle": {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": first_score}]},
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch) as score_batch:
                run = score_records_api_run(
                    [first_enriched, second_enriched],
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="hardscaping",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=4096,
                    max_calls=1,
                    max_tokens_per_run=0,
                    max_usd_per_run=0.0,
                )

        self.assertEqual(score_batch.call_count, 1)
        self.assertEqual(run.calls, 1)
        self.assertEqual(run.tokens, 15)
        self.assertTrue(run.stopped_by_cap)
        self.assertEqual([row["Business Name"] for row in run.manual_run.rows], ["A+ Landscaping LLC"])
        self.assertEqual([missing.lead_name for missing in run.manual_run.missing_scores], ["Second Landscaping"])

    def test_api_llm_token_cap_stops_before_batch_that_could_exceed_remaining_budget(self) -> None:
        from score_leads import build_api_llm_scoring_prompt, build_llm_scoring_record, score_records_api_run

        first_score = self._manual_score(total_score=75, grade="B")
        first_enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        second_enriched = {
            "google": {
                **self._google_fixture(),
                "business_name": "Second Landscaping",
                "place_id": "ChIJ456",
            },
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int, **_: object) -> dict:
            return {
                "bundle": {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": first_score}]},
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            }

        projected_prompt = build_api_llm_scoring_prompt(
            [build_llm_scoring_record(dict(first_enriched), fallback_vertical="hardscaping")],
            batch_id="batch_001",
        )
        projected_tokens = (len(projected_prompt) + 3) // 4 + 20

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch) as score_batch:
                run = score_records_api_run(
                    [first_enriched, second_enriched],
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="hardscaping",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=20,
                    max_calls=0,
                    max_tokens_per_run=projected_tokens + 4,
                    max_usd_per_run=0.0,
                )

        self.assertEqual(score_batch.call_count, 1)
        self.assertEqual(run.calls, 1)
        self.assertEqual(run.tokens, 5)
        self.assertTrue(run.stopped_by_cap)
        self.assertEqual(run.stop_reason, "MAX_LLM_TOKENS_PER_RUN")
        self.assertEqual([row["Business Name"] for row in run.manual_run.rows], ["A+ Landscaping LLC"])
        self.assertEqual([missing.lead_name for missing in run.manual_run.missing_scores], ["Second Landscaping"])

    def test_api_llm_usd_cap_stops_before_batch_that_could_exceed_remaining_budget(self) -> None:
        from score_leads import build_api_llm_scoring_prompt, build_llm_scoring_record, estimate_llm_usd, score_records_api_run

        first_score = self._manual_score(total_score=75, grade="B")
        first_enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        second_enriched = {
            "google": {
                **self._google_fixture(),
                "business_name": "Second Landscaping",
                "place_id": "ChIJ456",
            },
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int, **_: object) -> dict:
            return {
                "bundle": {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": first_score}]},
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }

        projected_prompt = build_api_llm_scoring_prompt(
            [build_llm_scoring_record(dict(first_enriched), fallback_vertical="hardscaping")],
            batch_id="batch_001",
        )
        projected_usd = estimate_llm_usd(
            {"input_tokens": (len(projected_prompt) + 3) // 4, "output_tokens": 20}
        )
        first_call_usd = estimate_llm_usd({"input_tokens": 1, "output_tokens": 1})

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch) as score_batch:
                run = score_records_api_run(
                    [first_enriched, second_enriched],
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="hardscaping",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=20,
                    max_calls=0,
                    max_tokens_per_run=0,
                    max_usd_per_run=projected_usd + (first_call_usd / 2),
                )

        self.assertEqual(score_batch.call_count, 1)
        self.assertEqual(run.calls, 1)
        self.assertTrue(run.stopped_by_cap)
        self.assertEqual(run.stop_reason, "MAX_LLM_USD_PER_RUN")
        self.assertEqual([row["Business Name"] for row in run.manual_run.rows], ["A+ Landscaping LLC"])
        self.assertEqual([missing.lead_name for missing in run.manual_run.missing_scores], ["Second Landscaping"])

    def test_api_llm_spend_is_priced_by_the_provider_that_actually_served_the_batch(self) -> None:
        """A $0 subscription runner first in the chain must not zero-rate a paid fallback.

        With LLM_SCORING_PROVIDER_CHAIN=codex,kimi,... the run-level model is
        "subscription:codex" at $0/$0. If codex is quota-blocked and kimi serves
        the batch, pricing the usage by the run-level model bills real Moonshot
        tokens at zero and MAX_LLM_USD_PER_RUN never trips.
        """
        from score_leads import estimate_llm_usd, score_records_api_run

        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        usage = {"input_tokens": 400_000, "output_tokens": 200_000, "total_tokens": 600_000}

        def kimi_served(prompt: str, *, model: str, max_tokens: int, **_: object) -> dict:
            # What llm_client returns after falling past codex onto kimi.
            return {
                "bundle": {
                    "batch_id": "batch_001",
                    "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}],
                },
                "usage": dict(usage),
                "provider": "kimi",
                "model": "kimi-k3",
            }

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch", side_effect=kimi_served):
                run = score_records_api_run(
                    [enriched],
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="hardscaping",
                    batch_size=1,
                    model="subscription:codex",
                    max_tokens=20,
                    max_calls=0,
                    max_tokens_per_run=0,
                    max_usd_per_run=0,
                )

        self.assertEqual(run.usd, estimate_llm_usd(usage, model="kimi-k3"))
        self.assertGreater(run.usd, 0.0)
        self.assertNotEqual(run.usd, estimate_llm_usd(usage, model="subscription:codex"))

    def test_spend_projection_uses_the_priciest_reachable_tier_not_the_first(self) -> None:
        """A $0 runner in front must not zero out the pre-spend projection.

        With codex first, resolve_scoring_model() is "subscription:codex" at
        $0/$0. Projecting with that makes LLM_SPEND_CONFIRM_USD unreachable, so
        a run that falls through to a paid tier spends without ever asking.
        """
        from score_leads import resolve_projection_model, llm_pricing

        with patch.dict(
            os.environ,
            {"LLM_SCORING_PROVIDER_CHAIN": "codex,kimi,claude,anthropic", "KIMI_SCORING_MODEL": "kimi-k3"},
            clear=False,
        ):
            projected = resolve_projection_model()

        self.assertNotEqual(projected, "subscription:codex")
        self.assertGreater(llm_pricing(projected)[1], 0.0)

        # An all-subscription chain has no paid tier to fall through to.
        with patch.dict(os.environ, {"LLM_SCORING_PROVIDER_CHAIN": "codex,claude"}, clear=False):
            self.assertEqual(llm_pricing(resolve_projection_model())[1], 0.0)

    def test_projection_aborts_when_a_reachable_chain_model_has_no_price(self) -> None:
        """AGENTS.md promises this abort; the code used to `continue` past it.

        An unpriced chain member is invisible to the projection, so it projects as
        if that tier did not exist. Fall through to it and the run spends real
        money the pre-spend gate never saw. Aborting costs nothing here — this
        runs before any call.
        """
        from score_leads import resolve_projection_model, UnknownLlmModelError

        with patch.dict(
            os.environ,
            {"LLM_SCORING_PROVIDER_CHAIN": "codex,kimi",
             "KIMI_SCORING_MODEL": "kimi-not-in-the-pricing-table"},
            clear=False,
        ):
            with self.assertRaises(UnknownLlmModelError) as caught:
                resolve_projection_model()

        self.assertIn("kimi-not-in-the-pricing-table", str(caught.exception))
        self.assertIn("LLM_PRICING_USD_PER_MILLION_TOKENS", str(caught.exception))

    def test_unpriced_served_model_banks_at_worst_case_not_at_a_free_runner(self) -> None:
        """Mid-flight the call is already paid for, so this must not abort — but
        charging an unknown served model at a $0 subscription run-level model
        banks real spend as $0.00 and the USD cap stops binding. Charge it at the
        priciest tier the chain could have reached instead.
        """
        from score_leads import resolve_served_model, llm_pricing

        charged = resolve_served_model(
            {"model": "some-model-nobody-registered"},
            "subscription:codex",
            worst_case="claude-opus-5",
        )

        self.assertEqual(charged, "claude-opus-5")
        self.assertGreater(llm_pricing(charged)[1], 0.0)

    def test_unpriced_served_model_never_aborts_when_worst_case_is_also_unknown(self) -> None:
        # The call is already paid for; an unknown id must not kill the run.
        from score_leads import resolve_served_model

        self.assertEqual(
            resolve_served_model({"model": "mystery"}, "kimi-k3", worst_case="also-mystery"),
            "kimi-k3",
        )
        self.assertEqual(resolve_served_model({"model": "mystery"}, "kimi-k3"), "kimi-k3")

    def test_subscription_served_batch_is_billed_at_zero_not_the_runner_model_rate(self) -> None:
        """The mirror of the test above: a sub-served batch must stay free.

        _score_batch_cli reports model="subscription:<runner>" precisely so the
        caller prices it at $0. Pricing by the runner's model id instead
        (claude-sonnet-5) would charge MAX_LLM_USD_PER_RUN API rates for tokens
        that cost nothing on a subscription.
        """
        from score_leads import estimate_llm_usd, score_records_api_run

        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        usage = {"input_tokens": 1, "output_tokens": 1307, "total_tokens": 1308}

        def claude_sub_served(prompt: str, *, model: str, max_tokens: int, **_: object) -> dict:
            return {
                "bundle": {
                    "batch_id": "batch_001",
                    "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}],
                },
                "usage": dict(usage),
                "provider": "claude",
                "model": "subscription:claude",
                "runner_model": "claude-sonnet-5",
            }

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch", side_effect=claude_sub_served):
                run = score_records_api_run(
                    [enriched],
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="hardscaping",
                    batch_size=1,
                    model="subscription:codex",
                    max_tokens=20,
                    max_calls=0,
                    max_tokens_per_run=0,
                    max_usd_per_run=0,
                )

        self.assertEqual(run.usd, 0.0)
        self.assertGreater(estimate_llm_usd(usage, model="claude-sonnet-5"), 0.0)
        # Tokens still count, so MAX_LLM_TOKENS_PER_RUN keeps bounding sub runs.
        self.assertEqual(run.tokens, 1308)

    def _run_score_leads(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("sys.argv", argv), redirect_stdout(stdout), redirect_stderr(stderr):
            code = score_leads_main()
        return code, stdout.getvalue(), stderr.getvalue()

    def _write_enriched_jsonl(self, path: Path) -> None:
        record = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    def _fake_score_batch_factory(self):
        score = self._manual_score(total_score=75, grade="B")

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int, **_: object) -> dict:
            return {
                "bundle": {
                    "batch_id": "batch_001",
                    "scores": [{"score_file": "chij123.json", "score": score}],
                },
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }

        return fake_score_batch

    def test_dry_run_llm_cost_does_not_call_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "enriched.jsonl"
            output_path = Path(tmp) / "scored.csv"
            self._write_enriched_jsonl(input_path)
            argv = [
                "score_leads.py",
                "--mode", "api-llm",
                "--input", str(input_path),
                "--output", str(output_path),
                "--scores-dir", str(Path(tmp) / "scores"),
                "--vertical", "hardscaping",
                "--dry-run-llm-cost",
            ]
            with patch("score_leads.llm_client.score_batch") as score_batch:
                code, stdout, stderr = self._run_score_leads(argv)

            self.assertEqual(code, 0, stderr)
            score_batch.assert_not_called()
            self.assertFalse(output_path.exists())
            self.assertIn("[LLM COST]", stdout)
            self.assertIn("batches=1", stdout)
            self.assertIn("projected_usd=", stdout)
            self.assertIn("[DRY-RUN]", stdout)

    def _write_large_evidence_scored_csv(self, path: Path) -> str:
        """Write a scored CSV whose Raw Evidence field exceeds the 128 KB
        default csv field-size limit, returning the giant value."""
        huge_evidence = json.dumps({"blob": "x" * 200_000})
        self.assertGreater(len(huge_evidence), 131072)
        fieldnames = [
            "Business Name",
            "Phone",
            "Sendable",
            "Packet Eligible",
            "Raw Evidence",
        ]
        row = {
            "Business Name": "Big Evidence Co",
            "Phone": "216-555-0100",
            "Sendable": "Yes",
            "Packet Eligible": "Yes",
            "Raw Evidence": huge_evidence,
        }
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow(row)
        return huge_evidence

    @contextmanager
    def _default_csv_field_limit(self):
        """Force the stock 128 KB csv field-size limit so the regression
        actually exercises the raise; restore the prior limit afterward."""
        previous = csv.field_size_limit()
        csv.field_size_limit(131072)
        try:
            yield
        finally:
            csv.field_size_limit(previous)

    def test_read_csv_rows_handles_large_raw_evidence_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scored_path = Path(tmp) / "scored.csv"
            huge_evidence = self._write_large_evidence_scored_csv(scored_path)
            with self._default_csv_field_limit():
                rows = read_csv_rows(scored_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Raw Evidence"], huge_evidence)

    def test_dry_run_llm_cost_resume_reads_large_existing_scored_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "enriched.jsonl"
            output_path = Path(tmp) / "scored.csv"
            self._write_enriched_jsonl(input_path)
            self._write_large_evidence_scored_csv(output_path)
            argv = [
                "score_leads.py",
                "--mode", "api-llm",
                "--input", str(input_path),
                "--output", str(output_path),
                "--scores-dir", str(Path(tmp) / "scores"),
                "--vertical", "hardscaping",
                "--dry-run-llm-cost",
            ]
            with self._default_csv_field_limit():
                with patch("score_leads.llm_client.score_batch") as score_batch:
                    code, stdout, stderr = self._run_score_leads(argv)
            self.assertEqual(code, 0, stderr)
            score_batch.assert_not_called()
            self.assertIn("[RESUME] Existing scored rows: 1", stdout)
            self.assertIn("[DRY-RUN]", stdout)

    def test_upload_airtable_reads_large_raw_evidence_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scored_path = Path(tmp) / "scored.csv"
            self._write_large_evidence_scored_csv(scored_path)
            with self._default_csv_field_limit():
                records = upload_airtable.read_csv_records(
                    scored_path,
                    packet_dir=Path(tmp),
                )
            self.assertEqual(len(records), 1)

    def test_projected_cost_over_threshold_refuses_without_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "enriched.jsonl"
            output_path = Path(tmp) / "scored.csv"
            self._write_enriched_jsonl(input_path)
            argv = [
                "score_leads.py",
                "--mode", "api-llm",
                "--input", str(input_path),
                "--output", str(output_path),
                "--scores-dir", str(Path(tmp) / "scores"),
                "--vertical", "hardscaping",
            ]
            # Pin the chain: score_leads.main() calls load_dotenv(), so without
            # this the developer's real .env decides the projection model and a
            # subscription-first chain would project $0 and skip the refusal.
            env = {"LLM_SPEND_CONFIRM_USD": "0.0001", "LLM_SCORING_PROVIDER_CHAIN": "anthropic"}
            with patch.dict(os.environ, env, clear=False):
                with patch("score_leads.llm_client.score_batch") as score_batch:
                    code, stdout, stderr = self._run_score_leads(argv)

                self.assertEqual(code, 1)
                score_batch.assert_not_called()
                self.assertFalse(output_path.exists())
                self.assertIn("[REFUSE]", stdout)
                self.assertIn("--confirm-llm-spend", stdout)

    def test_confirm_llm_spend_allows_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "enriched.jsonl"
            output_path = Path(tmp) / "scored.csv"
            self._write_enriched_jsonl(input_path)
            argv = [
                "score_leads.py",
                "--mode", "api-llm",
                "--input", str(input_path),
                "--output", str(output_path),
                "--scores-dir", str(Path(tmp) / "scores"),
                "--vertical", "hardscaping",
                "--confirm-llm-spend",
            ]
            with patch.dict(os.environ, {"LLM_SPEND_CONFIRM_USD": "0.0001"}, clear=False):
                with patch(
                    "score_leads.llm_client.score_batch",
                    side_effect=self._fake_score_batch_factory(),
                ) as score_batch:
                    code, stdout, stderr = self._run_score_leads(argv)

                self.assertEqual(code, 0, stderr)
                score_batch.assert_called_once()
                self.assertTrue(output_path.exists())

    def test_stale_scores_dir_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "enriched.jsonl"
            output_path = Path(tmp) / "scored.csv"
            self._write_enriched_jsonl(input_path)
            scores_dir = Path(tmp) / "scores"
            scores_dir.mkdir(parents=True)
            (scores_dir / "stale_old.json").write_text("{}", encoding="utf-8")
            argv = [
                "score_leads.py",
                "--mode", "api-llm",
                "--input", str(input_path),
                "--output", str(output_path),
                "--scores-dir", str(scores_dir),
                "--vertical", "hardscaping",
            ]
            with patch("score_leads.llm_client.score_batch") as score_batch:
                code, stdout, stderr = self._run_score_leads(argv)

            self.assertEqual(code, 1)
            score_batch.assert_not_called()
            self.assertTrue((scores_dir / "stale_old.json").exists())
            self.assertIn("[REFUSE]", stdout)
            self.assertIn("--fresh-scores-dir", stdout)

    def test_fresh_scores_dir_clears_stale_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "enriched.jsonl"
            output_path = Path(tmp) / "scored.csv"
            self._write_enriched_jsonl(input_path)
            scores_dir = Path(tmp) / "scores"
            bundle_dir = scores_dir / "_api_bundles"
            bundle_dir.mkdir(parents=True)
            (scores_dir / "stale_old.json").write_text("{}", encoding="utf-8")
            (bundle_dir / "old_bundle.json").write_text("{}", encoding="utf-8")
            argv = [
                "score_leads.py",
                "--mode", "api-llm",
                "--input", str(input_path),
                "--output", str(output_path),
                "--scores-dir", str(scores_dir),
                "--vertical", "hardscaping",
                "--fresh-scores-dir",
            ]
            with patch(
                "score_leads.llm_client.score_batch",
                side_effect=self._fake_score_batch_factory(),
            ) as score_batch:
                code, stdout, stderr = self._run_score_leads(argv)

            self.assertEqual(code, 0, stderr)
            score_batch.assert_called_once()
            self.assertFalse((scores_dir / "stale_old.json").exists())
            self.assertFalse((bundle_dir / "old_bundle.json").exists())
            self.assertIn("[FRESH]", stdout)

    def test_usage_metadata_written_after_successful_call_even_if_validation_fails(self) -> None:
        from score_leads import score_records_api_run

        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        with tempfile.TemporaryDirectory() as tmp:
            usage_path = Path(tmp) / "usage.json"
            with patch(
                "score_leads.llm_client.score_batch",
                side_effect=self._fake_score_batch_factory(),
            ):
                with patch(
                    "score_leads.score_records_manual_run",
                    side_effect=ValueError("merge validation failed"),
                ):
                    with self.assertRaises(ValueError):
                        score_records_api_run(
                            [enriched],
                            scores_dir=Path(tmp) / "scores",
                            fallback_vertical="hardscaping",
                            batch_size=1,
                            model="claude-sonnet-5",
                            max_tokens=4096,
                            max_calls=0,
                            max_tokens_per_run=0,
                            max_usd_per_run=0.0,
                            usage_output_path=usage_path,
                        )

            self.assertTrue(usage_path.exists())
            payload = json.loads(usage_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["calls"], 1)
            self.assertEqual(payload["total_tokens"], 15)
            self.assertTrue(payload["partial"])

    def test_api_llm_mode_writes_byte_identical_csv_to_manual_bundle_import(self) -> None:
        score_one = self._manual_score(total_score=75, grade="B")
        score_two = self._manual_score(
            total_score=86,
            grade="A",
            subscores={
                "icp_fit": 18,
                "reputation_demand_proof": 14,
                "web_conversion_pain": 22,
                "mockup_leverage": 12,
                "reachability": 8,
                "offer_angle_clarity": 8,
                "evidence_confidence": 4,
            },
            best_angle="No-Site Launch",
        )
        first_enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        second_enriched = {
            "google": {
                **self._google_fixture(),
                "business_name": "Second Landscaping",
                "place_id": "ChIJ456",
            },
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        bundle = {
            "batch_id": "batch_001",
            "scores": [
                {"score_file": "chij123.json", "score": score_one},
                {"score_file": "chij456.json", "score": score_two},
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            input_path = tmp_path / "enriched.jsonl"
            input_path.write_text(f"{json.dumps(first_enriched)}\n{json.dumps(second_enriched)}\n", encoding="utf-8")
            manual_scores_dir = tmp_path / "manual_scores"
            api_scores_dir = tmp_path / "api_scores"
            manual_output = tmp_path / "manual.csv"
            api_output = tmp_path / "api.csv"
            bundle_path = tmp_path / "bundle.json"
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            import_score_bundle(bundle_path, manual_scores_dir)

            manual_argv = [
                "score_leads.py",
                "--input",
                str(input_path),
                "--output",
                str(manual_output),
                "--vertical",
                "hardscaping",
                "--mode",
                "manual-llm",
                "--scores-dir",
                str(manual_scores_dir),
            ]
            with patch("sys.argv", manual_argv):
                manual_code = score_leads_main()

            api_argv = [
                "score_leads.py",
                "--input",
                str(input_path),
                "--output",
                str(api_output),
                "--vertical",
                "hardscaping",
                "--mode",
                "api-llm",
                "--scores-dir",
                str(api_scores_dir),
                "--llm-batch-size",
                "2",
            ]
            with patch("score_leads.llm_client.score_batch", return_value={"bundle": bundle, "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}):
                with patch("sys.argv", api_argv):
                    api_code = score_leads_main()

            manual_bytes = manual_output.read_bytes()
            api_bytes = api_output.read_bytes()

        self.assertEqual(manual_code, 0)
        self.assertEqual(api_code, 0)
        self.assertEqual(api_bytes, manual_bytes)

    def test_api_llm_resume_skips_existing_csv_rows_and_continues_from_same_input(self) -> None:
        records = [
            self._huge_detailing_record(name=f"Detailing {index}", place_id=f"ChIJ{index}")
            for index in range(1, 5)
        ]
        for index, record in enumerate(records, start=1):
            record["google"]["google_maps_url"] = f"https://maps.google.com/?cid={index}"

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int, **_: object) -> dict:
            score_files = sorted(set(re.findall(r'"score_file": "([^"]+\.json)"', prompt)))
            return {
                "bundle": {
                    "batch_id": "batch_001",
                    "scores": [
                        {"score_file": score_file, "score": self._manual_score(total_score=75, grade="B")}
                        for score_file in score_files
                    ],
                },
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            input_path = tmp_path / "enriched.jsonl"
            output_path = tmp_path / "scored.csv"
            scores_dir = tmp_path / "scores"
            input_path.write_text(
                "".join(f"{json.dumps(record)}\n" for record in records),
                encoding="utf-8",
            )

            first_argv = [
                "score_leads.py",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--vertical",
                "detailing",
                "--mode",
                "api-llm",
                "--scores-dir",
                str(scores_dir),
                "--llm-batch-size",
                "1",
                "--limit",
                "2",
            ]
            with patch.dict(os.environ, {"MAX_LLM_CALLS_PER_RUN": "1", "MAX_LLM_TOKENS_PER_RUN": "0", "MAX_LLM_USD_PER_RUN": "0"}):
                with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch):
                    with patch("sys.argv", first_argv):
                        first_code = score_leads_main()

            with output_path.open(newline="", encoding="utf-8") as f:
                first_rows = list(csv.DictReader(f))
            self.assertEqual(first_code, 0)
            self.assertEqual([row["Place Key"] for row in first_rows], ["ChIJ1"])

            second_argv = list(first_argv)
            with patch.dict(os.environ, {"MAX_LLM_CALLS_PER_RUN": "0", "MAX_LLM_TOKENS_PER_RUN": "0", "MAX_LLM_USD_PER_RUN": "0"}):
                with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch) as score_batch:
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        with patch("sys.argv", second_argv):
                            second_code = score_leads_main()

            with output_path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            scored_files = [
                sorted(set(re.findall(r'"score_file": "([^"]+\.json)"', call.args[0])))
                for call in score_batch.call_args_list
            ]

        self.assertEqual(second_code, 0)
        self.assertEqual(scored_files, [["chij2.json"], ["chij3.json"]])
        self.assertEqual([row["Place Key"] for row in rows], ["ChIJ1", "ChIJ2", "ChIJ3"])
        self.assertIn("[RESUME] Existing scored rows: 1", stdout.getvalue())
        self.assertIn("[RESUME] Skipping already scored: 1", stdout.getvalue())
        self.assertIn("[RESUME] New records selected: 2", stdout.getvalue())

    def test_api_llm_resume_offset_selects_manual_chunk_after_skipping_scored_rows(self) -> None:
        records = [
            self._huge_detailing_record(name=f"Detailing {index}", place_id=f"ChIJ{index}")
            for index in range(1, 5)
        ]
        for index, record in enumerate(records, start=1):
            record["google"]["google_maps_url"] = f"https://maps.google.com/?cid={index}"

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int, **_: object) -> dict:
            score_files = sorted(set(re.findall(r'"score_file": "([^"]+\.json)"', prompt)))
            return {
                "bundle": {
                    "batch_id": "batch_001",
                    "scores": [
                        {"score_file": score_file, "score": self._manual_score(total_score=75, grade="B")}
                        for score_file in score_files
                    ],
                },
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            input_path = tmp_path / "enriched.jsonl"
            output_path = tmp_path / "scored.csv"
            scores_dir = tmp_path / "scores"
            input_path.write_text(
                "".join(f"{json.dumps(record)}\n" for record in records),
                encoding="utf-8",
            )
            write_csv(output_path, [{"Place Key": "ChIJ2", "Business Name": "Existing Detail"}])

            argv = [
                "score_leads.py",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--vertical",
                "detailing",
                "--mode",
                "api-llm",
                "--scores-dir",
                str(scores_dir),
                "--llm-batch-size",
                "1",
                "--offset",
                "1",
                "--limit",
                "1",
            ]
            with patch.dict(os.environ, {"MAX_LLM_CALLS_PER_RUN": "0", "MAX_LLM_TOKENS_PER_RUN": "0", "MAX_LLM_USD_PER_RUN": "0"}):
                with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch) as score_batch:
                    with patch("sys.argv", argv):
                        code = score_leads_main()

            with output_path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            scored_files = [
                sorted(set(re.findall(r'"score_file": "([^"]+\.json)"', call.args[0])))
                for call in score_batch.call_args_list
            ]

        self.assertEqual(code, 0)
        self.assertEqual(scored_files, [["chij3.json"]])
        self.assertEqual([row["Place Key"] for row in rows], ["ChIJ2", "ChIJ3"])

    def test_api_llm_resume_does_not_hide_unknown_refs_for_new_records(self) -> None:
        first = self._huge_detailing_record(name="Already Scored Detail", place_id="ChIJ1")
        second = self._huge_detailing_record(name="Bad Ref Detail", place_id="ChIJ2")
        first["google"]["google_maps_url"] = "https://maps.google.com/?cid=1"
        second["google"]["google_maps_url"] = "https://maps.google.com/?cid=2"
        fake_ref_score = self._manual_score(
            total_score=75,
            grade="B",
            evidence_refs=["fake_unknown_ref"],
            safe_outreach_claims=[
                {"claim": "Your buyer-intent signals show active demand.", "evidence_refs": ["fake_unknown_ref"]}
            ],
        )
        fake_ref_bundle = {
            "batch_id": "batch_001",
            "scores": [{"score_file": "chij2.json", "score": fake_ref_score}],
        }

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            existing_keys = {"place:ChIJ1"}
            with patch(
                "score_leads.llm_client.score_batch",
                return_value={
                    "bundle": fake_ref_bundle,
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                },
            ):
                with self.assertRaises(ValueError) as caught:
                    from score_leads import score_records_api_run

                    score_records_api_run(
                        [first, second],
                        scores_dir=scores_dir,
                        fallback_vertical="detailing",
                        batch_size=1,
                        model="claude-sonnet-5",
                        max_tokens=4096,
                        max_calls=0,
                        max_tokens_per_run=100_000,
                        max_usd_per_run=0.0,
                        resume_scored_keys=existing_keys,
                    )

        self.assertIn("Unknown evidence_refs: fake_unknown_ref", str(caught.exception))

    def _huge_detailing_record(self, *, name: str = "A+ Detailing LLC", place_id: str = "ChIJ123") -> dict:
        giant_text = (
            "Ceramic coating packages include paint correction before installation. "
            "Call or text for a quote and appointment. "
            + ("generic navigation filler SHOULD_NOT_APPEAR_IN_COMPACT_PROMPT " * 900)
        )
        return {
            "google": {
                **self._google_fixture(),
                "business_name": name,
                "place_id": place_id,
                "category": "auto_detailing",
                "address": "123 Main St, Kirtland, OH",
            },
            "website": {
                "site_status": "weak",
                "final_url": "https://example.com",
                "homepage": {"url": "https://example.com", "text": giant_text},
                "pages": {
                    "services": {
                        "url": "https://example.com/services",
                        "text": "Paint protection film and PPF installs are available. Request an estimate online.",
                    },
                    "about": {
                        "url": "https://example.com/about",
                        "text": "Award winning team. " + ("about filler " * 600),
                    },
                },
                "combined_text": giant_text,
                "cta_strength": "Weak",
                "contact_form_detected": "No",
                "estimate_form_detected": "No",
                "form_strength": "None",
                "form_detection_method": "none",
                "high_ticket_terms": ["ceramic coating", "paint correction", "PPF"],
                "third_party_tools": ["ServiceTitan"],
                "social_links": ["https://facebook.com/example-detailing"],
                "desktop_screenshot_path": "screenshots/detailing-desktop.png",
                "mobile_screenshot_path": "screenshots/detailing-mobile.png",
                "visual_review_needed": "Yes",
                "visual_quality_score": "Weak",
            },
            "screenshots": {"desktop": "screenshots/detailing-desktop.png", "mobile": "screenshots/detailing-mobile.png"},
            "evidence": self._evidence_fixture(
                site_status="weak",
                evidence_items=[
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 130 reviews"},
                    {"id": "E2", "field": "cta_strength", "value": "Weak"},
                    {"id": "E3", "field": "estimate_form_detected", "value": "No"},
                    {"id": "E4", "field": "third_party_tools", "value": "ServiceTitan"},
                    {"id": "E5", "field": "screenshots", "value": {"desktop": "screenshots/detailing-desktop.png", "mobile": "screenshots/detailing-mobile.png"}},
                ],
            ),
            "buyer_intent": self._buyer_intent_fixture(),
            "safe_outreach_claims": [{"claim": "Your site mentions ceramic coating.", "evidence_refs": ["E2"]}],
        }

    def test_llm_scoring_record_preserves_full_enriched_text_but_compacts_prompt(self) -> None:
        from score_leads import build_api_llm_scoring_prompt, build_llm_scoring_record

        record = self._huge_detailing_record()
        original_combined = record["website"]["combined_text"]
        original_homepage = record["website"]["homepage"]["text"]
        original_pages_text = record["website"]["pages"]["about"]["text"]

        compact = build_llm_scoring_record(record, fallback_vertical="detailing")
        prompt = build_api_llm_scoring_prompt([compact], batch_id="batch_001")

        self.assertEqual(record["website"]["combined_text"], original_combined)
        self.assertEqual(record["website"]["homepage"]["text"], original_homepage)
        self.assertEqual(record["website"]["pages"]["about"]["text"], original_pages_text)
        self.assertNotIn(original_combined, prompt)
        self.assertNotIn("SHOULD_NOT_APPEAR_IN_COMPACT_PROMPT", prompt)
        self.assertIn("Ceramic coating packages include paint correction before installation.", prompt)
        self.assertIn("Paint protection film and PPF installs are available.", prompt)
        self.assertLess(len(json.dumps(compact, ensure_ascii=False)), 6000)

    def test_llm_scoring_record_preserves_screenshot_paths_and_visual_flags(self) -> None:
        from score_leads import build_llm_scoring_record

        compact = build_llm_scoring_record(self._huge_detailing_record(), fallback_vertical="detailing")

        self.assertEqual(compact["screenshots"]["desktop"], "screenshots/detailing-desktop.png")
        self.assertEqual(compact["screenshots"]["mobile"], "screenshots/detailing-mobile.png")
        self.assertEqual(compact["visual_review"]["visual_review_needed"], "Yes")
        self.assertEqual(compact["visual_review"]["visual_quality_score"], "Weak")

    def test_llm_scoring_record_always_includes_evidence_ref_options(self) -> None:
        from score_leads import build_llm_scoring_record

        record = {"google": {**self._google_fixture(), "website_url": ""}, "website": {}, "evidence": {}}

        compact = build_llm_scoring_record(record, fallback_vertical="detailing")

        options = compact["evidence_ref_options"]
        self.assertTrue(options)
        self.assertTrue(all(option.get("id") for option in options))
        self.assertIn("google_profile", {option["id"] for option in options})

    def test_evidence_ref_options_carry_only_id_and_label(self) -> None:
        # Slim the prompt: evidence_ref_options exist only so the LLM can copy an
        # id into evidence_refs. The proof text lives in selected_evidence_snippets
        # and the capsule fields, so snippet/source here are redundant tokens.
        from score_leads import build_llm_scoring_record, compact_evidence_ref_ids

        compact = build_llm_scoring_record(self._huge_detailing_record(), fallback_vertical="detailing")

        options = compact["evidence_ref_options"]
        self.assertTrue(options)
        for option in options:
            self.assertLessEqual(set(option.keys()), {"id", "label"})
            self.assertNotIn("snippet", option)
            self.assertNotIn("source", option)
        # ids are preserved so downstream evidence_ref validation still works
        self.assertTrue(compact_evidence_ref_ids(compact))

    def test_evidence_item_ref_options_only_include_selected_snippets(self) -> None:
        from score_leads import MAX_LLM_SCORING_SNIPPETS, build_llm_scoring_record

        record = {
            "google": {
                **self._google_fixture(),
                "business_name": "Evidence Heavy Detailer",
                "place_id": "ChIJEVIDENCE",
                "category": "auto_detailing",
            },
            "website": {"combined_text": ""},
            "evidence": self._evidence_fixture(
                site_status="weak",
                evidence_items=[
                    {
                        "id": f"E{i}",
                        "field": "cta_strength",
                        "value": f"Weak CTA proof {i}",
                        "source_url": f"https://example.com/{i}",
                    }
                    for i in range(MAX_LLM_SCORING_SNIPPETS + 3)
                ],
            ),
        }

        compact = build_llm_scoring_record(record, fallback_vertical="detailing")

        selected_ids = {
            item["id"]
            for item in compact["selected_evidence_snippets"]
            if item.get("kind") == "evidence_item"
        }
        offered_evidence_ids = {
            option["id"]
            for option in compact["evidence_ref_options"]
            if option["id"].startswith("E")
        }
        self.assertEqual(len(selected_ids), MAX_LLM_SCORING_SNIPPETS)
        self.assertEqual(offered_evidence_ids, selected_ids)
        self.assertNotIn(f"E{MAX_LLM_SCORING_SNIPPETS}", offered_evidence_ids)

    def test_buyer_intent_capsule_is_compacted_without_dropping_money_signal(self) -> None:
        # Raw buyer_intent duplicates buyer_intent_summary, the money_signal flag,
        # and provider_evidence.crm_booking_stack. Keep only the selected signals.
        from score_leads import build_llm_scoring_record

        compact = build_llm_scoring_record(self._huge_detailing_record(), fallback_vertical="detailing")

        buyer_intent = compact["buyer_intent"]
        self.assertNotIn("evidence", buyer_intent)
        self.assertNotIn("crm_booking_stack", buyer_intent)
        # money and demand signals still available to the scorer
        self.assertEqual(buyer_intent.get("money_signal"), "yes")
        self.assertEqual(compact["money_signal"], "Yes")
        self.assertEqual(buyer_intent.get("demand_signal", {}).get("state"), "yes")
        # The rubric has to be able to tell an unrun check from a negative one.
        self.assertEqual(
            compact["buyer_intent"].get("ads_not_checked"),
            ["google_ad_transparency", "meta_ad_library"],
        )
        self.assertTrue(compact["buyer_intent_summary"])
        self.assertEqual(
            compact["provider_evidence"]["crm_booking_stack"].get("detected_tools"),
            ["ServiceTitan"],
        )

    def test_api_llm_prompt_serializes_compact_records_minified(self) -> None:
        from score_leads import build_api_llm_scoring_prompt, build_llm_scoring_record

        compact = build_llm_scoring_record(self._huge_detailing_record(), fallback_vertical="detailing")
        prompt = build_api_llm_scoring_prompt([compact], batch_id="batch_001")

        records_block = prompt.split("compact_records:\n", 1)[1]
        # Minified JSON keeps the whole array on one line (no indent newlines).
        self.assertLessEqual(records_block.strip().count("\n"), 1)
        self.assertNotIn(': "', records_block)  # compact separators drop the ": " spacing

    def test_score_batch_with_debug_charges_failed_attempt_usage(self) -> None:
        # A validation-failed first attempt is a real paid call; its usage must be
        # added to the repaired result so token/USD caps see the true spend.
        import score_leads
        from lead_machine import llm_client

        compact_records = [
            {"score_file": "phone-123.json", "lead": {"business_name": "X"},
             "evidence_ref_options": [{"id": "google_profile", "label": "Google profile"}]}
        ]
        failed = llm_client.InvalidLlmResponse(
            "validation failed",
            raw_text="{}",
            parsed_json={"batch_id": "b", "scores": []},
            usage={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
        )
        repaired = {"bundle": {"batch_id": "b", "scores": []},
                    "usage": {"input_tokens": 800, "output_tokens": 150, "total_tokens": 950}}

        calls = {"n": 0}

        def fake_score_batch(prompt, *, model, max_tokens, **_):
            calls["n"] += 1
            if calls["n"] == 1:
                raise failed
            return dict(repaired)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(score_leads.llm_client, "score_batch", side_effect=fake_score_batch), \
                 patch.object(score_leads, "build_validation_repair_prompt", return_value="repair"), \
                 patch.object(score_leads, "_write_validation_failed_llm_response", return_value=Path(tmp) / "dbg.json"):
                result = score_leads._score_batch_with_debug(
                    "prompt", batch_id="b", bundle_dir=Path(tmp),
                    compact_records=compact_records, model="m", max_tokens=4096,
                )

        self.assertEqual(
            result["usage"],
            {"input_tokens": 1800, "output_tokens": 350, "total_tokens": 2150},
        )
        self.assertEqual(result["call_count"], 2)

    def test_score_batch_with_debug_counts_usage_when_repair_also_fails(self) -> None:
        # Both attempts are real paid calls, so the caps must see them even
        # though the batch produced nothing. This used to be asserted on the
        # evidence_refs backfill's return value; that backfill minted citations
        # the model declined to make and was removed, so the same property is
        # now pinned on the error the failed batch raises.
        import score_leads
        from lead_machine import llm_client

        compact_records = [{"score_file": "phone-123.json", "lead": {"business_name": "X"},
                            "evidence_ref_options": [{"id": "google_profile", "label": "Google profile"}]}]
        failed = llm_client.InvalidLlmResponse(
            "validation failed", raw_text="{}", parsed_json={"batch_id": "b", "scores": []},
            usage={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
        )
        repair_failed = llm_client.InvalidLlmResponse(
            "still bad", raw_text="{}", parsed_json={"batch_id": "b", "scores": []},
            usage={"input_tokens": 700, "output_tokens": 100, "total_tokens": 800},
        )

        calls = {"n": 0}

        def fake_score_batch(prompt, *, model, max_tokens, **_):
            calls["n"] += 1
            raise failed if calls["n"] == 1 else repair_failed

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(score_leads.llm_client, "score_batch", side_effect=fake_score_batch), \
                 patch.object(score_leads, "build_validation_repair_prompt", return_value="repair"), \
                 patch.object(score_leads, "_write_validation_failed_llm_response", return_value=Path(tmp) / "dbg.json"):
                with self.assertRaises(RuntimeError) as caught:
                    score_leads._score_batch_with_debug(
                        "prompt", batch_id="b", bundle_dir=Path(tmp),
                        compact_records=compact_records, model="m", max_tokens=4096,
                    )

        self.assertEqual(
            caught.exception.usage,
            {"input_tokens": 1700, "output_tokens": 300, "total_tokens": 2000},
        )
        self.assertEqual(caught.exception.call_count, 2)

    def test_default_provider_chain_is_subscription_only(self) -> None:
        """The unattended default must not be able to spend money or leak PII.

        Two properties, one tuple. Every default member must be a $0
        subscription runner, so (a) an unattended run cannot bill anything
        regardless of cap configuration, and (b) prospect PII — street address,
        phone, raw page-text windows — cannot reach a third-party API by
        default. A paid member anywhere in the chain loses both the moment the
        subscription quota exhausts and the run falls through to it.
        """
        from lead_machine import llm_client
        from score_leads import llm_pricing

        self.assertEqual(llm_client.DEFAULT_PROVIDER_CHAIN[0], "codex")
        for provider in llm_client.DEFAULT_PROVIDER_CHAIN:
            model = llm_client.model_for_provider(provider)
            self.assertTrue(
                model.startswith("subscription:"),
                f"default chain member {provider!r} resolves to {model!r}, not a subscription runner",
            )
            self.assertEqual(
                llm_pricing(model), (0.0, 0.0),
                f"default chain member {provider!r} is priced and can bill an unattended run",
            )

    def test_llm_client_has_exactly_one_outbound_http_seam(self) -> None:
        """Every provider must POST through `_post`, never `requests.post`.

        This is a test-integrity invariant, not a style rule. The suite patches
        `lead_machine.llm_client._post` to keep scoring tests offline. When the
        pooled session first landed and the providers stopped calling
        `requests.post`, nine tests that patched the old name kept passing their
        patch to nothing and made *live* Anthropic and Moonshot calls instead —
        billed, non-deterministic, and green. A second seam re-opens that hole
        silently, so pin that only one exists.
        """
        import ast

        source = Path("lead_machine/llm_client.py").read_text(encoding="utf-8")
        offenders = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            rendered = ast.unparse(func)
            # Anything reached through the `requests` module, plus any `.post(`
            # on some other receiver (a Session held elsewhere, say). Bare
            # `.get(` is excluded deliberately — it is dict access here, not HTTP.
            is_http = rendered.startswith("requests.") or func.attr == "post"
            if not is_http:
                continue
            # Constructing the pooled Session is not an outbound call.
            if rendered == "requests.Session":
                continue
            # `_session().post(...)` inside the `_post` wrapper is the one seam.
            if isinstance(func.value, ast.Call) and getattr(func.value.func, "id", "") == "_session":
                continue
            offenders.append(f"line {node.lineno}: {rendered}")

        self.assertEqual(
            offenders,
            [],
            "llm_client must make outbound HTTP only via _post(); found "
            + "; ".join(offenders),
        )

    def test_api_llm_skips_partition_cap_stops_from_batch_failures(self) -> None:
        # One list carries two unrelated causes. Reported under the single heading
        # "Records skipped by LLM caps", a `batch_failed` lead reads as a budget
        # stop — so an operator judging batch completeness concludes the run ran
        # out of money and the leads are fine to re-score, when in fact the model
        # failed on them and the run still exited 0.
        import score_leads

        skips = [
            score_leads.ApiLlmSkip(lead_name="A", score_file="a.json",
                                   reason="single_record_exceeds_token_cap"),
            score_leads.ApiLlmSkip(lead_name="B", score_file="b.json", reason="batch_failed"),
            score_leads.ApiLlmSkip(lead_name="C", score_file="c.json", reason="batch_failed"),
        ]

        cap_skips, failures = score_leads.partition_api_llm_skips(skips)

        self.assertEqual([s.score_file for s in cap_skips], ["a.json"])
        self.assertEqual([s.score_file for s in failures], ["b.json", "c.json"])
        self.assertEqual(score_leads.partition_api_llm_skips(None), ([], []))

    def test_api_llm_skips_partition_treats_unknown_reasons_as_failures(self) -> None:
        # A reason nobody has classified yet must not be reported as a cap stop.
        # Cap stops are the benign half; defaulting an unknown cause into the
        # benign bucket is how the mislabelling happened in the first place.
        import score_leads

        skips = [score_leads.ApiLlmSkip(lead_name="Z", score_file="z.json",
                                        reason="something_new_and_unclassified")]

        cap_skips, failures = score_leads.partition_api_llm_skips(skips)

        self.assertEqual(cap_skips, [])
        self.assertEqual([s.score_file for s in failures], ["z.json"])

    def test_score_batch_with_debug_reports_truncation_retry_in_debug_notes(self) -> None:
        # The truncation retry is a second billed call. It printed to stdout and
        # dropped an `invalid_response_attempt_1.txt` into _api_bundles/, but it
        # never entered the run's own debug_notes — so the run reported the repair
        # retries and stayed silent about these, leaving a quarter of retry spend
        # invisible in the artifact an operator actually reviews.
        import score_leads
        from lead_machine import llm_client

        compact_records = [{"score_file": "phone-123.json", "lead": {"business_name": "X"},
                            "evidence_ref_options": [{"id": "google_profile", "label": "Google profile"}]}]
        truncated = llm_client.InvalidLlmResponse(
            "Unterminated string starting at line 4",
            raw_text='{"scores": [',
            parsed_json=None,
            usage={"input_tokens": 900, "output_tokens": 4096, "total_tokens": 4996},
        )
        good = {"bundle": {"batch_id": "b", "scores": []},
                "usage": {"input_tokens": 900, "output_tokens": 300, "total_tokens": 1200}}

        calls = {"n": 0}

        def fake_score_batch(prompt, *, model, max_tokens, **_):
            calls["n"] += 1
            if calls["n"] == 1:
                raise truncated
            return dict(good)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(score_leads.llm_client, "score_batch", side_effect=fake_score_batch):
                result = score_leads._score_batch_with_debug(
                    "prompt", batch_id="batch_010", bundle_dir=Path(tmp),
                    compact_records=compact_records, model="m", max_tokens=4096,
                )

        self.assertEqual(result["call_count"], 2)
        notes = result.get("debug_notes") or []
        self.assertTrue(
            any("batch_010" in note and "truncat" in note.lower() for note in notes),
            f"truncation retry must be named in debug_notes; got {notes!r}",
        )

    def test_chain_exhaustion_still_charges_the_calls_the_batch_already_made(self) -> None:
        # `All LLM scoring providers unavailable` arrives as a bare RuntimeError
        # with no usage and no call_count, so _bank_failed_batch banked zero for
        # a batch that had already paid for calls. That undercounts
        # MAX_LLM_CALLS_PER_RUN and MAX_LLM_TOKENS_PER_RUN by exactly this
        # batch's spend — a cap hole, not just a reporting gap. The retry note
        # rides the same exception and was dropped with it.
        import score_leads
        from lead_machine import llm_client

        compact_records = [{"score_file": "phone-123.json", "lead": {"business_name": "X"},
                            "evidence_ref_options": [{"id": "google_profile", "label": "Google profile"}]}]
        truncated = llm_client.InvalidLlmResponse(
            "Unterminated string starting at line 4", raw_text='{"scores": [', parsed_json=None,
            usage={"input_tokens": 900, "output_tokens": 4096, "total_tokens": 4996},
        )

        calls = {"n": 0}

        def fake_score_batch(prompt, *, model, max_tokens, **_):
            calls["n"] += 1
            if calls["n"] == 1:
                raise truncated
            raise RuntimeError("All LLM scoring providers unavailable: kimi: down; codex: quota")

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(score_leads.llm_client, "score_batch", side_effect=fake_score_batch):
                with self.assertRaises(RuntimeError) as caught:
                    score_leads._score_batch_with_debug(
                        "prompt", batch_id="batch_022", bundle_dir=Path(tmp),
                        compact_records=compact_records, model="m", max_tokens=4096,
                    )

        exc = caught.exception
        self.assertEqual(exc.call_count, 2)
        self.assertEqual(exc.usage["output_tokens"], 4096)
        self.assertTrue(
            any("batch_022" in note for note in getattr(exc, "debug_notes", None) or []),
            "retry note must survive chain exhaustion",
        )

    def test_score_batch_with_debug_carries_retry_notes_onto_the_failure(self) -> None:
        # A batch that retried and *then* failed is the worst reporting case: the
        # retry was billed and the batch produced nothing. The note has to ride the
        # exception so `_record_batch_failure` can bank it into the run's notes.
        import score_leads
        from lead_machine import llm_client

        compact_records = [{"score_file": "phone-123.json", "lead": {"business_name": "X"},
                            "evidence_ref_options": [{"id": "google_profile", "label": "Google profile"}]}]
        truncated = llm_client.InvalidLlmResponse(
            "Unterminated string starting at line 4", raw_text='{"scores": [', parsed_json=None,
            usage={"input_tokens": 900, "output_tokens": 4096, "total_tokens": 4996},
        )
        still_bad = llm_client.InvalidLlmResponse(
            "not json at all", raw_text="???", parsed_json=None,
            usage={"input_tokens": 900, "output_tokens": 50, "total_tokens": 950},
        )

        calls = {"n": 0}

        def fake_score_batch(prompt, *, model, max_tokens, **_):
            calls["n"] += 1
            raise truncated if calls["n"] == 1 else still_bad

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(score_leads.llm_client, "score_batch", side_effect=fake_score_batch):
                with self.assertRaises(ValueError) as caught:
                    score_leads._score_batch_with_debug(
                        "prompt", batch_id="batch_015", bundle_dir=Path(tmp),
                        compact_records=compact_records, model="m", max_tokens=4096,
                    )

        notes = getattr(caught.exception, "debug_notes", None) or []
        self.assertTrue(
            any("batch_015" in note and "truncat" in note.lower() for note in notes),
            f"retry note must ride the failure; got {notes!r}",
        )

    def test_api_llm_prompt_requires_non_empty_evidence_refs_from_options(self) -> None:
        from score_leads import build_api_llm_scoring_prompt, build_llm_scoring_record

        compact = build_llm_scoring_record(self._huge_detailing_record(), fallback_vertical="detailing")
        prompt = build_api_llm_scoring_prompt([compact], batch_id="batch_001")

        self.assertIn("evidence_refs is REQUIRED for every score", prompt)
        self.assertIn("non-empty array of 1-3 strings", prompt)
        self.assertIn("copied exactly from that lead's evidence_ref_options[].id", prompt)
        self.assertIn("never output [] for evidence_refs", prompt)

    def test_api_llm_prompt_uses_shared_sendability_rules(self) -> None:
        from score_leads import build_api_llm_scoring_prompt, build_llm_scoring_record

        compact = build_llm_scoring_record(self._huge_detailing_record(), fallback_vertical="detailing")
        prompt = build_api_llm_scoring_prompt([compact], batch_id="batch_001")

        self.assertIn(
            "Missing site plus strong reviews can be an A lead only when there is a concrete conversion gap and enough evidence.",
            prompt,
        )
        self.assertIn("Do not score A/sendable just because the business is reputable.", prompt)
        self.assertIn(
            "mature plus an obvious broken/missing estimate path can still be high value when backed by money intent or very strong conversion-pain evidence.",
            prompt,
        )

    def test_api_llm_merge_accepts_lead_compact_evidence_refs_only(self) -> None:
        from score_leads import score_records_api_run

        first = self._huge_detailing_record(name="Buyer Intent Detail", place_id="ChIJ123")
        buyer_intent_score = self._manual_score(
            total_score=75,
            grade="B",
            evidence_refs=["buyer_intent"],
            safe_outreach_claims=[
                {"claim": "Your buyer-intent signals show active demand.", "evidence_refs": ["buyer_intent"]}
            ],
        )
        buyer_intent_bundle = {
            "batch_id": "batch_001",
            "scores": [{"score_file": "chij123.json", "score": buyer_intent_score}],
        }

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            with patch(
                "score_leads.llm_client.score_batch",
                return_value={
                    "bundle": buyer_intent_bundle,
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                },
            ):
                run = score_records_api_run(
                    [first],
                    scores_dir=scores_dir,
                    fallback_vertical="detailing",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=4096,
                    max_calls=0,
                    max_tokens_per_run=100_000,
                    max_usd_per_run=0.0,
                )

        self.assertEqual(run.manual_run.rows[0]["Evidence Refs"], json.dumps(["buyer_intent"]))
        self.assertNotIn("Invalid evidence reference", run.manual_run.rows[0]["Send Blockers"])

        second = self._huge_detailing_record(name="Fake Ref Detail", place_id="ChIJ456")
        fake_ref_score = self._manual_score(
            total_score=75,
            grade="B",
            evidence_refs=["fake_unknown_ref"],
            safe_outreach_claims=[
                {"claim": "Your buyer-intent signals show active demand.", "evidence_refs": ["fake_unknown_ref"]}
            ],
        )
        valid_first_bundle = {
            "batch_id": "batch_001",
            "scores": [{"score_file": "chij123.json", "score": buyer_intent_score}],
        }
        fake_ref_bundle = {
            "batch_id": "batch_002",
            "scores": [{"score_file": "chij456.json", "score": fake_ref_score}],
        }

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            with patch(
                "score_leads.llm_client.score_batch",
                side_effect=[
                    {
                        "bundle": valid_first_bundle,
                        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    },
                    {
                        "bundle": fake_ref_bundle,
                        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    },
                ],
            ):
                with self.assertRaises(ValueError) as caught:
                    score_records_api_run(
                        [first, second],
                        scores_dir=scores_dir,
                        fallback_vertical="detailing",
                        batch_size=1,
                        model="claude-sonnet-5",
                        max_tokens=4096,
                        max_calls=0,
                        max_tokens_per_run=100_000,
                        max_usd_per_run=0.0,
                    )

            self.assertTrue((scores_dir / "chij123.json").exists())
            debug_path = scores_dir / "_api_bundles" / "merge_validation_failed_chij456.json"
            self.assertTrue(debug_path.exists())
            debug_payload = json.loads(debug_path.read_text(encoding="utf-8"))

        message = str(caught.exception)
        self.assertIn("Unknown evidence_refs: fake_unknown_ref", message)
        self.assertIn("API merge validation debug written", message)
        self.assertEqual(debug_payload["lead_id"], "ChIJ456")
        self.assertEqual(debug_payload["offending_evidence_refs"], ["fake_unknown_ref"])
        self.assertIn("buyer_intent", debug_payload["allowed_refs"])
        self.assertNotIn("fake_unknown_ref", debug_payload["allowed_refs"])

    def test_api_llm_merge_accepts_ev_refs_shown_in_compact_capsule_text_only(self) -> None:
        from score_leads import build_llm_scoring_record, score_records_api_run

        record = self._huge_detailing_record(name="Capsule Ref Detail", place_id="ChIJ789")
        record["website"]["combined_text"] = (
            "Ceramic coating evidence ev-services_detected-9 appears in the compact capsule text only."
        )
        record["website"]["homepage"]["text"] = record["website"]["combined_text"]
        compact = build_llm_scoring_record(record, fallback_vertical="detailing")

        self.assertIn("ev-services_detected-9", json.dumps(compact, ensure_ascii=False))
        self.assertIn(
            "ev-services_detected-9",
            {option["id"] for option in compact["evidence_ref_options"]},
        )

        shown_ref_score = self._manual_score(
            total_score=75,
            grade="B",
            evidence_refs=["ev-services_detected-9"],
            safe_outreach_claims=[
                {"claim": "Your site mentions ceramic coating.", "evidence_refs": ["ev-services_detected-9"]}
            ],
        )
        fake_ref_score = self._manual_score(
            total_score=75,
            grade="B",
            evidence_refs=["fake_unknown_ref"],
            safe_outreach_claims=[
                {"claim": "Your site mentions ceramic coating.", "evidence_refs": ["fake_unknown_ref"]}
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            with patch(
                "score_leads.llm_client.score_batch",
                return_value={
                    "bundle": {
                        "batch_id": "batch_001",
                        "scores": [{"score_file": "chij789.json", "score": shown_ref_score}],
                    },
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                },
            ):
                run = score_records_api_run(
                    [record],
                    scores_dir=scores_dir,
                    fallback_vertical="detailing",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=4096,
                    max_calls=0,
                    max_tokens_per_run=100_000,
                    max_usd_per_run=0.0,
                )

        self.assertEqual(run.manual_run.rows[0]["Evidence Refs"], json.dumps(["ev-services_detected-9"]))

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            with patch(
                "score_leads.llm_client.score_batch",
                return_value={
                    "bundle": {
                        "batch_id": "batch_001",
                        "scores": [{"score_file": "chij789.json", "score": fake_ref_score}],
                    },
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                },
            ):
                with self.assertRaises(ValueError) as caught:
                    score_records_api_run(
                        [record],
                        scores_dir=scores_dir,
                        fallback_vertical="detailing",
                        batch_size=1,
                        model="claude-sonnet-5",
                        max_tokens=4096,
                        max_calls=0,
                        max_tokens_per_run=100_000,
                        max_usd_per_run=0.0,
                    )

        self.assertIn("Unknown evidence_refs: fake_unknown_ref", str(caught.exception))

    def test_api_llm_compact_prompt_for_ten_huge_leads_stays_under_run_token_cap(self) -> None:
        from score_leads import build_api_llm_scoring_prompt, build_llm_scoring_record, estimate_prompt_tokens

        records = [
            build_llm_scoring_record(
                self._huge_detailing_record(name=f"Detailing {index}", place_id=f"ChIJ{index}"),
                fallback_vertical="detailing",
            )
            for index in range(10)
        ]
        prompt = build_api_llm_scoring_prompt(records, batch_id="batch_001")

        self.assertLess(estimate_prompt_tokens(prompt), 100_000)

    def test_api_llm_limit_ten_makes_at_least_one_call_under_normal_caps(self) -> None:
        from score_leads import score_records_api_run

        records = [
            self._huge_detailing_record(name=f"Detailing {index}", place_id=f"ChIJ{index}")
            for index in range(10)
        ]

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int, **_: object) -> dict:
            score_files = sorted(set(re.findall(r'"score_file": "([^"]+\.json)"', prompt)))
            return {
                "bundle": {
                    "batch_id": "batch_001",
                    "scores": [
                        {"score_file": score_file, "score": self._manual_score(total_score=75, grade="B")}
                        for score_file in score_files
                    ],
                },
                "usage": {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
            }

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch) as score_batch:
                run = score_records_api_run(
                    records,
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="detailing",
                    batch_size=5,
                    model="claude-sonnet-5",
                    max_tokens=4096,
                    max_calls=0,
                    max_tokens_per_run=100_000,
                    max_usd_per_run=0.0,
                )

        self.assertGreaterEqual(score_batch.call_count, 1)
        self.assertGreaterEqual(run.calls, 1)

    def test_api_llm_shrinks_chunk_when_projected_prompt_exceeds_token_cap(self) -> None:
        from score_leads import build_api_llm_scoring_prompt, build_llm_scoring_record, estimate_prompt_tokens, score_records_api_run

        first = self._huge_detailing_record(name="First Detail", place_id="ChIJ1")
        second = self._huge_detailing_record(name="Second Detail", place_id="ChIJ2")
        single_prompt = build_api_llm_scoring_prompt(
            [build_llm_scoring_record(first, fallback_vertical="detailing")],
            batch_id="batch_001",
        )
        double_prompt = build_api_llm_scoring_prompt(
            [
                build_llm_scoring_record(first, fallback_vertical="detailing"),
                build_llm_scoring_record(second, fallback_vertical="detailing"),
            ],
            batch_id="batch_001",
        )
        run_cap = estimate_prompt_tokens(single_prompt) + 20 + 50
        self.assertLess(run_cap, estimate_prompt_tokens(double_prompt) + 20)

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int, **_: object) -> dict:
            score_files = sorted(set(re.findall(r'"score_file": "([^"]+\.json)"', prompt)))
            return {
                "bundle": {
                    "batch_id": "batch_001",
                    "scores": [
                        {"score_file": score_file, "score": self._manual_score(total_score=75, grade="B")}
                        for score_file in score_files
                    ],
                },
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            }

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch) as score_batch:
                run = score_records_api_run(
                    [first, second],
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="detailing",
                    batch_size=2,
                    model="claude-sonnet-5",
                    max_tokens=20,
                    max_calls=0,
                    max_tokens_per_run=run_cap,
                    max_usd_per_run=0.0,
                )

        self.assertEqual(score_batch.call_count, 2)
        self.assertEqual(run.calls, 2)
        self.assertFalse(run.stopped_by_cap)

    def test_api_llm_skips_single_record_that_exceeds_token_cap(self) -> None:
        from score_leads import score_records_api_run

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch") as score_batch:
                run = score_records_api_run(
                    [self._huge_detailing_record()],
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="detailing",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=20,
                    max_calls=0,
                    max_tokens_per_run=10,
                    max_usd_per_run=0.0,
                )

        score_batch.assert_not_called()
        self.assertEqual(len(run.skipped_records or []), 1)
        self.assertEqual((run.skipped_records or [])[0].reason, "single_record_exceeds_token_cap")

    def test_api_llm_invalid_json_writes_raw_debug_output_and_skips_the_batch(self) -> None:
        """The raw body must land on disk and the batch must be skipped, not fatal.

        This used to assert the run raised. It no longer does: a batch that stays
        unparseable after its retry and repair paths is contained to that batch
        (see BatchFailureIsolationTest), so the debug artifact and the reason
        reach the caller via `debug_notes`/`skipped_records` instead of a
        traceback. The artifact contract itself is unchanged.
        """
        from lead_machine.llm_client import InvalidLlmResponse
        from score_leads import score_records_api_run

        raw_text = '{"batch_id":"batch_001","scores":[{"score_file":"chij123.json","score":{"opener":"unterminated'

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            with patch(
                "score_leads.llm_client.score_batch",
                side_effect=InvalidLlmResponse("Invalid LLM score bundle JSON: Unterminated string", raw_text=raw_text),
            ):
                run = score_records_api_run(
                    [self._huge_detailing_record()],
                    scores_dir=scores_dir,
                    fallback_vertical="detailing",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=8192,
                    max_calls=0,
                    max_tokens_per_run=100_000,
                    max_usd_per_run=0.0,
                )

            debug_path = scores_dir / "_api_bundles" / "batch_001_invalid_response_attempt_1.txt"
            self.assertTrue(debug_path.exists())
            self.assertEqual(debug_path.read_text(encoding="utf-8"), raw_text)
            self.assertIn(str(debug_path), " ".join(run.debug_notes))
            self.assertEqual([skip.reason for skip in run.skipped_records], ["batch_failed"])

    def test_api_llm_terminal_failure_writes_partial_usage_output(self) -> None:
        from lead_machine.llm_client import InvalidLlmResponse
        from score_leads import score_records_api_run

        usage = {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200}

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            usage_path = Path(tmp) / "usage.json"
            with patch(
                "score_leads.llm_client.score_batch",
                side_effect=InvalidLlmResponse(
                    "terminal non-repairable error",
                    raw_text='{"bad": true}',
                    usage=usage,
                ),
            ):
                run = score_records_api_run(
                    [self._huge_detailing_record()],
                    scores_dir=scores_dir,
                    fallback_vertical="detailing",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=4096,
                    max_calls=0,
                    max_tokens_per_run=100_000,
                    max_usd_per_run=0.0,
                    usage_output_path=usage_path,
                )

            # A failed attempt is still a paid attempt: it must be charged against
            # the run's caps whether or not the run goes on to finish.
            payload = json.loads(usage_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["calls"], 1)
            self.assertEqual(payload["input_tokens"], 1000)
            self.assertEqual(payload["output_tokens"], 200)
            self.assertEqual(payload["total_tokens"], 1200)
            self.assertTrue(payload["partial"])
            self.assertEqual([skip.reason for skip in run.skipped_records], ["batch_failed"])

    def test_api_llm_no_text_response_retries_with_larger_token_cap(self) -> None:
        from lead_machine.llm_client import InvalidLlmResponse
        from score_leads import score_records_api_run

        raw_text = json.dumps({"content": [{"type": "thinking", "thinking": "no final text yet"}]})

        calls = 0

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int, **_: object) -> dict:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise InvalidLlmResponse("Anthropic response did not include text content", raw_text=raw_text)
            score_files = sorted(set(re.findall(r'"score_file": "([^"]+\.json)"', prompt)))
            return {
                "bundle": {
                    "batch_id": "batch_001",
                    "scores": [
                        {"score_file": score_file, "score": self._manual_score(total_score=75, grade="B")}
                        for score_file in score_files
                    ],
                },
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            with patch(
                "score_leads.llm_client.score_batch",
                side_effect=fake_score_batch,
            ) as score_batch:
                run = score_records_api_run(
                    [self._huge_detailing_record()],
                    scores_dir=scores_dir,
                    fallback_vertical="detailing",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=4096,
                    max_calls=0,
                    max_tokens_per_run=100_000,
                    max_usd_per_run=0.0,
                )

            debug_path = scores_dir / "_api_bundles" / "batch_001_invalid_response_attempt_1.txt"
            self.assertTrue(debug_path.exists())
            self.assertEqual(debug_path.read_text(encoding="utf-8"), raw_text)
            self.assertEqual(score_batch.call_count, 2)
            self.assertEqual(score_batch.call_args_list[1].kwargs["max_tokens"], 8192)
            self.assertEqual(len(run.manual_run.rows), 1)

    def test_api_llm_validation_failure_repairs_with_evidence_ref_options(self) -> None:
        from lead_machine.llm_client import InvalidLlmResponse
        from score_leads import score_records_api_run

        invalid_score = self._manual_score(total_score=75, grade="B", evidence_refs=[])
        invalid_score["safe_outreach_claims"] = [
            {"claim": "Your Google profile shows strong reviews.", "evidence_refs": ["E1"]}
        ]
        repaired_score = self._manual_score(total_score=75, grade="B", evidence_refs=["E1"])
        invalid_bundle = {
            "batch_id": "batch_001",
            "scores": [{"score_file": "chij123.json", "score": invalid_score}],
        }
        repaired_bundle = {
            "batch_id": "batch_001",
            "scores": [{"score_file": "chij123.json", "score": repaired_score}],
        }
        raw_text = json.dumps(invalid_bundle)

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            with patch(
                "score_leads.llm_client.score_batch",
                side_effect=[
                    InvalidLlmResponse(
                        "Validation failures:\n- scores[0] (chij123.json): evidence_refs must be a non-empty list",
                        raw_text=raw_text,
                        parsed_json=invalid_bundle,
                    ),
                    {"bundle": repaired_bundle, "usage": {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12}},
                ],
            ) as score_batch:
                run = score_records_api_run(
                    [self._huge_detailing_record()],
                    scores_dir=scores_dir,
                    fallback_vertical="detailing",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=4096,
                    max_calls=0,
                    max_tokens_per_run=100_000,
                    max_usd_per_run=0.0,
                )

        self.assertEqual(score_batch.call_count, 2)
        repair_prompt = score_batch.call_args_list[1].kwargs.get("prompt") or score_batch.call_args_list[1].args[0]
        self.assertIn("Validation failures:", repair_prompt)
        self.assertIn(raw_text, repair_prompt)
        self.assertIn("evidence_ref_options", repair_prompt)
        self.assertEqual(run.manual_run.rows[0]["Evidence Refs"], json.dumps(["E1"]))

    def test_api_llm_validation_failure_writes_raw_debug_output(self) -> None:
        from lead_machine.llm_client import InvalidLlmResponse
        from score_leads import score_records_api_run

        invalid_score = self._manual_score(confidence="certain", evidence_refs=[])
        invalid_bundle = {
            "batch_id": "batch_001",
            "scores": [{"score_file": "chij123.json", "score": invalid_score}],
        }
        raw_text = json.dumps(invalid_bundle)

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            with patch(
                "score_leads.llm_client.score_batch",
                side_effect=[
                    InvalidLlmResponse(
                        "Validation failures:\n- scores[0] (chij123.json): confidence must be low, medium, or high",
                        raw_text=raw_text,
                        parsed_json=invalid_bundle,
                    ),
                    InvalidLlmResponse(
                        "Validation failures:\n- scores[0] (chij123.json): confidence must be low, medium, or high",
                        raw_text=raw_text,
                        parsed_json=invalid_bundle,
                    ),
                ],
            ):
                run = score_records_api_run(
                    [self._huge_detailing_record()],
                    scores_dir=scores_dir,
                    fallback_vertical="detailing",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=4096,
                    max_calls=0,
                    max_tokens_per_run=100_000,
                    max_usd_per_run=0.0,
                )

            debug_path = scores_dir / "_api_bundles" / "batch_001_validation_failed_attempt_1.json"
            self.assertTrue(debug_path.exists())
            self.assertEqual(debug_path.read_text(encoding="utf-8"), raw_text)
            # Same diagnostic content as when this was a raised RuntimeError —
            # it now reaches the operator through the run's debug notes.
            message = " ".join(run.debug_notes)
            self.assertIn("batch_001", message)
            self.assertIn("confidence must be low, medium, or high", message)
            self.assertIn(str(debug_path), message)
            self.assertEqual([skip.reason for skip in run.skipped_records], ["batch_failed"])

    def test_api_llm_preserves_successful_first_batch_when_second_batch_validation_fails(self) -> None:
        from lead_machine.llm_client import InvalidLlmResponse
        from score_leads import score_records_api_run

        first = self._huge_detailing_record(name="First Detail", place_id="ChIJ123")
        second = self._huge_detailing_record(name="Second Detail", place_id="ChIJ456")
        first_bundle = {
            "batch_id": "batch_001",
            "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}],
        }
        invalid_bundle = {
            "batch_id": "batch_002",
            "scores": [{"score_file": "chij456.json", "score": self._manual_score(confidence="certain", evidence_refs=[])}],
        }

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            with patch(
                "score_leads.llm_client.score_batch",
                side_effect=[
                    {"bundle": first_bundle, "usage": {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}},
                    InvalidLlmResponse(
                        "Validation failures:\n- scores[0] (chij456.json): confidence must be low, medium, or high",
                        raw_text=json.dumps(invalid_bundle),
                        parsed_json=invalid_bundle,
                    ),
                    InvalidLlmResponse(
                        "Validation failures:\n- scores[0] (chij456.json): confidence must be low, medium, or high",
                        raw_text=json.dumps(invalid_bundle),
                        parsed_json=invalid_bundle,
                    ),
                ],
            ):
                run = score_records_api_run(
                    [first, second],
                    scores_dir=scores_dir,
                    fallback_vertical="detailing",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=4096,
                    max_calls=0,
                    max_tokens_per_run=100_000,
                    max_usd_per_run=0.0,
                )

            self.assertTrue((scores_dir / "_api_bundles" / "batch_001_response.json").exists())
            self.assertTrue((scores_dir / "chij123.json").exists())
            self.assertTrue((scores_dir / "_api_bundles" / "batch_002_validation_failed_attempt_1.json").exists())
            # The preservation this test is named for now reaches the output too:
            # the good batch becomes a scored row instead of dying with the run.
            self.assertEqual([row["Business Name"] for row in run.manual_run.rows], ["First Detail"])
            self.assertEqual([skip.lead_name for skip in run.skipped_records], ["Second Detail"])

    def test_manual_score_loader_accepts_new_schema_and_normalizes_mismatched_total(self) -> None:
        raw_score = self._manual_score(total_score=1, grade="Skip")

        with tempfile.TemporaryDirectory() as tmp:
            score_path = Path(tmp) / "lead.json"
            score_path.write_text(json.dumps(raw_score), encoding="utf-8")
            validated = load_manual_score(score_path, allowed_evidence_ids={"E1", "E2"})

        self.assertEqual(validated["total_score"], 75)
        self.assertEqual(validated["grade"], "B")
        self.assertEqual(validated["confidence"], "medium")

    def test_manual_score_loader_rejects_versionless_score(self) -> None:
        raw_score = self._manual_score()
        raw_score.pop("score_schema_version", None)

        with tempfile.TemporaryDirectory() as tmp:
            score_path = Path(tmp) / "lead.json"
            score_path.write_text(json.dumps(raw_score), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "score_schema_version must be 2"):
                load_manual_score(score_path, allowed_evidence_ids={"E1", "E2"})

    def test_partially_populated_unversioned_score_is_rejected(self) -> None:
        raw_score = self._manual_score()
        raw_score.pop("score_schema_version")
        with tempfile.TemporaryDirectory() as tmp:
            score_path = Path(tmp) / "lead.json"
            score_path.write_text(json.dumps(raw_score), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "score_schema_version must be 2"):
                load_manual_score(score_path, allowed_evidence_ids={"E1", "E2"})

    def test_refresh_row_rejects_legacy_score(self) -> None:
        with self.assertRaisesRegex(SchemaMigrationError, "current schema v2 required"):
            refresh_row({"Place Key": "old", "Sendable": "Yes", "Packet Eligible": "Yes"})

    def test_missing_or_misspelled_contact_eligibility_holds_with_state_error(self) -> None:
        enriched = {
            "google": {**self._google_fixture(), "reviews": "18"},
            "buyer_intent": self._buyer_intent_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        row = merge_manual_score(enriched, self._manual_score(), fallback_vertical="hardscaping")
        row["Contact Eligibility"] = ""
        row["contact_eligibility"] = "contact-no"
        held = apply_sendability(row)
        self.assertEqual(held["Contact Eligibility"], "hold-commercial-case")
        self.assertEqual(held["Sendable"], "No")
        self.assertIn("invalid contact_eligibility contact-no; held", held["Send Blockers"])

    def test_pass_asset_receipt_propagates_to_both_aliases(self) -> None:
        enriched = {
            "google": {**self._google_fixture(), "reviews": "18"},
            "buyer_intent": self._buyer_intent_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        row = merge_manual_score(
            enriched,
            self._manual_score(
                asset_state="verified-existing",
                promise_state="existing-verified-asset",
                asset_id="asset-verified-1",
                asset_verifier_verdict="PASS",
            ),
            fallback_vertical="hardscaping",
        )
        self.assertEqual(row["Asset ID"], "asset-verified-1")
        self.assertEqual(row["asset_id"], "asset-verified-1")
        self.assertEqual(row["Asset Verifier Verdict"], "PASS")
        self.assertEqual(row["asset_verifier_verdict"], "PASS")

    def test_single_lead_prompt_uses_shared_v2_commercial_schema(self) -> None:
        prompt = build_llm_prompt(
            {"google": self._google_fixture(), "evidence": self._evidence_fixture(site_status="weak")},
            fallback_vertical="hardscaping",
        )
        self.assertIn('"score_schema_version": 2', prompt)
        self.assertIn('"asset_id": ""', prompt)
        self.assertIn('"asset_verifier_verdict": ""', prompt)
        self.assertIn("all nine commercial dimensions", prompt)

    def test_manual_llm_csv_outputs_user_facing_score_columns(self) -> None:
        raw_score = self._manual_score(
            subscores={
                "icp_fit": 18,
                "reputation_demand_proof": 14,
                "web_conversion_pain": 22,
                "mockup_leverage": 12,
                "reachability": 8,
                "offer_angle_clarity": 8,
                "evidence_confidence": 4,
            },
            total_score=86,
            grade="A",
            best_angle="No-Site Launch",
            confidence="medium",
        )
        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scores_dir = tmp_path / "scores"
            scores_dir.mkdir()
            (scores_dir / "chij123.json").write_text(json.dumps(raw_score), encoding="utf-8")
            output_path = tmp_path / "scored.csv"

            rows = score_records_manual([enriched], scores_dir=scores_dir, fallback_vertical="hardscaping")
            write_csv(output_path, rows)

            with output_path.open(newline="", encoding="utf-8") as f:
                [csv_row] = list(csv.DictReader(f))

        self.assertEqual(csv_row["Total Score"], "86")
        self.assertEqual(csv_row["Grade"], "A")
        self.assertEqual(csv_row["Best Angle"], "No-Site Launch")
        self.assertEqual(csv_row["Confidence"], "medium")
        self.assertEqual(csv_row["Sendable"], "Yes")
        self.assertEqual(csv_row["Minimum Evidence Passed"], "Yes")

    def test_manual_llm_airtable_fields_override_source_identity_fields(self) -> None:
        raw_score = self._manual_score(
            best_angle="No-Site Launch",
            recommended_action="Build no-site mockup",
            airtable_fields={
                "business_name": "Painesville Patio Co",
                "phone": "(440) 555-0199",
                "vertical": "hardscaping",
                "city": "Painesville",
                "category": "patio contractor",
                "recommended_action": "Send corrected hardscaping mockup",
            },
        )
        enriched = {
            "google": {
                **self._google_fixture(),
                "Business Name": "Painsville Services",
                "business_name": "",
                "Vertical": "service",
                "vertical": "",
                "City": "Painsville",
                "city": "",
                "Phone": "(440) 555-0100",
                "phone": "",
            },
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scores_dir = tmp_path / "scores"
            scores_dir.mkdir()
            (scores_dir / "chij123.json").write_text(json.dumps(raw_score), encoding="utf-8")
            output_path = tmp_path / "scored.csv"

            rows = score_records_manual([enriched], scores_dir=scores_dir, fallback_vertical="service")
            write_csv(output_path, rows)
            packet = packet_markdown(rows[0])

            with output_path.open(newline="", encoding="utf-8") as f:
                [csv_row] = list(csv.DictReader(f))

        self.assertEqual(csv_row["Business Name"], "Painesville Patio Co")
        self.assertEqual(csv_row["business_name"], "Painesville Patio Co")
        self.assertEqual(csv_row["Phone"], "(440) 555-0199")
        self.assertEqual(csv_row["phone"], "(440) 555-0199")
        self.assertEqual(csv_row["Vertical"], "hardscaping")
        self.assertEqual(csv_row["vertical"], "hardscaping")
        self.assertEqual(csv_row["category"], "patio contractor")
        self.assertEqual(csv_row["City"], "Painesville")
        self.assertEqual(csv_row["city"], "Painesville")
        self.assertEqual(csv_row["Best Service Angle"], "No-Site Launch")
        self.assertEqual(csv_row["best_angle"], "No-Site Launch")
        self.assertEqual(csv_row["Recommended Action"], "Send corrected hardscaping mockup")
        self.assertEqual(csv_row["recommended_action"], "Send corrected hardscaping mockup")
        self.assertIn("- **Business:** Painesville Patio Co", packet)
        self.assertIn("- **Vertical:** hardscaping", packet)
        self.assertIn("- **City:** Painesville", packet)

    def test_manual_llm_ignores_bad_airtable_field_keys_and_preserves_canonical_fields(self) -> None:
        raw_score = self._manual_score(
            airtable_fields={
                "Website URL": "https://bad.example",
                "Reviews": "999",
                "Rating": "1.0",
                "[Phone](tel:000)": "(000) 000-0000",
                "Business Name](https://bad.example)": "Bad Markdown Name",
            },
        )
        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertNotIn("[Phone](tel:000)", row)
        self.assertNotIn("Business Name](https://bad.example)", row)
        self.assertEqual(row["Website URL"], "https://example.com")
        self.assertEqual(row["Phone"], "(440) 637-5848")
        self.assertEqual(row["Reviews"], "130")
        self.assertEqual(row["Rating"], "4.9")

    def test_score_leads_writes_csv_after_importing_bad_airtable_fields_bundle(self) -> None:
        first_score = self._manual_score(total_score=75, grade="B")
        second_score = self._manual_score(
            total_score=75,
            grade="B",
            airtable_fields={
                "Website URL": "https://bad.example",
                "Reviews": "999",
                "Rating": "1.0",
                "[Phone](tel:000)": "(000) 000-0000",
            },
        )
        first_enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        second_enriched = {
            "google": {
                **self._google_fixture(),
                "business_name": "Bad Bundle Landscaping",
                "place_id": "ChIJ456",
            },
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_path = tmp_path / "bundle.json"
            scores_dir = tmp_path / "scores"
            input_path = tmp_path / "enriched.jsonl"
            output_path = tmp_path / "scored.csv"
            bundle_path.write_text(
                json.dumps(
                    {
                        "scores": [
                            {"score_file": "chij123.json", "score": first_score},
                            {"score_file": "chij456.json", "score": second_score},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            input_path.write_text(
                f"{json.dumps(first_enriched)}\n{json.dumps(second_enriched)}\n",
                encoding="utf-8",
            )

            import_score_bundle(bundle_path, scores_dir)
            argv = [
                "score_leads.py",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--vertical",
                "hardscaping",
                "--mode",
                "manual-llm",
                "--scores-dir",
                str(scores_dir),
            ]
            with patch("sys.argv", argv):
                code = score_leads_main()

            with output_path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

        bad_bundle_row = next(row for row in rows if row["Business Name"] == "Bad Bundle Landscaping")
        self.assertEqual(code, 0)
        self.assertNotIn("[Phone](tel:000)", rows[0].keys())
        self.assertEqual(bad_bundle_row["Website URL"], "https://example.com")
        self.assertEqual(bad_bundle_row["Phone"], "(440) 637-5848")
        self.assertEqual(bad_bundle_row["Reviews"], "130")
        self.assertEqual(bad_bundle_row["Rating"], "4.9")

    def test_manual_scored_packet_omits_legacy_review_prompt(self) -> None:
        row = merge_manual_score(
            {"google": self._google_fixture(), "evidence": self._evidence_fixture(site_status="weak")},
            self._manual_score(),
            fallback_vertical="hardscaping",
        )

        packet = packet_markdown(row)

        self.assertIn("## Extracted Evidence", packet)
        self.assertIn("## Suggested Opener", packet)
        self.assertIn("## Core TSV Draft", packet)
        self.assertNotIn("ChatGPT/Claude Review Prompt", packet)

    def test_manual_llm_strict_mode_raises_when_any_score_missing(self) -> None:
        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        missing_score = {
            "google": {
                **self._google_fixture(),
                "business_name": "Missing Score Landscaping",
                "place_id": "ChIJ999",
            },
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scores_dir = tmp_path / "scores"
            scores_dir.mkdir()
            (scores_dir / "chij123.json").write_text(json.dumps(self._manual_score()), encoding="utf-8")

            with self.assertRaises(FileNotFoundError) as caught:
                score_records_manual(
                    [enriched, missing_score],
                    scores_dir=scores_dir,
                    fallback_vertical="hardscaping",
                )

        self.assertIn("Missing Score Landscaping", str(caught.exception))
        self.assertIn("chij999.json", str(caught.exception))

    def test_manual_llm_only_scored_writes_outputs_for_available_scores(self) -> None:
        raw_score = self._manual_score()
        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        missing_score = {
            "google": {
                **self._google_fixture(),
                "business_name": "Missing Score Landscaping",
                "place_id": "ChIJ999",
            },
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scores_dir = tmp_path / "scores"
            scores_dir.mkdir()
            (scores_dir / "chij123.json").write_text(json.dumps(raw_score), encoding="utf-8")
            input_path = tmp_path / "enriched.jsonl"
            input_path.write_text(
                f"{json.dumps(enriched)}\n{json.dumps(missing_score)}\n",
                encoding="utf-8",
            )
            output_path = tmp_path / "scored.csv"
            cards_path = tmp_path / "cards.jsonl"
            stdout = io.StringIO()

            argv = [
                "score_leads.py",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--vertical",
                "hardscaping",
                "--mode",
                "manual-llm",
                "--scores-dir",
                str(scores_dir),
                "--cards-output",
                str(cards_path),
                "--only-scored",
            ]
            with patch("sys.argv", argv), redirect_stdout(stdout):
                score_leads_main()

            with output_path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            cards = [json.loads(line) for line in cards_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Business Name"], "A+ Landscaping LLC")
        self.assertIn("Sendable", rows[0])
        self.assertIn("Minimum Evidence Passed", rows[0])
        self.assertEqual(rows[0]["Sendable"], "Yes")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["business_name"], "A+ Landscaping LLC")
        self.assertNotIn("Missing Score Landscaping", [row["Business Name"] for row in rows])
        summary = stdout.getvalue()
        self.assertIn("[MANUAL] Scored records processed: 1", summary)
        self.assertIn("[MANUAL] Missing scores skipped: 1", summary)
        self.assertIn("Missing Score Landscaping", summary)
        self.assertIn(str(scores_dir / "chij999.json"), summary)
        self.assertIn(str(scores_dir / "missing-score-landscaping.json"), summary)

    def test_airtable_payload_and_feedback_metrics(self) -> None:
        card = {
            "place_id_or_unique_key": "ChIJ123",
            "grade": "A",
            "confidence": "medium",
            "best_angle": "Estimate Funnel",
            "recommended_action": "Build mockup then SMS owner",
            "airtable_fields": {
                "Business Name": "A+ Landscaping LLC",
                "Lead Priority": "A",
                "AI Confidence": "medium",
                "Contact Form Detected": "No",
                "Estimate Form Detected": "No",
                "Form Strength": "None",
                "Form Detection Method": "none",
                "CTA Strength": "Weak",
                "Visible Pain Score": "35",
                "Visible Pain Reasons": "No estimate form detected",
                "Send Trigger": "No estimate form detected",
            },
        }

        payload = build_airtable_payload(card)
        self.assertEqual(payload["fields"]["Place Key"], "ChIJ123")
        self.assertEqual(payload["fields"]["Lead Priority"], "A")
        self.assertEqual(payload["fields"]["Visible Pain Score"], 35)
        self.assertEqual(payload["fields"]["Send Trigger"], "No estimate form detected")

        sync_plan = plan_airtable_sync([card], existing_records_by_key={"ChIJ123": "rec123"})
        self.assertEqual(sync_plan[0]["method"], "PATCH")
        self.assertEqual(sync_plan[0]["record_id"], "rec123")

        metrics = compute_feedback_metrics(
            [
                {
                    "AI Grade": "A",
                    "Lou Grade": "A",
                    "Partner Grade": "B",
                    "Human Review Minutes": "2.5",
                },
                {
                    "AI Grade": "A",
                    "Lou Grade": "C",
                    "Partner Grade": "",
                    "Human Review Minutes": "4",
                },
                {
                    "AI Grade": "Skip",
                    "Sampled Skip Verdict": "False Skip",
                },
            ]
        )

        self.assertAlmostEqual(metrics["a_lead_human_approval_rate"], 0.5)
        self.assertAlmostEqual(metrics["average_a_review_minutes"], 3.25)
        self.assertAlmostEqual(metrics["sampled_false_skip_rate"], 1.0)

    def test_sendability_normalizes_duplicate_evidence_refs_and_flattens_safe_claims(self) -> None:
        self.assertEqual(normalize_evidence_refs(["E1", "E2", "E1", "E3"]), ["E1", "E2", "E3"])
        self.assertEqual(normalize_evidence_refs("E1, E2; E3 E2"), ["E1", "E2", "E3"])

        flattened = flatten_safe_outreach_claims(
            [
                {"claim": "Your quote path is hard to find on mobile.", "evidence_refs": ["E3", "E7"]},
                {"claim": "Your best photos are buried.", "evidence_refs": "E9"},
            ]
        )

        self.assertEqual(
            flattened,
            "Your quote path is hard to find on mobile. [E3,E7]; Your best photos are buried. [E9]",
        )

    def test_manual_score_with_invalid_safe_claim_ref_gets_blocked(self) -> None:
        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        raw_score = self._manual_score(
            safe_outreach_claims=[
                {"claim": "Your quote path is hard to find.", "evidence_refs": ["E2", "missing-ref"]}
            ]
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Invalid evidence reference", row["Send Blockers"])
        self.assertEqual(row["Minimum Evidence Passed"], "No")

    def test_manual_score_with_b_grade_but_no_concrete_issue_gets_blocked(self) -> None:
        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(
                site_status="okay",
                evidence_items=[
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 130 reviews"},
                    {"id": "E2", "field": "trust_signals", "value": "licensed, insured"},
                ],
            ),
        }
        raw_score = self._manual_score(
            safe_outreach_claims=[{"claim": "Your reviews are strong.", "evidence_refs": ["E1"]}]
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Lead Priority"], "B")
        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Missing concrete conversion issue", row["Send Blockers"])
        self.assertEqual(row["Packet Eligible"], "")

    def test_sendability_rejects_skip_grade_even_when_other_gates_pass(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "reviews": "18",
            },
            "buyer_intent": self._buyer_intent_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        sendable = merge_manual_score(enriched, self._manual_score(), fallback_vertical="hardscaping")
        self.assertEqual(sendable["Sendable"], "Yes")

        sendable["Lead Priority"] = "Skip"
        sendable["grade"] = "Skip"
        blocked = apply_sendability(sendable)

        self.assertEqual(blocked["Sendable"], "No")
        self.assertIn("Lead grade below sendable threshold", blocked["Send Blockers"])
        self.assertNotEqual(blocked["Packet Tier"], "Sendable Packet")
        self.assertEqual(blocked["Packet Eligible"], "")

    def test_hard_disqualified_manual_score_is_no_packet_even_with_high_total(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "reviews": "18",
            },
            "buyer_intent": self._buyer_intent_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        raw_score = self._manual_score(
            hard_disqualifiers=["Outside target vertical"],
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Score Total"], 75)
        self.assertEqual(row["Lead Priority"], "Skip")
        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Hard disqualifier present", row["Send Blockers"])
        self.assertEqual(row["Minimum Evidence Passed"], "No")
        self.assertEqual(row["Packet Tier"], "No Packet")
        self.assertEqual(row["Packet Eligible"], "")

    def test_isolated_minor_fix_is_nurture_not_sendable(self) -> None:
        enriched = {
            "google": {**self._google_fixture(), "reviews": "18"},
            "buyer_intent": self._buyer_intent_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        row = merge_manual_score(
            enriched,
            self._manual_score(
                commercial_fit_state="minor-fix/nurture",
                contact_eligibility="hold-commercial-case",
            ),
            fallback_vertical="hardscaping",
        )
        self.assertEqual(row["Sendable"], "No")
        self.assertEqual(row["Commercial Fit State"], "minor-fix/nurture")
        self.assertIn("Commercial fit gate failed: minor-fix/nurture", row["Send Blockers"])

    def test_missing_asset_can_contact_now_with_asset_free_promise(self) -> None:
        enriched = {
            "google": {**self._google_fixture(), "reviews": "18"},
            "buyer_intent": self._buyer_intent_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        row = merge_manual_score(
            enriched,
            self._manual_score(asset_state="none", promise_state="asset-free", contact_eligibility="contact-now"),
            fallback_vertical="hardscaping",
        )
        self.assertEqual(row["Sendable"], "Yes")
        self.assertEqual(row["Asset State"], "none")
        self.assertEqual(row["Promise State"], "asset-free")

    def test_asset_specific_promise_waits_for_asset(self) -> None:
        enriched = {
            "google": {**self._google_fixture(), "reviews": "18"},
            "buyer_intent": self._buyer_intent_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        row = merge_manual_score(
            enriched,
            self._manual_score(
                asset_state="planned", promise_state="future-build", contact_eligibility="contact-after-asset"
            ),
            fallback_vertical="hardscaping",
        )
        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Contact eligibility is not contact-now", row["Send Blockers"])
        self.assertEqual(row["Minimum Evidence Passed"], "Yes")

    def test_existing_asset_promise_requires_pass_receipt(self) -> None:
        enriched = {
            "google": {**self._google_fixture(), "reviews": "18"},
            "buyer_intent": self._buyer_intent_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        with self.assertRaisesRegex(ValueError, "PASS asset_verifier_verdict"):
            merge_manual_score(
                enriched,
                self._manual_score(
                    asset_state="verified-existing", promise_state="existing-verified-asset",
                    contact_eligibility="contact-now", asset_id="asset-1", asset_verifier_verdict="BLOCK",
                ),
                fallback_vertical="hardscaping",
            )

    def test_send_trigger_is_required_for_sendable_yes(self) -> None:
        enriched = {
            "google": self._google_fixture(),
            "evidence": {
                "site_status": "weak",
                "visible_pain_score": 35,
                "visible_pain_reasons": ["Generic/template website copy"],
                "send_trigger": "",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 18 reviews"},
                    {"id": "E2", "field": "site_issue_flags", "value": "generic_copy_signals"},
                ],
            },
        }
        raw_score = self._manual_score(
            evidence_refs=["E1", "E2"],
            safe_outreach_claims=[
                {"claim": "Your Google profile has enough review proof.", "evidence_refs": ["E1"]},
                {"claim": "The site has generic copy signals.", "evidence_refs": ["E2"]},
            ],
            airtable_fields={"Send Trigger": ""},
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Missing concrete send trigger", row["Send Blockers"])
        self.assertEqual(row["Packet Eligible"], "")

    def test_strong_reviews_with_sufficient_visible_pain_is_sendable(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "reviews": "130",
            },
            "buyer_intent": {
                "logic_version": BUYER_INTENT_LOGIC_VERSION,
                "crm_booking_stack": {
                    "detected_tools": ["ServiceTitan"],
                    "has_paid_crm": True,
                    "evidence": ["Detected paid CRM/booking tool: ServiceTitan"],
                },
                "review_velocity": {
                    "velocity_band": "steady",
                    "source": "count_only",
                    "evidence": ["130 Google reviews with 4.9 rating"],
                },
                "ads": {
                    "google_ad_transparency": "unknown",
                    "meta_ad_library": "unknown",
                    "lsa_badge": "unknown",
                    "errors": [],
                },
                "money_signal": True,
                "evidence": ["Paid CRM/booking stack detected"],
            },
            "evidence": {
                "site_status": "weak",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Weak",
                "visible_pain_score": 35,
                "visible_pain_reasons": ["Weak estimate form detected"],
                "visible_pain_categories": ["estimate_path_pain"],
                "send_trigger": "Weak estimate form detected",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 130 reviews"},
                    {"id": "E2", "field": "form_strength", "value": "Weak"},
                    {"id": "E3", "field": "send_trigger", "value": "Weak estimate form detected"},
                ],
            },
        }
        raw_score = self._manual_score(
            evidence_refs=["E1", "E2", "E3"],
            visible_pain_score=35,
            visible_pain_reasons=["Weak estimate form detected"],
            visible_pain_categories=["estimate_path_pain"],
            send_trigger="Weak estimate form detected",
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong review proof.", "evidence_refs": ["E1"]},
                {"claim": "The estimate form looks weak for the demand signal.", "evidence_refs": ["E2", "E3"]},
            ],
            opener="Your reviews look strong, but the estimate form feels weak for that demand.",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "Yes")
        self.assertNotIn("Visible pain too weak for review count", row["Send Blockers"])

    def test_bad_site_without_money_signal_is_sendable_with_real_pain(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "reviews": "18",
            },
            "buyer_intent": {
                "crm_booking_stack": {"detected_tools": [], "has_paid_crm": False, "evidence": []},
                "review_velocity": {
                    "velocity_band": "steady",
                    "source": "count_only",
                    "evidence": ["18 Google reviews with 4.9 rating"],
                },
                "ads": {
                    "google_ad_transparency": "unknown",
                    "meta_ad_library": "unknown",
                    "lsa_badge": "unknown",
                    "errors": [],
                },
                "money_signal": False,
                "evidence": [],
            },
            "evidence": {
                "site_status": "missing",
                "visible_pain_score": 40,
                "visible_pain_reasons": ["No owned website found"],
                "visible_pain_categories": ["no_site_or_social_only_pain"],
                "send_trigger": "No owned website found",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 18 reviews"},
                    {"id": "E2", "field": "site_status", "value": "missing"},
                    {"id": "E3", "field": "send_trigger", "value": "No owned website found"},
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Website Rebuild",
            evidence_refs=["E1", "E2", "E3"],
            visible_pain_score=40,
            visible_pain_reasons=["No owned website found"],
            visible_pain_categories=["no_site_or_social_only_pain"],
            send_trigger="No owned website found",
            safe_outreach_claims=[
                {"claim": "No owned website is visible.", "evidence_refs": ["E2", "E3"]},
                {"claim": "The Google profile has review proof.", "evidence_refs": ["E1"]},
            ],
            opener="I could not find an owned website, only the Google profile.",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "Yes")
        # "Unknown", not "No": site_status is missing, so there was no site text
        # to read a paid stack out of. The lead is still sendable on real pain,
        # which is the property this test exists to protect.
        self.assertEqual(row["Money Signal"], "Unknown")

    def test_advanced_optimization_is_not_sendable_without_concrete_visible_pain(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "reviews": "92",
            },
            "evidence": {
                "site_status": "okay",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "cta_strength": "Strong",
                "visible_pain_score": 0,
                "visible_pain_reasons": [],
                "send_trigger": "",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 92 reviews"},
                    {"id": "E2", "field": "form_strength", "value": "Strong"},
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Advanced Optimization",
            evidence_refs=["E1", "E2"],
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong reviews.", "evidence_refs": ["E1"]},
                {"claim": "The site has a strong estimate form.", "evidence_refs": ["E2"]},
            ],
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Advanced Optimization without concrete visible pain", row["Send Blockers"])
        self.assertIn("Missing concrete send trigger", row["Send Blockers"])

    def test_manual_score_with_high_score_and_low_confidence_gets_blocked_but_evidence_can_pass(self) -> None:
        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        raw_score = self._manual_score(confidence="low")

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Lead Priority"], "B")
        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Low confidence", row["Send Blockers"])
        self.assertEqual(row["Minimum Evidence Passed"], "Yes")

    def test_no_site_lead_with_strong_reviews_passes_concrete_issue_gate(self) -> None:
        row = score_record(
            {
                "google": {
                    "business_name": "Ron & Sons Landscaping",
                    "city": "East Cleveland",
                    "phone": "(216) 555-1212",
                    "rating": "4.9",
                    "reviews": "31",
                    "website_url": "",
                    "google_maps_url": "https://maps.google.com/?cid=456",
                    "place_id": "ChIJ456",
                },
                "website": {
                    "site_status": "No Website",
                    "site_issue_flags": [],
                    "pages": {},
                    "emails_found": [],
                    "phones_found": [],
                    "forms_found": False,
                    "cta_terms": [],
                    "combined_text": "",
                },
                "derived": {},
                "screenshots": {},
                "buyer_intent": self._buyer_intent_fixture(),
            },
            "hardscaping",
        )

        self.assertEqual(row["Lead Priority"], "A")
        self.assertEqual(row["Sendable"], "Yes")
        self.assertEqual(row["Minimum Evidence Passed"], "Yes")
        self.assertEqual(row["Packet Eligible"], "Yes")
        self.assertIn("no owned website or conversion asset", row["Safe Outreach Claims"])

    def test_static_fetch_failure_with_rendered_load_is_not_broken_claim(self) -> None:
        record = {
            "google": {
                **self._google_fixture(),
                "business_name": "BG Contracting",
                "reviews": "18",
                "rating": "4.8",
            },
            "website": {
                "site_status": "Loaded",
                "rendered_site_status": "Loaded",
                "rendered_status_reason": "Browser rendered homepage successfully after static fetch failed.",
                "visual_review_needed": "Yes",
                "site_issue_flags": [
                    "static_fetch_failed",
                    "rendered_check_needed",
                    "manual_visual_review_needed",
                    "outdated_site",
                    "premium_perception_gap",
                ],
                "final_url": "https://bgcontracting.example",
                "domain": "bgcontracting.example",
                "homepage": {
                    "url": "https://bgcontracting.example",
                    "status_code": "",
                    "title": "",
                    "meta": "",
                    "text": "",
                    "error": "static request timed out",
                },
                "pages": {},
                "emails_found": ["office@bgcontracting.example"],
                "phones_found": ["(440) 555-0199"],
                "forms_found": False,
                "contact_form_detected": "Unknown",
                "estimate_form_detected": "Unknown",
                "form_strength": "None",
                "form_detection_method": "none",
                "cta_strength": "Unknown",
                "cta_terms": [],
                "high_ticket_terms": ["hardscaping", "retaining wall", "patio"],
                "generic_copy_terms": [],
                "combined_text": "",
            },
            "derived": {
                "phone_consistency": "matched",
                "has_form": False,
                "has_high_ticket_terms": True,
                "non_standard_site_or_booking": False,
            },
            "screenshots": {
                "desktop": "screenshots/bg-contracting-desktop.png",
                "mobile": "screenshots/bg-contracting-mobile.png",
                "error": "",
            },
            "buyer_intent": self._buyer_intent_fixture(),
        }
        record["evidence"] = build_evidence_profile(record)

        row = score_record(record, "hardscaping")

        self.assertEqual(row["Site Status"], "Loaded")
        self.assertEqual(row["Rendered Site Status"], "Loaded")
        self.assertEqual(row["Visual Review Needed"], "Yes")
        self.assertNotEqual(row["Send Trigger"], "Website appears broken")
        self.assertNotIn("website appears broken", row["Safe Outreach Claims"].lower())
        self.assertNotIn("appears broken", row["Opener"].lower())
        self.assertIn(row["Offer Fit"], {"Website Rebuild", "Premium Perception Gap", "Website Cleanup"})
        self.assertEqual(row["Sendable"], "Yes")
        self.assertEqual(row["Packet Eligible"], "Yes")

    def test_high_scoring_hallucinated_lead_gets_blocked_when_claims_are_ungrounded(self) -> None:
        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(
                site_status="okay",
                evidence_items=[
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 130 reviews"},
                    {"id": "E2", "field": "trust_signals", "value": "licensed, insured"},
                ],
            ),
        }
        raw_score = self._manual_score(
            safe_outreach_claims=[{"claim": "You are losing jobs because your form is broken.", "evidence_refs": []}]
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Missing concrete conversion issue", row["Send Blockers"])
        self.assertIn("Missing evidence-backed opener", row["Send Blockers"])
        self.assertIn("Safe outreach claim missing evidence reference", row["Send Blockers"])
        self.assertEqual(row["Minimum Evidence Passed"], "No")

    def test_packet_eligible_cannot_be_yes_when_sendable_is_no(self) -> None:
        row = {"Lead Priority": "A", "Packet Eligible": "Yes", "Sendable": "No"}

        self.assertFalse(is_eligible(row, include_all=False, priority_filter=None))
        self.assertTrue(is_eligible(row, include_all=True, priority_filter=None))

    # --- Packet tier: split packet creation from sendability ----------------

    def _facebook_only_evidence(self, *, reviews: str, reasons: list) -> dict:
        return {
            "google": {
                **self._google_fixture(),
                "business_name": "Facebook Only Detailing LLC",
                "rating": "5.0",
                "reviews": reviews,
            },
            "evidence": {
                "site_status": "weak",
                "visible_pain_score": 50,
                "visible_pain_reasons": reasons,
                "visible_pain_categories": ["no_site_or_social_only_pain", "estimate_path_pain"],
                "send_trigger": "Social-only web presence",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": f"5.0 stars / {reviews} reviews"},
                    {"id": "E2", "field": "site_status", "value": "weak"},
                    {"id": "E3", "field": "send_trigger", "value": "Social-only web presence"},
                ],
            },
        }

    def _facebook_only_score(self, **overrides: object) -> dict:
        base = dict(
            best_angle="No-Site Launch",
            evidence_refs=["E1", "E2", "E3"],
            visible_pain_score=50,
            visible_pain_reasons=[
                "No standalone website — business presence is Facebook-only",
                "No clear estimate CTA detected",
                "Phone number hard to find on the page",
            ],
            visible_pain_categories=["no_site_or_social_only_pain", "estimate_path_pain"],
            send_trigger="Social-only web presence",
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong review proof.", "evidence_refs": ["E1"]},
                {"claim": "The web presence relies on social pages, not an owned site.", "evidence_refs": ["E2", "E3"]},
            ],
            opener="Your reviews look strong, but I could not find a standalone website. Want me to send a direction?",
        )
        base.update(overrides)
        return self._manual_score(**base)

    def test_b_no_site_facebook_only_low_confidence_is_manual_review_packet(self) -> None:
        enriched = self._facebook_only_evidence(
            reviews="45",
            reasons=[
                "No standalone website — business presence is Facebook-only",
                "No clear estimate CTA detected",
                "Phone number hard to find on the page",
            ],
        )
        raw_score = self._facebook_only_score(confidence="low")

        row = merge_manual_score(enriched, raw_score, fallback_vertical="detailing")

        self.assertEqual(row["Lead Priority"], "B")
        self.assertEqual(row["Sendable"], "No")
        self.assertEqual(row["Minimum Evidence Passed"], "Yes")
        self.assertEqual(row["Packet Tier"], "Manual Review Packet")
        self.assertEqual(row["Packet Eligible"], "Yes")
        self.assertTrue(row["Manual Review Reason"])
        self.assertTrue(is_eligible(row, include_all=False, priority_filter=None))

    def test_refresh_packet_tiers_backfills_old_rehab_style_csv_row(self) -> None:
        from refresh_packet_tiers import refresh_csv

        enriched = self._facebook_only_evidence(
            reviews="45",
            reasons=[
                "No standalone website — business presence is Facebook-only",
                "No clear estimate CTA detected",
                "Phone number hard to find on the page",
            ],
        )
        enriched["google"]["business_name"] = "Rehab Auto Spa"
        raw_score = self._facebook_only_score(confidence="low")
        scored = merge_manual_score(enriched, raw_score, fallback_vertical="detailing")

        old_row = dict(scored)
        old_row.pop("Packet Tier", None)
        old_row.pop("Manual Review Reason", None)
        old_row["Packet Eligible"] = ""
        old_row["Packet Eligibility Reason"] = ""

        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "old_scored.csv"
            output_path = Path(tmp) / "refreshed.csv"
            fieldnames = [key for key in old_row if key not in {"Packet Tier", "Manual Review Reason"}]
            with input_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(old_row)

            refresh_csv(input_path, output_path)

            with output_path.open(newline="", encoding="utf-8") as f:
                [row] = list(csv.DictReader(f))

        self.assertEqual(row["Business Name"], "Rehab Auto Spa")
        self.assertEqual(row["Lead Priority"], scored["Lead Priority"])
        self.assertEqual(row["Packet Tier"], "Manual Review Packet")
        self.assertEqual(row["Packet Eligible"], "Yes")
        self.assertIn("Manual Review Packet", row["Packet Eligibility Reason"])
        self.assertTrue(row["Manual Review Reason"])

    def test_c_no_site_facebook_only_below_b_is_manual_review_packet(self) -> None:
        enriched = self._facebook_only_evidence(
            reviews="24",
            reasons=[
                "Business uses a Facebook profile as its primary web presence — no standalone website",
                "No estimate CTA or form detectable",
            ],
        )
        raw_score = self._facebook_only_score(
            subscores={
                "icp_fit": 10,
                "reputation_demand_proof": 10,
                "web_conversion_pain": 12,
                "mockup_leverage": 8,
                "reachability": 6,
                "offer_angle_clarity": 5,
                "evidence_confidence": 4,
            },
            visible_pain_reasons=[
                "Business uses a Facebook profile as its primary web presence — no standalone website",
                "No estimate CTA or form detectable",
            ],
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="detailing")

        self.assertEqual(row["Lead Priority"], "C")
        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Score below B threshold", row["Send Blockers"])
        self.assertEqual(row["Minimum Evidence Passed"], "Yes")
        self.assertEqual(row["Packet Tier"], "Manual Review Packet")
        self.assertEqual(row["Packet Eligible"], "Yes")

    def test_c_estimate_funnel_strong_cta_low_visible_pain_is_not_manual_review(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "business_name": "Luminix Detailing and Ceramic Coating",
                "rating": "5.0",
                "reviews": "15",
            },
            "evidence": {
                "site_status": "okay",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Basic",
                "cta_strength": "Strong",
                "visible_pain_score": 12,
                "visible_pain_reasons": ["Basic estimate form only — no paid CRM or booking tool detected"],
                "visible_pain_categories": ["estimate_path_pain"],
                "send_trigger": "Basic estimate form only",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "5.0 stars / 15 reviews"},
                    {"id": "E2", "field": "form_strength", "value": "Basic"},
                    {"id": "E3", "field": "send_trigger", "value": "Basic estimate form only"},
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Estimate Funnel",
            subscores={
                "icp_fit": 11,
                "reputation_demand_proof": 10,
                "web_conversion_pain": 12,
                "mockup_leverage": 8,
                "reachability": 6,
                "offer_angle_clarity": 5,
                "evidence_confidence": 3,
            },
            evidence_refs=["E1", "E2", "E3"],
            visible_pain_score=12,
            visible_pain_reasons=["Basic estimate form only — no paid CRM or booking tool detected"],
            visible_pain_categories=["estimate_path_pain"],
            send_trigger="Basic estimate form only",
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong review proof.", "evidence_refs": ["E1"]},
                {"claim": "The estimate form is basic for the demand signal.", "evidence_refs": ["E2", "E3"]},
            ],
            opener="Your reviews look strong; the estimate form looks basic for that demand.",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="detailing")

        self.assertEqual(row["Lead Priority"], "C")
        self.assertEqual(row["Sendable"], "No")
        self.assertNotEqual(row["Packet Tier"], "Manual Review Packet")
        self.assertEqual(row["Packet Eligible"], "")
        self.assertFalse(is_eligible(row, include_all=False, priority_filter=None))

    def test_skip_social_only_high_pain_becomes_watchlist_not_manual_review_packet(self) -> None:
        # Skip grade (total < 50) + social-only pain: should land in Watchlist, never Manual Review Packet.
        enriched = self._facebook_only_evidence(
            reviews="45",
            reasons=[
                "No standalone website — business presence is Facebook-only",
                "No clear estimate CTA detected",
            ],
        )
        raw_score = self._facebook_only_score(
            subscores={
                "icp_fit": 8,
                "reputation_demand_proof": 7,
                "web_conversion_pain": 10,
                "mockup_leverage": 6,
                "reachability": 5,
                "offer_angle_clarity": 5,
                "evidence_confidence": 4,
            },
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="detailing")

        self.assertEqual(row["Lead Priority"], "Skip")
        self.assertEqual(row["Sendable"], "No")
        self.assertNotEqual(row["Packet Tier"], "Manual Review Packet")
        self.assertEqual(row["Packet Tier"], "Watchlist")
        self.assertEqual(row["Packet Eligible"], "")
        self.assertFalse(is_eligible(row, include_all=False, priority_filter=None))

    def test_advanced_optimization_strong_form_no_issue_is_no_packet(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "reviews": "40",
            },
            "evidence": {
                "site_status": "okay",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "cta_strength": "Strong",
                "visible_pain_score": 0,
                "visible_pain_reasons": [],
                "send_trigger": "",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 40 reviews"},
                    {"id": "E2", "field": "form_strength", "value": "Strong"},
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Advanced Optimization",
            evidence_refs=["E1", "E2"],
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong reviews.", "evidence_refs": ["E1"]},
                {"claim": "The site has a strong estimate form.", "evidence_refs": ["E2"]},
            ],
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="detailing")

        self.assertEqual(row["Sendable"], "No")
        self.assertEqual(row["Packet Tier"], "No Packet")
        self.assertEqual(row["Packet Eligible"], "")
        self.assertFalse(is_eligible(row, include_all=False, priority_filter=None))

    def test_sendable_lead_is_sendable_packet(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "reviews": "18",
            },
            "buyer_intent": self._buyer_intent_fixture(),
            "evidence": {
                "site_status": "missing",
                "visible_pain_score": 40,
                "visible_pain_reasons": ["No owned website found"],
                "visible_pain_categories": ["no_site_or_social_only_pain"],
                "send_trigger": "No owned website found",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 18 reviews"},
                    {"id": "E2", "field": "site_status", "value": "missing"},
                    {"id": "E3", "field": "send_trigger", "value": "No owned website found"},
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Website Rebuild",
            evidence_refs=["E1", "E2", "E3"],
            visible_pain_score=40,
            visible_pain_reasons=["No owned website found"],
            visible_pain_categories=["no_site_or_social_only_pain"],
            send_trigger="No owned website found",
            safe_outreach_claims=[
                {"claim": "No owned website is visible.", "evidence_refs": ["E2", "E3"]},
                {"claim": "The Google profile has review proof.", "evidence_refs": ["E1"]},
            ],
            opener="I could not find an owned website, only the Google profile.",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "Yes")
        self.assertEqual(row["Packet Tier"], "Sendable Packet")
        self.assertEqual(row["Packet Eligible"], "Yes")
        self.assertEqual(row["Manual Review Reason"], "")
        self.assertTrue(is_eligible(row, include_all=False, priority_filter=None))

    def test_score_record_uses_record_vertical_profile_for_detailing_denylist(self) -> None:
        row = score_record(
            {
                "google": {
                    **self._google_fixture(),
                    "business_name": "Mister Car Wash Cleveland",
                    "category": "Car detailing service",
                    "source_query": "ceramic coating Cleveland OH",
                    "vertical": "detailing",
                    "rating": "4.8",
                    "reviews": "28",
                },
                "website": {
                    "site_status": "No Website",
                    "site_issue_flags": [],
                    "combined_text": "ceramic coating paint correction ppf",
                    "high_ticket_terms": ["ceramic coating", "paint correction", "ppf"],
                },
                "derived": {
                    "has_high_ticket_terms": True,
                    "has_social_links": False,
                },
                "evidence": {
                    "site_status": "No Website",
                    "visible_pain_score": 40,
                    "visible_pain_reasons": ["No owned website found"],
                    "visible_pain_categories": ["no_site_or_social_only_pain"],
                    "send_trigger": "No owned website found",
                    "evidence_items": [
                        {"id": "ev-rating", "field": "google_rating", "value": "4.8 / 28"},
                        {"id": "ev-trigger", "field": "send_trigger", "value": "No owned website found"},
                    ],
                },
            },
            "hardscaping",
        )

        self.assertEqual(row["Lead Priority"], "Skip")
        self.assertIn("franchise/brand denylist", row["Fit Flags"])

    def test_no_site_lead_is_not_demoted_for_an_unobservable_money_signal(self) -> None:
        """The inverse of the rule this replaced.

        `no_site and not money_signal -> C` could not do anything but fire:
        money_signal is read from paid-CRM text scraped off the website, so a
        business with no website could never satisfy it. Across two real
        batches 25/25 and 13/13 no-site leads went base=A -> ceiling=C -> C,
        which is below the A/B floor Sendable requires — the pipeline was built
        to find these leads and then graded every one of them out.
        """
        row = score_record(
            {
                "google": {
                    **self._google_fixture(),
                    "rating": "4.9",
                    "reviews": "18",
                },
                "website": {
                    "site_status": "No Website",
                    "site_issue_flags": [],
                    "combined_text": "hardscape patio retaining wall",
                    "high_ticket_terms": ["hardscape", "patio", "retaining wall"],
                },
                "derived": {"has_high_ticket_terms": True},
            },
            "hardscaping",
        )

        self.assertEqual(row["Money Signal"], "Unknown")
        self.assertEqual(row["Lead Priority"], "A")

    def test_readable_site_with_a_real_money_negative_is_still_graded_on_its_merits(self) -> None:
        # Removing the demotion must not silently promote every lead: a site we
        # *could* read and found no paid stack in still grades off offer fit and
        # the review band, not off money_signal.
        row = score_record(
            {
                "google": {
                    **self._google_fixture(),
                    "rating": "4.9",
                    "reviews": "18",
                },
                "website": {
                    "site_status": "Okay",
                    "site_issue_flags": [],
                    "combined_text": "hardscape patio retaining wall free estimate",
                    "high_ticket_terms": ["hardscape", "patio", "retaining wall"],
                },
                "derived": {"has_high_ticket_terms": True},
            },
            "hardscaping",
        )

        self.assertEqual(row["Money Signal"], "No")
        self.assertIn(row["Lead Priority"], {"A", "B", "C", "Skip"})

    def test_money_signal_defends_high_review_bad_site_deterministic_tier(self) -> None:
        base_record = {
            "google": {
                **self._google_fixture(),
                "rating": "4.9",
                "reviews": "92",
            },
            "website": {
                "site_status": "No Website",
                "site_issue_flags": [],
                "combined_text": "hardscape patio retaining wall ServiceTitan",
                "high_ticket_terms": ["hardscape", "patio", "retaining wall"],
            },
            "buyer_intent": {
                "logic_version": BUYER_INTENT_LOGIC_VERSION,
                "crm_booking_stack": {
                    "detected_tools": ["ServiceTitan"],
                    "has_paid_crm": True,
                    "evidence": ["Detected paid CRM/booking tool: ServiceTitan"],
                },
                "review_velocity": {"velocity_band": "live", "source": "count_only", "evidence": []},
                "ads": {
                    "google_ad_transparency": "unknown",
                    "meta_ad_library": "unknown",
                    "lsa_badge": "unknown",
                    "errors": [],
                },
                "money_signal": True,
                "evidence": ["Paid CRM/booking stack detected"],
            },
            "derived": {"has_high_ticket_terms": True},
        }

        row = score_record(base_record, "hardscaping")

        self.assertEqual(row["Lead Priority"], "A")
        self.assertEqual(row["Money Signal"], "Yes")

    def test_score_record_surfaces_ideal_growth_review_demand_band(self) -> None:
        row = score_record(
            {
                "google": {
                    **self._google_fixture(),
                    "rating": "4.7",
                    "reviews": "20",
                },
                "website": {
                    "site_status": "Loaded",
                    "site_issue_flags": ["no_clear_cta_detected", "weak_basic_form"],
                    "combined_text": "hardscape patio retaining wall estimate",
                    "high_ticket_terms": ["hardscape", "patio", "retaining wall"],
                    "forms_found": True,
                    "contact_form_detected": "Yes",
                    "estimate_form_detected": "Yes",
                    "form_strength": "Weak",
                    "cta_strength": "Weak",
                },
                "evidence": {
                    "site_status": "weak",
                    "visible_pain_score": 35,
                    "visible_pain_reasons": ["Weak estimate path"],
                    "visible_pain_categories": ["estimate_path_pain"],
                    "send_trigger": "Weak estimate path",
                    "evidence_items": [
                        {"id": "E1", "field": "google_rating", "value": "4.7 stars / 20 reviews"},
                        {"id": "E2", "field": "form_strength", "value": "Weak"},
                        {"id": "E3", "field": "send_trigger", "value": "Weak estimate path"},
                    ],
                },
                "derived": {"has_high_ticket_terms": True, "has_form": True},
            },
            "hardscaping",
        )

        self.assertEqual(row["Review Demand Band"], "ideal_growth")
        self.assertEqual(row["Review Fit Band"], "ideal_growth")

    def test_batch_prompt_separates_money_intent_from_count_only_review_demand(self) -> None:
        prompt = build_batch_prompt([], batch_id="batch_test")

        self.assertIn("Paid CRM/booking tools and known ad signals can support money intent.", prompt)
        self.assertIn("Count-only review demand bands support reputation fit, not money intent by themselves.", prompt)
        self.assertNotIn("live review velocity", prompt)

    def test_mature_high_review_strong_funnel_is_not_sendable(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "rating": "5.0",
                "reviews": "260",
            },
            "evidence": {
                "site_status": "okay",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "cta_strength": "Strong",
                "visible_pain_score": 0,
                "visible_pain_reasons": [],
                "visible_pain_categories": [],
                "send_trigger": "",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "5.0 stars / 260 reviews"},
                    {"id": "E2", "field": "form_strength", "value": "Strong estimate form"},
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Review/Trust Proof Upgrade",
            evidence_refs=["E1", "E2"],
            safe_outreach_claims=[
                {"claim": "Your Google profile shows mature review proof.", "evidence_refs": ["E1"]},
                {"claim": "The site has a strong estimate form.", "evidence_refs": ["E2"]},
            ],
            opener="Your reviews are strong and the estimate form is visible.",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "No")
        self.assertEqual(row["Packet Eligible"], "")
        self.assertIn("Missing concrete conversion issue", row["Send Blockers"])
        self.assertIn("Visible conversion pain too weak for business maturity", row["Send Blockers"])

    def test_mature_high_review_missing_estimate_path_can_be_sendable_with_money_signal(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "rating": "4.9",
                "reviews": "260",
            },
            "buyer_intent": self._buyer_intent_fixture(),
            "evidence": {
                "site_status": "weak",
                "contact_form_detected": "No",
                "estimate_form_detected": "No",
                "form_strength": "None",
                "cta_strength": "Weak",
                "visible_pain_score": 45,
                "visible_pain_reasons": ["Missing estimate path"],
                "visible_pain_categories": ["estimate_path_pain"],
                "send_trigger": "Missing estimate path",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 260 reviews"},
                    {"id": "E2", "field": "site_issue_flags", "value": "missing_estimate_path"},
                    {"id": "E3", "field": "send_trigger", "value": "Missing estimate path"},
                    {"id": "buyer_intent", "field": "buyer_intent", "value": "paid CRM detected"},
                ],
            },
        }
        raw_score = self._manual_score(
            evidence_refs=["E1", "E2", "E3", "buyer_intent"],
            visible_pain_score=45,
            visible_pain_reasons=["Missing estimate path"],
            visible_pain_categories=["estimate_path_pain"],
            send_trigger="Missing estimate path",
            safe_outreach_claims=[
                {"claim": "Your Google profile shows mature review proof.", "evidence_refs": ["E1"]},
                {"claim": "The estimate path appears missing.", "evidence_refs": ["E2", "E3"]},
            ],
            opener="Your reviews look strong, but I could not find a clear estimate path.",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Money Signal"], "Yes")
        self.assertEqual(row["Sendable"], "Yes")
        self.assertNotIn("Visible conversion pain too weak for business maturity", row["Send Blockers"])

    def _google_fixture(self) -> dict:
        return {
            "business_name": "A+ Landscaping LLC",
            "city": "Kirtland",
            "phone": "(440) 637-5848",
            "website_url": "https://example.com",
            "google_maps_url": "https://maps.google.com/?cid=123",
            "rating": "4.9",
            "reviews": "130",
            "place_id": "ChIJ123",
        }

    def _buyer_intent_fixture(self) -> dict:
        return {
            "logic_version": BUYER_INTENT_LOGIC_VERSION,
            "crm_booking_stack": {
                "detected_tools": ["ServiceTitan"],
                "has_paid_crm": True,
                "evidence": ["Detected paid CRM/booking tool: ServiceTitan"],
            },
            "review_velocity": {
                "state": "unknown",
                "velocity_band": "unknown",
                "source": "insufficient_history",
                "evidence": ["Review velocity needs at least two count snapshots 14 days apart."],
            },
            "ads": {
                "google_ad_transparency": "unknown",
                "meta_ad_library": "unknown",
                "lsa_badge": "unknown",
                "errors": [
                    "google_ad_transparency: unimplemented",
                    "meta_ad_library: unimplemented",
                ],
            },
            "money_signal": "yes",
            "demand_signal": {
                "state": "yes",
                "source": "review_count",
                "velocity_band": "unknown",
                "demand_band": "ideal_growth",
                "evidence": ["Lifetime review count places this in the ideal_growth demand band."],
            },
            "evidence": ["Paid CRM/booking stack detected"],
        }

    def _evidence_fixture(self, *, site_status: str, evidence_items: list | None = None) -> dict:
        return {
            "site_status": site_status,
            "screenshots": {"desktop": "desktop.png", "mobile": "mobile.png"},
            "visible_pain_score": 35 if site_status == "weak" else 0,
            "visible_pain_reasons": ["No clear estimate CTA"] if site_status == "weak" else [],
            "send_trigger": "No clear estimate CTA" if site_status == "weak" else "",
            "evidence_items": evidence_items
            or [
                {"id": "E1", "field": "google_rating", "value": "4.9 stars / 130 reviews"},
                {"id": "E2", "field": "site_issue_flags", "value": "no_clear_cta_detected"},
                {"id": "E3", "field": "send_trigger", "value": "No clear estimate CTA"},
            ],
        }

    def _manual_score(self, **overrides: object) -> dict:
        score = {
            "score_schema_version": 2,
            "subscores": {
                "icp_fit": 14,
                "reputation_demand_proof": 12,
                "web_conversion_pain": 18,
                "mockup_leverage": 10,
                "reachability": 7,
                "offer_angle_clarity": 9,
                "evidence_confidence": 5,
            },
            "confidence": "medium",
            "hard_disqualifiers": [],
            "best_angle": "Estimate Funnel",
            "commercial_fit_state": "commercially-sendable",
            "commercial_materiality": "Premium estimate requests can fail to reach a clear conversion path.",
            "recurring_consequence": "The same path affects each new high-intent visitor.",
            "root_cause_confidence": "Fresh rendered evidence shows the CTA-path weakness.",
            "offer_component": "Get the Lead",
            "packageable_solution": "Rebuild the estimate path and trust placement.",
            "value_mechanism": "Reduce friction between existing demand and estimate requests.",
            "beyond_free_fix": "The CTA, mobile path, trust proof, and routing require one coherent system.",
            "self_fix_risk": "The existing multi-step path has persisted and needs coordinated implementation.",
            "intentionality": "The verified customer path contradicts an intentional low-conversion design.",
            "contact_eligibility": "contact-now",
            "asset_state": "none",
            "promise_state": "asset-free",
            "asset_id": "",
            "asset_verifier_verdict": "",
            "recommended_action": "Build mockup then SMS owner",
            "why_this_lead": ["Strong reviews", "Visible quote-path issue"],
            "why_this_might_be_wrong": [],
            "opener": "Your reviews are strong, but the quote path looks hard to find. Want me to send a cleaner version?",
            "evidence_refs": ["E1", "E2"],
            "safe_outreach_claims": [
                {"claim": "Your quote path looks hard to find.", "evidence_refs": ["E2"]},
                {"claim": "Your Google profile shows strong reviews.", "evidence_refs": ["E1"]},
            ],
            "unsafe_claims": ["Do not claim they are losing jobs."],
            "airtable_fields": {},
        }
        score.update(overrides)
        return score


class NoOwnedSiteOfferFitTest(unittest.TestCase):
    """The deterministic scorer must draw the same no-owned-site line the LLM does.

    There is nothing to rebuild for a business whose only web presence is a
    Facebook page or a Jobber booking link, and the LLM path already labels that
    No-Site Launch (via its "Social/Jobber Escape"). Before this, the
    deterministic path called all of them Website Rebuild, so the No-Site Launch
    select option was unreachable from it.
    """

    def _record(self, site_status: str, *, booking: bool = False) -> dict:
        return {
            "google": {
                "business_name": "Acme Hardscaping", "rating": "4.9", "reviews": "130",
                "city": "Cleveland", "phone": "(440) 637-5848", "place_id": "ChIJ1",
                "website_url": "https://acme.example",
            },
            "website": {
                "site_status": site_status, "form_strength": "None", "cta_strength": "Weak",
                "contact_form_detected": "No", "estimate_form_detected": "No",
                "high_ticket_terms": ["patio", "retaining wall"], "generic_copy_terms": [],
                "combined_text": "patio retaining wall hardscape",
            },
            "derived": {
                "phone_consistency": "matched", "has_email": False, "has_form": False,
                "has_cta": False, "has_high_ticket_terms": True,
                "non_standard_site_or_booking": booking,
            },
            "screenshots": {},
        }

    def test_no_owned_site_statuses_produce_no_site_launch(self) -> None:
        for status in ("No Website", "Social Only", "Jobber Only"):
            with self.subTest(site_status=status):
                row = score_record(self._record(status), "hardscaping")
                self.assertEqual(row["Offer Fit"], "No-Site Launch")
                self.assertEqual(
                    normalize_airtable_value("Offer Fit", row["Offer Fit"]), "No-Site Launch"
                )

    def test_booking_only_url_produces_no_site_launch(self) -> None:
        row = score_record(self._record("Loaded", booking=True), "hardscaping")
        self.assertEqual(row["Offer Fit"], "No-Site Launch")

    def test_broken_owned_site_is_still_a_rebuild(self) -> None:
        row = score_record(self._record("Broken Website"), "hardscaping")
        self.assertEqual(row["Offer Fit"], "Website Rebuild")

    def test_jobber_only_is_not_scored_as_a_healthy_site(self) -> None:
        # Jobber Only was missing from both the offer-fit and grade paths, so a
        # business with no owned site scored as Advanced Optimization / Good.
        row = score_record(self._record("Jobber Only"), "hardscaping")
        self.assertEqual(row["Current Website Quality"], "Poor")
        self.assertNotEqual(row["Offer Fit"], "Advanced Optimization")

    def test_no_site_launch_keeps_packet_eligibility_and_grade(self) -> None:
        from score_leads import CONCRETE_PACKET_OFFERS, base_priority_from_delta

        self.assertIn("No-Site Launch", CONCRETE_PACKET_OFFERS)
        for status in ("No Website", "Social Only", "Jobber Only", "Broken Website"):
            with self.subTest(site_status=status):
                self.assertEqual(
                    base_priority_from_delta(self._record(status), "No-Site Launch"), "A"
                )


if __name__ == "__main__":
    unittest.main()
