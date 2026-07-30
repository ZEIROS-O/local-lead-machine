"""The seven visual-audit fields were the literal string "Unknown" on every lead.

One writer set them — an all-Unknown initializer in the enricher — and nothing
ever overwrote it, so every review packet carried seven dead keys into the
prompt as noise. These tests hold the two properties that make filling them
worth anything: the values are derived from evidence the crawl really collected,
and a lead whose page was never read still says Unknown instead of inventing a
grade. A confident score derived from nothing is worse than no score, because
the model reads it as a fact.
"""

import unittest
from pathlib import Path
from unittest.mock import patch

import enrich_leads
from lead_machine.evidence import (
    VISUAL_AESTHETIC_CEILING,
    VISUAL_AUDIT_SCORE_FIELDS,
    VISUAL_SCORE_SCALE,
    VISUAL_UNKNOWN,
    build_evidence_profile,
    derive_visual_audit_scores,
)


RICH_HOMEPAGE = """
<html><head>
  <title>Cleveland Paver Patios and Retaining Walls</title>
  <meta name="description" content="Paver patios, retaining walls and outdoor living in Cleveland.">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head><body>
  <h1>Paver Patio and Retaining Wall Builders in Cleveland</h1>
  <a href="/request-estimate">Request a Free Estimate</a>
  <a href="/gallery">Project Gallery</a>
  <p>Licensed and insured. Family owned. Read our reviews before you request a free estimate.</p>
  <h2>Outdoor living</h2><h2>Drainage</h2><h3>Fire pit</h3>
  <img src="1.jpg"><img src="2.jpg"><img src="3.jpg"><img src="4.jpg"><img src="5.jpg">
  <p>Call (216) 555-0134 today.</p>
</body></html>
"""

GALLERY_PAGE = "<html><body><h1>Gallery</h1>" + ("<img src='g.jpg'>" * 10) + "</body></html>"

ESTIMATE_PAGE = """
<html><body>
  <h1>Request an estimate</h1>
  <form>
    <input name="first_name"><input type="email" name="email"><input type="tel" name="phone">
    <input name="address"><textarea name="service_details"></textarea>
    <button type="submit">Submit</button>
  </form>
</body></html>
"""

DATED_HOMEPAGE = """
<html><head><title>Home</title></head><body bgcolor="#ffffff">
  <center><font size="5">Welcome to our website</font></center>
  <p>We pride ourselves on quality workmanship and customer satisfaction.</p>
</body></html>
"""


def _fetch_stub(pages: dict) -> callable:
    def fake_fetch(url: str, timeout: int = 15) -> dict:
        html = pages.get(url)
        if html is None:
            return {"ok": False, "status_code": 404, "final_url": url, "html": "", "error": ""}
        return {"ok": True, "status_code": 200, "final_url": url, "html": html, "error": ""}

    return fake_fetch


def _scrape(pages: dict, url: str = "https://example.com") -> dict:
    with patch("enrich_leads.fetch_url", side_effect=_fetch_stub(pages)):
        return enrich_leads.scrape_site(url)


def _loaded_site(**overrides) -> dict:
    """A minimal crawl result the deriver is willing to grade."""
    site = {
        "site_status": "Loaded",
        "rendered_site_status": "Loaded",
        "site_issue_flags": [],
        "final_url": "https://example.com",
        "homepage": {"url": "https://example.com", "title": "Patios", "meta": "", "text": "paver patio work"},
        "pages": {},
        "combined_text": "paver patio work",
        "page_structure": {
            "h1_texts": ["Paver Patios"],
            "heading_count": 4,
            "image_count": 6,
            "has_viewport_meta": True,
            "responsive_viewport": True,
            "legacy_markup": [],
        },
        "high_ticket_terms": ["paver", "patio"],
        "generic_copy_terms": [],
        "cta_terms": [],
        "phones_found": [],
        "form_strength": "None",
    }
    site.update(overrides)
    return site


