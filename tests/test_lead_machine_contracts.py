from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import upload_airtable
from enrich_leads import detect_form_signals, scrape_site
from lead_machine.airtable_sync import build_airtable_payload, plan_airtable_sync
from lead_machine.evidence import build_evidence_profile
from lead_machine.feedback import compute_feedback_metrics
from lead_machine.icp import hard_icp_gate
from lead_machine.lead_card import build_lead_card
from lead_machine.llm_queue import build_llm_prompt, write_review_queue
from lead_machine.manual_scores import load_manual_score, merge_manual_score
from lead_machine.schema import normalize_lead, row_dedupe_keys
from lead_machine.sendability import flatten_safe_outreach_claims, normalize_evidence_refs
from make_packets import is_eligible, packet_markdown
from make_llm_batch import write_llm_batches
from import_llm_score_bundle import import_score_bundle
from score_leads import main as score_leads_main
from score_leads import score_record, score_records_manual, write_csv


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

    def _dry_run_plan(self, stdout: str) -> list[dict[str, object]]:
        lines = stdout.splitlines()
        self.assertTrue(lines and lines[0].startswith("[DRY-RUN]"))
        return json.loads("\n".join(lines[1:]))

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
        self.assertEqual(fields["Best Service Angle"], "Website Rebuild")
        self.assertEqual(fields["Status"], "Mockup Needed")
        self.assertEqual(fields["Next Action"], "Build mockup")
        self.assertEqual(fields["Outreach Channel"], "Google Maps")
        self.assertEqual(fields["Packet Eligible"], "Yes")
        self.assertEqual(fields["Sendable"], "Yes")
        self.assertEqual(fields["Contact Form Detected"], "No")
        self.assertEqual(fields["Estimate Form Detected"], "No")
        self.assertEqual(fields["Form Strength"], "None")
        self.assertEqual(fields["Form Detection Method"], "none")
        self.assertEqual(fields["CTA Strength"], "Weak")
        self.assertEqual(fields["Visible Pain Score"], 35)
        self.assertEqual(fields["Visible Pain Reasons"], "No estimate form detected; No clear estimate CTA")
        self.assertEqual(fields["Send Trigger"], "No estimate form detected")

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
        self.assertEqual(fields["Sendable"], "Yes")
        self.assertEqual(fields["Packet Eligible"], "Yes")
        self.assertNotIn("Recommended Action", fields)
        self.assertNotIn("Place Key", fields)

    def test_airtable_csv_dry_run_preserves_explicit_sendable_operational_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            self._write_airtable_csv(
                csv_path,
                [
                    self._airtable_csv_row(
                        **{
                            "Status": "Contacted",
                            "Next Action": "Follow up",
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
        self.assertEqual(fields["Next Action"], "Follow up")

    def test_airtable_patch_does_not_send_defaulted_operational_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            self._write_airtable_csv(csv_path, [self._airtable_csv_row(**{"Outreach Channel": ""})])

            records = upload_airtable.read_input_records(csv_path, only_sendable=False, packet_dir=Path(tmp))
            planned = plan_airtable_sync(records, existing_records_by_key={"place:ChIJ123": "rec-existing"})

        self.assertEqual(planned[0]["method"], "PATCH")
        fields = planned[0]["payload"]["fields"]  # type: ignore[index]
        self.assertNotIn("Status", fields)
        self.assertNotIn("Next Action", fields)
        self.assertNotIn("Outreach Channel", fields)

    def test_airtable_only_sendable_excludes_non_sendable_csv_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            rows = [
                self._airtable_csv_row(),
                self._airtable_csv_row(
                    **{"Business Name": "Not Sendable LLC", "Sendable": "No", "Place Key": "ChIJ999"}
                ),
                self._airtable_csv_row(
                    **{"Business Name": "No Packet LLC", "Packet Eligible": "No", "Place Key": "ChIJ888"}
                ),
            ]
            self._write_airtable_csv(csv_path, rows)

            code, stdout, stderr = self._run_upload(
                ["upload_airtable.py", "--input", str(csv_path), "--only-sendable"]
            )

        self.assertEqual(code, 0, stderr)
        planned = self._dry_run_plan(stdout)
        self.assertEqual(len(planned), 1)
        fields = planned[0]["payload"]["fields"]  # type: ignore[index]
        self.assertEqual(fields["Business Name"], "A+ Landscaping LLC")

    def test_airtable_csv_upsert_key_prefers_place_key_then_normalized_phone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            rows = [
                self._airtable_csv_row(),
                self._airtable_csv_row(**{"Business Name": "Phone Only LLC", "Place Key": "", "Phone": "+1 440.555.0101"}),
            ]
            self._write_airtable_csv(csv_path, rows)

            records = upload_airtable.read_input_records(csv_path, only_sendable=False, packet_dir=Path(tmp))
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
        self.assertIn("No specific observation = no send.", prompt)
        self.assertIn("Do not score A/sendable just because the business is reputable.", prompt)
        self.assertIn("A strong estimate form does not globally disqualify a lead.", prompt)
        self.assertIn("It only blocks missing-form, no-contact-path, and no-estimate-path claims.", prompt)
        self.assertIn("Do not use Estimate Funnel if the form is strong unless the actual estimate path is still visibly weak.", prompt)
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
        self.assertIn("No specific observation = no send.", prompt)
        self.assertIn("A strong estimate form does not globally disqualify a lead.", prompt)
        self.assertIn("Premium Perception Gap", prompt)
        self.assertIn("Portfolio Rebuild", prompt)
        self.assertIn('"send_trigger": ""', prompt)
        self.assertEqual(expected_files, ["chij123.json", "chij456.json"])

    def test_llm_batch_only_candidates_ranks_visible_pain_before_high_review_decent_site(self) -> None:
        high_review_decent = {
            "google": {
                **self._google_fixture(),
                "business_name": "Polished High Review Hardscapes",
                "reviews": "180",
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

    def test_manual_score_loader_accepts_new_schema_and_normalizes_mismatched_total(self) -> None:
        raw_score = self._manual_score(total_score=1, grade="Skip")

        with tempfile.TemporaryDirectory() as tmp:
            score_path = Path(tmp) / "lead.json"
            score_path.write_text(json.dumps(raw_score), encoding="utf-8")
            validated = load_manual_score(score_path, allowed_evidence_ids={"E1", "E2"})

        self.assertEqual(validated["total_score"], 75)
        self.assertEqual(validated["grade"], "B")
        self.assertEqual(validated["confidence"], "medium")

    def test_manual_score_loader_still_accepts_old_schema_without_total_or_grade(self) -> None:
        raw_score = self._manual_score()
        raw_score.pop("total_score", None)
        raw_score.pop("grade", None)

        with tempfile.TemporaryDirectory() as tmp:
            score_path = Path(tmp) / "lead.json"
            score_path.write_text(json.dumps(raw_score), encoding="utf-8")
            validated = load_manual_score(score_path, allowed_evidence_ids={"E1", "E2"})

        self.assertEqual(validated["total_score"], 75)
        self.assertEqual(validated["grade"], "B")

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
            },
            "hardscaping",
        )

        self.assertEqual(row["Lead Priority"], "A")
        self.assertEqual(row["Sendable"], "Yes")
        self.assertEqual(row["Minimum Evidence Passed"], "Yes")
        self.assertEqual(row["Packet Eligible"], "Yes")
        self.assertIn("no owned website or conversion asset", row["Safe Outreach Claims"])

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


if __name__ == "__main__":
    unittest.main()
