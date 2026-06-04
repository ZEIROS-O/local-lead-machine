from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lead_machine.icp_lanes import classify_icp_lane
from make_icp_lane_report import write_lane_report
from make_llm_batch import write_llm_batches


def _record(
    *,
    business_name: str = "A+ Patio Pros",
    website_url: str = "https://example.com",
    phone: str = "(440) 555-0100",
    rating: str = "4.9",
    reviews: str = "19",
    category: str = "landscaper",
    site_status: str = "okay",
    rendered_site_status: str = "Loaded",
    flags: list[str] | None = None,
    categories: list[str] | None = None,
    visible_pain_score: int = 0,
    visible_pain_reasons: list[str] | None = None,
    send_trigger: str = "",
    estimate_form_detected: str = "Yes",
    contact_form_detected: str = "Yes",
    form_strength: str = "Strong",
    cta_strength: str = "Strong",
    has_estimate_cta: bool = True,
    source_query: str = "hardscaping Cleveland OH",
    vertical: str = "hardscaping",
    combined_text: str = "hardscape patio retaining wall request an estimate gallery reviews",
    high_ticket_terms: list[str] | None = None,
    emails_found: list[str] | None = None,
    place_id: str = "ChIJLANE",
) -> dict[str, object]:
    flags = flags or []
    categories = categories or []
    visible_pain_reasons = visible_pain_reasons or []
    high_ticket_terms = high_ticket_terms if high_ticket_terms is not None else ["hardscape", "patio"]
    emails_found = emails_found if emails_found is not None else ["office@example.com"]
    return {
        "google": {
            "business_name": business_name,
            "website_url": website_url,
            "phone": phone,
            "rating": rating,
            "reviews": reviews,
            "category": category,
            "source_query": source_query,
            "vertical": vertical,
            "place_id": place_id,
        },
        "website": {
            "site_status": site_status,
            "rendered_site_status": rendered_site_status,
            "site_issue_flags": flags,
            "combined_text": combined_text,
            "estimate_form_detected": estimate_form_detected,
            "contact_form_detected": contact_form_detected,
            "form_strength": form_strength,
            "cta_strength": cta_strength,
            "forms_found": contact_form_detected.lower() == "yes",
            "emails_found": emails_found,
            "phones_found": [phone] if phone else [],
            "high_ticket_terms": high_ticket_terms,
        },
        "evidence": {
            "site_status": site_status,
            "rendered_site_status": rendered_site_status,
            "raw_flags": flags,
            "visible_pain_categories": categories,
            "visible_pain_score": visible_pain_score,
            "visible_pain_reasons": visible_pain_reasons,
            "send_trigger": send_trigger,
            "estimate_form_detected": estimate_form_detected,
            "contact_form_detected": contact_form_detected,
            "form_strength": form_strength,
            "cta_strength": cta_strength,
            "has_estimate_cta": has_estimate_cta,
            "phone_visible": bool(phone),
            "evidence_items": [
                {"id": "ev-rating", "field": "google_rating", "value": f"{rating} / {reviews}"},
                {"id": "ev-status", "field": "site_status", "value": site_status},
                {"id": "ev-flags", "field": "site_issue_flags", "value": ", ".join(flags)},
                {"id": "ev-pain", "field": "visible_pain_categories", "value": ", ".join(categories)},
                {"id": "ev-trigger", "field": "send_trigger", "value": send_trigger},
            ],
        },
    }


