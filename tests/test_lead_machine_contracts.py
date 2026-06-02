from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from lead_machine.airtable_sync import build_airtable_payload, plan_airtable_sync
from lead_machine.evidence import build_evidence_profile
from lead_machine.feedback import compute_feedback_metrics
from lead_machine.icp import hard_icp_gate
from lead_machine.lead_card import build_lead_card
from lead_machine.llm_queue import build_llm_prompt, write_review_queue
from lead_machine.manual_scores import load_manual_score, merge_manual_score
from lead_machine.schema import normalize_lead, row_dedupe_keys
from lead_machine.sendability import flatten_safe_outreach_claims, normalize_evidence_refs
from make_packets import is_eligible
from score_leads import score_record, score_records_manual, write_csv


class LeadMachineContractsTest(unittest.TestCase):
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
        self.assertEqual(card["grade"], "A")
        self.assertEqual(card["best_angle"], "Social/Jobber Escape")
        self.assertEqual(card["airtable_fields"]["Lead Priority"], "A")
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
            },
        }

        payload = build_airtable_payload(card)
        self.assertEqual(payload["fields"]["Place Key"], "ChIJ123")
        self.assertEqual(payload["fields"]["Lead Priority"], "A")

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
            "evidence_items": evidence_items
            or [
                {"id": "E1", "field": "google_rating", "value": "4.9 stars / 130 reviews"},
                {"id": "E2", "field": "site_issue_flags", "value": "no_clear_cta_detected"},
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