class RichEvidenceTests(unittest.TestCase):
    def test_a_fully_crawled_site_scores_every_field(self) -> None:
        site = _scrape(
            {
                "https://example.com": RICH_HOMEPAGE,
                "https://example.com/gallery": GALLERY_PAGE,
                "https://example.com/request-estimate": ESTIMATE_PAGE,
            }
        )

        scores = {field: site[field] for field in VISUAL_AUDIT_SCORE_FIELDS}

        self.assertNotIn(VISUAL_UNKNOWN, scores.values(), scores)
        for field, value in scores.items():
            self.assertIn(value, VISUAL_SCORE_SCALE, f"{field}={value}")

    def test_every_score_stays_on_the_one_published_scale(self) -> None:
        """The prompt gets these next to form_strength and cta_strength in the
        same JSON blob, so a private per-field scale would be undecodable."""
        allowed = set(VISUAL_SCORE_SCALE) | {VISUAL_UNKNOWN}
        cases = [
            _scrape({"https://example.com": RICH_HOMEPAGE}),
            _scrape({"https://example.com": DATED_HOMEPAGE}),
            _scrape({}),
            enrich_leads.empty_site_result("No Website", ["no_website"]),
        ]

        for site in cases:
            for field in VISUAL_AUDIT_SCORE_FIELDS:
                self.assertIn(site[field], allowed, f"{field}={site[field]}")

    def test_dated_markup_reads_as_a_weak_page(self) -> None:
        site = _scrape({"https://example.com": DATED_HOMEPAGE})

        self.assertEqual(site["visual_quality_score"], "Weak")
        self.assertIn("font", site["page_structure"]["legacy_markup"])

    def test_a_page_with_no_viewport_meta_scores_no_mobile_handling(self) -> None:
        """This is the one mobile claim markup can prove on its own: without a
        device-width viewport the page is served to phones zoomed out."""
        site = _scrape({"https://example.com": DATED_HOMEPAGE})

        self.assertEqual(site["mobile_visual_score"], "None")

    def test_a_mobile_render_is_what_lifts_the_mobile_score_to_strong(self) -> None:
        without_render = derive_visual_audit_scores(_loaded_site())
        with_render = derive_visual_audit_scores(
            _loaded_site(mobile_screenshot_path="screenshots/lead-mobile.png")
        )

        self.assertEqual(without_render["mobile_visual_score"], "Basic")
        self.assertEqual(with_render["mobile_visual_score"], "Strong")

    def test_trust_proof_only_on_a_deep_page_reads_as_buried(self) -> None:
        homepage_only = _loaded_site(
            homepage={"url": "https://example.com", "title": "", "meta": "", "text": "licensed and insured"},
        )
        buried = _loaded_site(
            pages={"about": {"url": "https://example.com/about", "ok": True, "text": "Licensed and insured"}},
        )
        nowhere = _loaded_site()

        self.assertEqual(derive_visual_audit_scores(homepage_only)["trust_proof_visibility_score"], "Basic")
        self.assertEqual(derive_visual_audit_scores(buried)["trust_proof_visibility_score"], "Weak")
        self.assertEqual(derive_visual_audit_scores(nowhere)["trust_proof_visibility_score"], "None")

    def test_none_and_unknown_are_different_claims(self) -> None:
        """"None" is a leak worth selling against: we read the page and the
        thing is absent. "Unknown" is worth nothing. Collapsing them would turn
        every unread page into a finding."""
        no_project_photos = _loaded_site()
        no_project_photos["page_structure"] = dict(no_project_photos["page_structure"], image_count=0)
        read_but_empty = derive_visual_audit_scores(no_project_photos)
        never_read = derive_visual_audit_scores(enrich_leads.empty_site_result("No Website", ["no_website"]))

        self.assertEqual(read_but_empty["gallery_visibility_score"], "None")
        self.assertEqual(never_read["gallery_visibility_score"], VISUAL_UNKNOWN)