class ICPLaneTest(unittest.TestCase):
    def test_missing_website_with_demand_and_phone_is_no_site_launch(self) -> None:
        result = classify_icp_lane(
            _record(
                website_url="",
                site_status="missing",
                rendered_site_status="Unknown",
                estimate_form_detected="Unknown",
                contact_form_detected="No",
                form_strength="None",
                visible_pain_score=40,
                visible_pain_reasons=["No owned website found"],
                categories=["no_site_or_social_only_pain"],
                send_trigger="No owned website found",
            )
        )

        self.assertEqual(result.lane, "no_site_launch")
        self.assertNotEqual(result.tier, "Skip")

    def test_jobber_clienthub_url_is_social_jobber_escape(self) -> None:
        result = classify_icp_lane(
            _record(
                website_url="https://clienthub.getjobber.com/public/request/example",
                site_status="jobber_only",
                reviews="130",
                visible_pain_score=32,
                visible_pain_reasons=["Jobber-only web presence"],
                categories=["no_site_or_social_only_pain"],
                send_trigger="Jobber-only web presence",
            )
        )

        self.assertEqual(result.lane, "social_jobber_escape")
        self.assertNotEqual(result.lane, "owned_weak_site_rebuild")

    def test_facebook_url_is_social_jobber_escape(self) -> None:
        result = classify_icp_lane(
            _record(
                website_url="https://www.facebook.com/a-plus-patios",
                site_status="social_only",
                reviews="54",
                visible_pain_score=35,
                visible_pain_reasons=["Social-only web presence"],
                categories=["no_site_or_social_only_pain"],
                send_trigger="Social-only web presence",
            )
        )

        self.assertEqual(result.lane, "social_jobber_escape")

    def test_microsite_urls_are_social_jobber_escape_not_estimate_funnel(self) -> None:
        for website_url in (
            "https://jb-restoration.godaddysites.com",
            "https://jbrestoration.wordpress.com",
            "https://jbrestoration.wixsite.com/home",
        ):
            with self.subTest(website_url=website_url):
                result = classify_icp_lane(
                    _record(
                        website_url=website_url,
                        site_status="okay",
                        flags=["weak_basic_form"],
                        categories=["estimate_path_pain"],
                        visible_pain_score=28,
                        visible_pain_reasons=["Basic estimate form only"],
                        send_trigger="Basic estimate form only",
                        form_strength="Basic",
                    )
                )

                self.assertEqual(result.lane, "social_jobber_escape")
                self.assertNotEqual(result.lane, "estimate_funnel")

    def test_hardscaping_estimate_funnel_caps_generic_contractor_without_service_evidence(self) -> None:
        result = classify_icp_lane(
            _record(
                business_name="Fraser Contracting",
                category="General contractor",
                reviews="54",
                flags=["missing_estimate_path"],
                categories=["estimate_path_pain"],
                visible_pain_score=32,
                visible_pain_reasons=["No estimate form detected"],
                send_trigger="No estimate form detected",
                estimate_form_detected="No",
                contact_form_detected="Yes",
                form_strength="None",
                cta_strength="Strong",
                has_estimate_cta=True,
                combined_text="general construction repair additions home improvement call for a quote",
                high_ticket_terms=[],
            )
        )

        self.assertEqual(result.lane, "estimate_funnel")
        self.assertNotEqual(result.tier, "A")
        self.assertLessEqual(result.score, 64)

    def test_no_estimate_form_with_visible_cta_and_contact_path_is_not_automatically_a(self) -> None:
        result = classify_icp_lane(
            _record(
                reviews="54",
                flags=["missing_estimate_path"],
                categories=["estimate_path_pain"],
                visible_pain_score=22,
                visible_pain_reasons=["No estimate form detected"],
                send_trigger="No estimate form detected",
                estimate_form_detected="No",
                contact_form_detected="Yes",
                form_strength="None",
                cta_strength="Strong",
                has_estimate_cta=True,
            )
        )

        self.assertEqual(result.lane, "estimate_funnel")
        self.assertEqual(result.tier, "B")

    def test_strong_service_fit_basic_estimate_path_can_still_be_b_or_a(self) -> None:
        result = classify_icp_lane(
            _record(
                reviews="42",
                flags=["weak_basic_form"],
                categories=["estimate_path_pain"],
                visible_pain_score=24,
                visible_pain_reasons=["Basic estimate form only"],
                send_trigger="Basic estimate form only",
                form_strength="Basic",
                combined_text=(
                    "hardscaping patio pavers retaining wall outdoor living "
                    "landscape design request an estimate"
                ),
                high_ticket_terms=["hardscaping", "patio", "pavers", "retaining wall", "outdoor living"],
            )
        )

        self.assertEqual(result.lane, "estimate_funnel")
        self.assertIn(result.tier, {"A", "B"})

    def test_owned_weak_site_without_visual_status_can_populate_rebuild_lane(self) -> None:
        result = classify_icp_lane(
            _record(
                site_status="weak",
                rendered_site_status="Unknown",
                flags=["generic_copy_signals", "weak_services_page"],
                categories=["service_clarity_pain"],
                visible_pain_score=24,
                visible_pain_reasons=["Unclear service presentation"],
                send_trigger="Unclear service presentation",
            )
        )

        self.assertEqual(result.lane, "owned_weak_site_rebuild")

    def test_151_plus_review_basic_form_only_is_optimization_not_a_estimate_funnel(self) -> None:
        result = classify_icp_lane(
            _record(
                reviews="180",
                site_status="okay",
                flags=["weak_basic_form"],
                categories=["estimate_path_pain"],
                visible_pain_score=35,
                visible_pain_reasons=["Basic estimate form only"],
                send_trigger="Basic estimate form only",
                form_strength="Basic",
            )
        )

        self.assertEqual(result.lane, "optimization_only")
        self.assertNotEqual(result.tier, "A")

    def test_76_plus_review_basic_form_only_requires_clearer_pain(self) -> None:
        result = classify_icp_lane(
            _record(
                reviews="104",
                site_status="okay",
                flags=["weak_basic_form"],
                categories=["estimate_path_pain"],
                visible_pain_score=28,
                visible_pain_reasons=["Basic estimate form only"],
                send_trigger="Basic estimate form only",
                form_strength="Basic",
            )
        )

        self.assertEqual(result.lane, "optimization_only")
        self.assertNotEqual(result.tier, "A")

    def test_owned_weak_loaded_site_with_no_form_is_rebuild_or_estimate_funnel(self) -> None:
        result = classify_icp_lane(
            _record(
                site_status="weak",
                flags=["no_form_detected", "no_clear_cta_detected"],
                categories=["estimate_path_pain"],
                visible_pain_score=34,
                visible_pain_reasons=["No estimate form detected", "No clear estimate CTA"],
                send_trigger="No estimate form detected",
                estimate_form_detected="No",
                contact_form_detected="No",
                form_strength="None",
                cta_strength="Weak",
                has_estimate_cta=False,
            )
        )

        self.assertIn(result.lane, {"owned_weak_site_rebuild", "estimate_funnel"})
        self.assertNotEqual(result.lane, "skip")

    def test_owned_outdated_site_flag_is_premium_perception_gap(self) -> None:
        result = classify_icp_lane(
            _record(
                site_status="weak",
                flags=["outdated_site"],
                categories=["premium_perception_gap"],
                visible_pain_score=22,
                visible_pain_reasons=["Website looks outdated"],
                send_trigger="Website looks outdated",
            )
        )

        self.assertEqual(result.lane, "premium_perception_gap")

    def test_owned_weak_gallery_flag_is_portfolio_presentation_gap(self) -> None:
        result = classify_icp_lane(
            _record(
                site_status="weak",
                flags=["weak_gallery"],
                categories=["portfolio_presentation_pain"],
                visible_pain_score=22,
                visible_pain_reasons=["Project photos are buried or weak for high-ticket work"],
                send_trigger="Project photos are buried or weak for high-ticket work",
            )
        )

        self.assertEqual(result.lane, "portfolio_presentation_gap")

    def test_okay_site_basic_form_high_review_is_not_a_rebuild(self) -> None:
        result = classify_icp_lane(
            _record(
                reviews="180",
                site_status="okay",
                flags=["weak_basic_form"],
                categories=["estimate_path_pain"],
                visible_pain_score=12,
                visible_pain_reasons=["Basic estimate form only"],
                send_trigger="Basic estimate form only",
                form_strength="Basic",
            )
        )

        self.assertIn(result.lane, {"estimate_funnel", "optimization_only"})
        self.assertNotEqual(result.lane, "owned_weak_site_rebuild")
        self.assertNotEqual(result.tier, "A")

    def test_strong_estimate_form_with_no_other_pain_is_not_rebuild(self) -> None:
        result = classify_icp_lane(_record(reviews="42"))

        self.assertIn(result.lane, {"skip", "optimization_only"})
        self.assertNotEqual(result.lane, "owned_weak_site_rebuild")

    def test_151_plus_review_weak_signal_only_is_not_a(self) -> None:
        result = classify_icp_lane(
            _record(
                reviews="210",
                site_status="weak",
                flags=["generic_copy_signals"],
                visible_pain_score=7,
                visible_pain_reasons=["Generic/template website copy"],
                send_trigger="Generic/template website copy",
            )
        )

        self.assertNotEqual(result.tier, "A")
        self.assertLess(result.score, 80)

    def test_llm_batch_can_filter_by_lane_and_includes_lane_metadata(self) -> None:
        no_site = _record(
            business_name="No Site Patio",
            website_url="",
            site_status="missing",
            rendered_site_status="Unknown",
            categories=["no_site_or_social_only_pain"],
            visible_pain_score=40,
            visible_pain_reasons=["No owned website found"],
            send_trigger="No owned website found",
            place_id="ChIJNOSITE",
        )
        premium = _record(
            business_name="Premium Patio",
            flags=["outdated_site"],
            categories=["premium_perception_gap"],
            visible_pain_score=22,
            visible_pain_reasons=["Website looks outdated"],
            send_trigger="Website looks outdated",
            place_id="ChIJPREMIUM",
        )

        with tempfile.TemporaryDirectory() as tmp:
            writes = write_llm_batches(
                [no_site, premium],
                Path(tmp),
                fallback_vertical="hardscaping",
                batch_size=5,
                lane="premium_perception_gap",
            )
            prompt = writes[0].prompt_path.read_text(encoding="utf-8")
            expected_files = writes[0].expected_files_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(expected_files, ["chijpremium.json"])
        self.assertIn('"icp_lane": "premium_perception_gap"', prompt)
        self.assertIn('"icp_lane_score"', prompt)
        self.assertNotIn("No Site Patio", prompt)

    def test_lane_report_lists_counts_and_top_records(self) -> None:
        no_site = _record(
            business_name="No Site Patio",
            website_url="",
            site_status="missing",
            rendered_site_status="Unknown",
            categories=["no_site_or_social_only_pain"],
            visible_pain_score=40,
            visible_pain_reasons=["No owned website found"],
            send_trigger="No owned website found",
            place_id="ChIJNOSITE",
        )
        premium = _record(
            business_name="Premium Patio",
            flags=["outdated_site"],
            categories=["premium_perception_gap"],
            visible_pain_score=22,
            visible_pain_reasons=["Website looks outdated"],
            send_trigger="Website looks outdated",
            place_id="ChIJPREMIUM",
        )

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reports/icp_lanes_test.md"
            write_lane_report([no_site, premium], output, input_label="test.jsonl")
            report = output.read_text(encoding="utf-8")

        self.assertIn("no_site_launch: 1", report)
        self.assertIn("premium_perception_gap: 1", report)
        self.assertIn("No Site Patio", report)
        self.assertIn("Premium Patio", report)
        self.assertIn("Website looks outdated", report)


if __name__ == "__main__":
    unittest.main()
