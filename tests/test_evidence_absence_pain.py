"""Pain charged for a page that was never read is pain we invented.

`_derive_visible_pain_profile` has two rules that fire off the *absence* of a
field rather than off anything observed, and neither asks whether there was a
page to observe:

- **"No clear estimate CTA" (+14)** triggers on `cta_strength in {none, weak}`,
  and `cta_strength` defaults to `"None"`. A lead with no website at all — and
  a lead whose homepage returned an empty body — both arrive here carrying the
  default, so the charge lands without a single CTA ever having been looked for.
- **"Phone number hard to find" (+8)** triggers on an empty `phones_found`,
  which is *website-scraped phones only*. The Google Business Profile phone is
  never consulted, so the reason fires on all 57 no-website leads in the corpus
  whose number is sitting right there on the GBP.

`derive_visual_audit_scores` already solved exactly this for the seven visual
fields: no parsed page evidence, no derived score, because a derived score
reads to the model as a fact and an Unknown does not. These tests hold the
form/CTA half of the profile to the same rule.

Neither charge flips a verdict on its own — the 40-point no-site reason clears
every threshold by itself — but both reach the packet copy a rep reads aloud,
and "we noticed your phone number is hard to find" said to someone whose phone
number is the first line of their Google listing is the kind of thing that ends
a call.
"""
from __future__ import annotations

import unittest

from enrich_leads import empty_site_result
from lead_machine.evidence import build_evidence_profile

NO_CTA_REASON = "No clear estimate CTA"
NO_PHONE_REASON = "Phone number hard to find"


def _profile(site: dict) -> dict:
    return build_evidence_profile(
        {
            "google": {"business_name": "Test Co", "reviews": 40, "rating": 4.7, "phone": "(216) 555-0142"},
            "website": site,
            "derived": {},
        }
    )


class AbsencePainNeedsAPageTest(unittest.TestCase):
    def test_a_no_website_lead_is_charged_for_the_missing_site_only(self) -> None:
        profile = _profile(empty_site_result("No Website", ["no_website"]))
        reasons = profile["visible_pain_reasons"]

        self.assertIn("No owned website found", reasons)
        self.assertNotIn(NO_CTA_REASON, reasons)
        self.assertNotIn(NO_PHONE_REASON, reasons)

    def test_a_failed_fetch_is_not_charged_for_cta_or_phone(self) -> None:
        # Same shape, different cause: the fetch failed, so there is no parsed
        # body to read a CTA or a phone number out of.
        profile = _profile(
            empty_site_result(
                "Unknown",
                ["static_fetch_failed", "rendered_check_needed", "manual_visual_review_needed"],
                "https://example.com",
                "Static fetch failed",
            )
        )
        reasons = profile["visible_pain_reasons"]

        self.assertNotIn(NO_CTA_REASON, reasons)
        self.assertNotIn(NO_PHONE_REASON, reasons)

    def test_an_empty_body_on_200_is_not_charged_for_cta_or_phone(self) -> None:
        # The Door Works shape: HTTP 200, a title, and nothing in <body>. The
        # document has "visible text" (the <title> survives `get_text()`), so
        # the generic parsed-evidence guard passes — the flag the enricher
        # writes is what says the body itself was empty.
        site = {
            "site_status": "Loaded",
            "site_issue_flags": ["empty_body_on_200"],
            "final_url": "https://example.com",
            "homepage": {"url": "https://example.com", "text": "Door Works | Garage Door Repair"},
            "pages": {},
            "page_structure": {},
            "combined_text": "Door Works | Garage Door Repair",
            "emails_found": [],
            "phones_found": [],
            "forms_found": False,
            "contact_form_detected": "Unknown",
            "estimate_form_detected": "Unknown",
            "form_strength": "Unknown",
            "form_detection_method": "no_body",
            "cta_strength": "Unknown",
            "cta_terms": [],
        }
        reasons = _profile(site)["visible_pain_reasons"]

        self.assertNotIn(NO_CTA_REASON, reasons)
        self.assertNotIn(NO_PHONE_REASON, reasons)

    def test_a_parsed_page_with_no_cta_and_no_phone_is_still_charged(self) -> None:
        # The control. The guard is about whether we read the page, not about
        # softening the finding — a site we read end to end that has no CTA and
        # no visible phone number is exactly the lead this pipeline exists for.
        site = {
            "site_status": "Loaded",
            "site_issue_flags": ["no_clear_cta_detected"],
            "final_url": "https://example.com",
            "homepage": {"url": "https://example.com", "text": "We install garage doors across Northeast Ohio."},
            "pages": {},
            "page_structure": {"headings": ["Our work"], "image_count": 3},
            "combined_text": "We install garage doors across Northeast Ohio.",
            "emails_found": [],
            "phones_found": [],
            "forms_found": False,
            "contact_form_detected": "No",
            "estimate_form_detected": "No",
            "form_strength": "None",
            "form_detection_method": "static_html",
            "cta_strength": "None",
            "cta_terms": [],
        }
        reasons = _profile(site)["visible_pain_reasons"]

        self.assertIn(NO_CTA_REASON, reasons)
        self.assertIn(NO_PHONE_REASON, reasons)


if __name__ == "__main__":
    unittest.main()