class UngroundedLeadTests(unittest.TestCase):
    def test_browser_loaded_after_static_fetch_failure_stays_unknown_without_dom_evidence(self) -> None:
        """A screenshot path proves that Chromium rendered pixels, but this
        deriver never opens those pixels. Without parsed DOM/copy it has no
        evidence for None/Weak claims about hero, CTA, trust, or perception."""
        rendered_only = _loaded_site(
            homepage={},
            pages={},
            combined_text="",
            page_structure={},
            high_ticket_terms=[],
            generic_copy_terms=[],
            cta_terms=[],
            phones_found=[],
            site_issue_flags=["static_fetch_failed", "manual_visual_review_needed"],
            desktop_screenshot_path="screenshots/lead-desktop.png",
            mobile_screenshot_path="screenshots/lead-mobile.png",
        )

        scores = derive_visual_audit_scores(rendered_only)

        self.assertEqual(set(scores.values()), {VISUAL_UNKNOWN})

    def test_a_site_that_never_loaded_stays_unknown(self) -> None:
        site = _scrape({})

        for field in VISUAL_AUDIT_SCORE_FIELDS:
            self.assertEqual(site[field], VISUAL_UNKNOWN, field)

    def test_a_lead_with_no_website_stays_unknown(self) -> None:
        site = enrich_leads.scrape_site("")

        self.assertEqual(site["site_status"], "No Website")
        for field in VISUAL_AUDIT_SCORE_FIELDS:
            self.assertEqual(site[field], VISUAL_UNKNOWN, field)

    def test_a_broken_site_is_not_graded_as_a_bad_looking_one(self) -> None:
        broken = _loaded_site(site_status="Broken Website", rendered_site_status="Broken")

        scores = derive_visual_audit_scores(broken)

        self.assertEqual(set(scores.values()), {VISUAL_UNKNOWN})

    def test_a_social_only_profile_is_not_graded_as_the_prospects_site(self) -> None:
        """Grading facebook.com would describe Facebook's design, not theirs."""
        social = _loaded_site(
            site_status="Social Only",
            rendered_site_status="Unknown",
            final_url="https://facebook.com/patio-pros",
        )

        scores = derive_visual_audit_scores(social)

        self.assertEqual(set(scores.values()), {VISUAL_UNKNOWN})

    def test_unparsed_markup_leaves_the_markup_only_fields_unknown(self) -> None:
        """An old enriched record has text but no page_structure. Reading a
        missing structure as "no images, no headings" would grade every one of
        those leads as a bad-looking site."""
        no_structure = _loaded_site(page_structure={})

        scores = derive_visual_audit_scores(no_structure)

        self.assertEqual(scores["visual_quality_score"], VISUAL_UNKNOWN)
        self.assertEqual(scores["mobile_visual_score"], VISUAL_UNKNOWN)
        # No gallery page and no gallery wording, but the homepage images were
        # never counted, so "no project photos anywhere" is not provable.
        self.assertEqual(scores["gallery_visibility_score"], VISUAL_UNKNOWN)
        # Copy-grounded fields still score: their evidence was collected.
        self.assertNotEqual(scores["trust_proof_visibility_score"], VISUAL_UNKNOWN)
        self.assertNotEqual(scores["premium_perception_score"], VISUAL_UNKNOWN)

    def test_an_aesthetic_call_never_claims_strong_without_eyes_on_the_page(self) -> None:
        """Markup can prove a page is dated or thin. Nothing short of a
        screenshot can prove it looks good."""
        best_case = _loaded_site(
            high_ticket_terms=["paver", "patio", "retaining wall", "outdoor kitchen"],
            combined_text="paver patio retaining wall licensed insured award reviews gallery cleveland",
            homepage={
                "url": "https://example.com",
                "title": "Cleveland Paver Patios",
                "meta": "",
                "text": "licensed insured award winning reviews gallery cleveland paver patio",
            },
            pages={"gallery": {"url": "https://example.com/gallery", "ok": True, "text": "", "image_count": 40}},
            page_structure={
                "h1_texts": ["Paver Patios in Cleveland"],
                "heading_count": 12,
                "image_count": 40,
                "has_viewport_meta": True,
                "responsive_viewport": True,
                "legacy_markup": [],
            },
        )

        scores = derive_visual_audit_scores(best_case)

        for field, ceiling in VISUAL_AESTHETIC_CEILING.items():
            self.assertEqual(scores[field], ceiling, field)

    def test_a_score_someone_else_already_set_is_not_overwritten(self) -> None:
        """A human review or a later vision pass actually saw the page; this
        deriver did not, so it must not argue with them."""
        reviewed = _loaded_site(visual_quality_score="Strong", hero_clarity_score="None")

        scores = derive_visual_audit_scores(reviewed)

        self.assertEqual(scores["visual_quality_score"], "Strong")
        self.assertEqual(scores["hero_clarity_score"], "None")


class EnrichRowTests(unittest.TestCase):
    def test_the_enriched_record_carries_real_scores(self) -> None:
        row = {"business_name": "Patio Pros", "place_id": "place-1", "website_url": "https://example.com"}
        pages = {
            "https://example.com": RICH_HOMEPAGE,
            "https://example.com/gallery": GALLERY_PAGE,
            "https://example.com/request-estimate": ESTIMATE_PAGE,
        }

        with patch("enrich_leads.fetch_url", side_effect=_fetch_stub(pages)), patch(
            "enrich_leads.build_buyer_intent"
        ):
            record = enrich_leads.enrich_row(row, Path("fake"), take_shots=False)

        scores = {field: record["website"][field] for field in VISUAL_AUDIT_SCORE_FIELDS}

        self.assertNotIn(VISUAL_UNKNOWN, scores.values(), scores)

    def test_the_evidence_profile_scores_records_enriched_before_this_existed(self) -> None:
        """Every enriched JSONL already on disk carries the all-Unknown block.
        Deriving at profile-build time is what stops those leads needing a
        re-crawl to be worth scoring."""
        stale = {"website": _loaded_site(**{field: VISUAL_UNKNOWN for field in VISUAL_AUDIT_SCORE_FIELDS})}

        profile = build_evidence_profile(stale)

        self.assertNotEqual(profile["visual_quality_score"], VISUAL_UNKNOWN)
        self.assertNotEqual(profile["hero_clarity_score"], VISUAL_UNKNOWN)


class FieldListDriftTests(unittest.TestCase):
    def test_the_enricher_and_the_evidence_profile_share_one_field_list(self) -> None:
        """These were two hand-maintained copies. A field added to one and not
        the other is written by the enricher and dropped by the profile — or
        graded by the profile and never initialised — with nothing failing."""
        from lead_machine import evidence

        self.assertIs(enrich_leads.VISUAL_AUDIT_SCORE_FIELDS, evidence.VISUAL_AUDIT_SCORE_FIELDS)

    def test_the_initializer_covers_exactly_the_declared_fields(self) -> None:
        empty = enrich_leads._empty_visual_audit_fields()

        self.assertEqual(
            sorted(key for key in empty if key.endswith("_score")),
            sorted(VISUAL_AUDIT_SCORE_FIELDS),
        )

    def test_the_deriver_returns_exactly_the_declared_fields(self) -> None:
        scores = derive_visual_audit_scores(_loaded_site())

        self.assertEqual(sorted(scores), sorted(VISUAL_AUDIT_SCORE_FIELDS))


if __name__ == "__main__":
    unittest.main()
