"""The scorer names Cleveland and hardscaping in logic that runs 11 metros
and 6 verticals.

Two hardcodings, both silent. The service-area weakness check knew four Ohio
place names, so every business in the other ten metros took a false +4 penalty
— and weakness gates the offer-fit branches at 10/12/16/18, so it could change
the offer a lead is pitched. `infer_vertical` knew four trades and checked
"roof" against the full scraped site text first, so any business whose copy
mentioned a roof was labelled Roofing.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from lead_machine.scoring_profiles import (
    ScoringProfileError,
    load_scoring_profile,
    market_service_area_terms,
)
from lead_machine.verticals import (
    HARDSCAPE_FRANCHISE_DENYLIST,
    VERTICAL_PROFILES,
    VerticalProfile,
    _infer_profile_key,
    _normalize_key,
    matches_term,
    resolve_profile,
)
from score_leads import (
    GENERIC_SERVICE_AREA_TERMS,
    infer_vertical,
    is_corporate_location_name,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _record(*, city: str = "", text: str = "", name: str = "", vertical: str = "", source_query: str = "") -> dict:
    return {
        "google": {
            "business_name": name,
            "city": city,
            "vertical": vertical,
            "source_query": source_query,
        },
        "website": {"combined_text": text, "homepage": {"text": text}, "pages": {}},
    }


class ServiceAreaTermsTest(unittest.TestCase):
    def test_terms_follow_the_lead_metro(self) -> None:
        cleveland = market_service_area_terms(_record(city="Lakewood"))
        columbus = market_service_area_terms(_record(city="Dublin"))

        self.assertIn("lakewood", cleveland)
        self.assertNotIn("lakewood", columbus)

    def test_cleveland_still_covers_every_term_the_hardcoded_list_had(self) -> None:
        # The regression guard: this batch must not take credit away from any
        # Cleveland lead that previously had it.
        previous = {"cleveland", "akron", "chardon", "northeast ohio"}
        terms = set(market_service_area_terms(_record(city="Lakewood")))

        self.assertTrue(previous <= terms, previous - terms)

    def test_an_unresolvable_metro_falls_back_to_the_generic_phrasings(self) -> None:
        # A lead with no known metro behaves exactly as it did before: only
        # "serving", "service area", "areas we serve" and "county" count.
        self.assertEqual(market_service_area_terms(_record(city="Nowhere")), ())
        self.assertIn("serving", GENERIC_SERVICE_AREA_TERMS)

    def test_other_metros_get_their_own_suburbs_not_clevelands(self) -> None:
        cincinnati = market_service_area_terms(_record(city="Blue Ash"))

        self.assertTrue(cincinnati, "Cincinnati should resolve to a market profile")
        self.assertIn("greater cincinnati", cincinnati)


class CorporateLocationNameTest(unittest.TestCase):
    def test_the_pattern_is_recognised_outside_cleveland(self) -> None:
        # The hint list was five Ohio place names, so "Brand of Toledo" simply
        # did not exist as a pattern for ten of the eleven metros.
        toledo = _record(city="Sylvania", name="Storm Guard of Toledo")

        self.assertTrue(is_corporate_location_name("storm guard of toledo", toledo))

    def test_an_owner_led_name_is_not_flagged(self) -> None:
        record = _record(city="Lakewood", name="Sons of Thunder Roofing")

        self.assertFalse(is_corporate_location_name("sons of thunder roofing", record))


class InferVerticalTest(unittest.TestCase):
    def test_a_declared_vertical_outranks_scraped_copy(self) -> None:
        record = _record(vertical="plumbing", text="we also repair the roof over your water heater")

        self.assertEqual(infer_vertical(record, "plumbing"), "Plumbing")

    def test_incidental_roof_copy_no_longer_relabels_the_lead(self) -> None:
        # "roof" was checked first against the whole scraped site text, so a
        # detailer mentioning a sunroof, or a plumber mentioning a roof vent,
        # came out as Roofing.
        record = _record(text="ceramic coating and paint correction; we protect roof rails too")

        self.assertEqual(infer_vertical(record, "detailing"), "Car Detailing")

    def test_concrete_copy_no_longer_forces_masonry(self) -> None:
        record = _record(text="mobile auto detailing; we work on concrete driveways at your home")

        self.assertEqual(infer_vertical(record, "detailing"), "Car Detailing")

    def test_every_registered_vertical_can_be_returned(self) -> None:
        # The old rules could only ever emit four labels; anything else fell
        # through to fallback.title(), which is where "Auto" came from.
        for key, profile in VERTICAL_PROFILES.items():
            with self.subTest(vertical=key):
                self.assertEqual(infer_vertical(_record(vertical=key), key), profile.label)

    def test_an_unknown_fallback_resolves_through_the_registry(self) -> None:
        # "auto" is not a vertical. It used to be title-cased and written to the
        # Vertical column, where Airtable silently dropped it as an unmapped
        # select value.
        label = infer_vertical(_record(text="drain cleaning and sewer line repair"), "auto")

        self.assertIn(label, {profile.label for profile in VERTICAL_PROFILES.values()})

    def test_source_query_resolves_the_vertical_when_nothing_is_declared(self) -> None:
        record = _record(source_query="water damage restoration Cleveland OH")

        self.assertEqual(
            infer_vertical(record, "auto"),
            resolve_profile("", source_query="water damage restoration Cleveland OH").label,
        )


class IcpGateTest(unittest.TestCase):
    def test_the_geo_gate_parameter_is_gone(self) -> None:
        # It defaulted to False, no caller passed True, and the term list it
        # checked was Cleveland-only — so enabling it would have rejected every
        # lead in the other ten metros. Geography is decided by the search
        # configs upstream and by scoring_v3 downstream.
        import inspect

        from lead_machine.icp import hard_icp_gate

        self.assertNotIn("enforce_target_geo", inspect.signature(hard_icp_gate).parameters)

    def test_profiles_no_longer_carry_a_cleveland_term_list(self) -> None:
        for key, profile in VERTICAL_PROFILES.items():
            with self.subTest(vertical=key):
                self.assertFalse(hasattr(profile, "geo_terms"))


class ExplicitNoFallbackTest(unittest.TestCase):
    """`fallback=""` is a caller saying "do not guess", and it was ignored."""

    def test_an_unknown_vertical_refuses_instead_of_becoming_hardscaping(self) -> None:
        # The resolver's last line returned hardscaping unconditionally, so an
        # unknown niche came back as a fully populated hardscaping profile and
        # was scored against hardscaping's terms, franchises and review bounds.
        self.assertEqual(resolve_profile("mobile mechanics", fallback="").key, "")

    def test_the_default_fallback_still_lands_on_hardscaping(self) -> None:
        # The other ~10 call sites take the default and must not move.
        self.assertEqual(resolve_profile("mobile mechanics").key, "hardscaping")
        self.assertEqual(resolve_profile("").key, "hardscaping")
        self.assertEqual(resolve_profile("auto", source_query="").key, "hardscaping")

    def test_a_named_but_unregistered_fallback_still_lands_on_hardscaping(self) -> None:
        # score_leads.infer_vertical passes the batch's own vertical string as
        # the fallback, and it is frequently not a registered key ("auto").
        # Only an *empty* fallback means refuse.
        self.assertEqual(resolve_profile("mobile mechanics", fallback="auto").key, "hardscaping")

    def test_a_resolvable_vertical_is_unaffected_by_the_empty_fallback(self) -> None:
        self.assertEqual(resolve_profile("plumbing", fallback="").key, "plumbing")
        self.assertEqual(
            resolve_profile("", source_query="ceramic coating Cleveland OH", fallback="").key,
            "detailing",
        )

    def test_the_scoring_v3_exact_vertical_guard_is_reachable(self) -> None:
        # This guard was dead code: `resolve_profile(vertical, fallback="").key`
        # could never be empty, so an unknown vertical was silently scored
        # against the hardscaping profile instead of refusing.
        with self.assertRaises(ScoringProfileError) as caught:
            load_scoring_profile("mobile mechanics", "cleveland")

        self.assertIn("Both exact vertical and market are required", str(caught.exception))


class EpoxyAndRoofingProfileTest(unittest.TestCase):
    """Both used to resolve to hardscaping, which is a different sale."""

    def test_each_resolves_to_itself(self) -> None:
        self.assertEqual(resolve_profile("epoxy").key, "epoxy")
        self.assertEqual(resolve_profile("roofing").key, "roofing")
        self.assertEqual(resolve_profile("epoxy flooring").key, "epoxy")
        self.assertEqual(resolve_profile("roofing contractor").key, "roofing")

    def test_epoxy_does_not_fall_back_to_hardscaping_when_guessing_is_off(self) -> None:
        self.assertNotEqual(resolve_profile("epoxy", fallback="").key, "hardscaping")
        self.assertEqual(resolve_profile("epoxy", fallback="").key, "epoxy")

    def test_a_search_query_resolves_to_the_right_one_of_the_two(self) -> None:
        # "concrete floor coating" contains "concrete", which hardscaping claims
        # as a service term, so epoxy batches with no declared vertical used to
        # be inferred as hardscaping.
        self.assertEqual(resolve_profile("", source_query="concrete floor coating Cleveland OH").key, "epoxy")
        self.assertEqual(resolve_profile("", source_query="roof replacement Cleveland OH").key, "roofing")

    def test_the_older_verticals_still_win_their_own_queries(self) -> None:
        # The two new inference branches sit ahead of hardscaping, so prove they
        # did not steal anything already resolving.
        for query, expected in (
            ("ceramic coating Cleveland OH", "detailing"),
            ("water damage restoration Cleveland OH", "water_restoration"),
            ("garage door repair Cleveland OH", "garage_doors"),
            ("concrete patio contractor Cleveland OH", "hardscaping"),
            ("retaining wall Cleveland OH", "hardscaping"),
        ):
            with self.subTest(query=query):
                self.assertEqual(resolve_profile("", source_query=query).key, expected)

    def test_epoxy_stops_borrowing_hardscapings_deny_list(self) -> None:
        # A resin/countertop retailer ranks for the same queries as a floor
        # coating contractor. Under the hardscaping fallback the only deny terms
        # the gate had were med-spa words, so the retailer passed as a lead.
        retailer = "lakeside epoxy supplies"
        hardscaping = resolve_profile("hardscaping")
        epoxy = resolve_profile("epoxy")

        self.assertFalse(
            any(term in retailer for term in hardscaping.deny_terms + hardscaping.low_ticket_terms)
        )
        self.assertTrue(any(term in retailer for term in epoxy.deny_terms + epoxy.low_ticket_terms))

    def test_roofing_keeps_every_franchise_the_hardscaping_fallback_caught(self) -> None:
        # Hardscaping's franchise denylist is entirely exterior-remodel brands
        # (Mr Roof, Erie Home, LeafFilter) precisely because roofing resolved
        # there. Splitting roofing out must not quietly stop skipping them.
        roofing = resolve_profile("roofing").franchise_denylist

        for franchise in HARDSCAPE_FRANCHISE_DENYLIST:
            with self.subTest(franchise=franchise):
                self.assertIn(franchise, roofing)

    def test_no_profile_can_carry_a_review_ceiling(self) -> None:
        # A ceiling silently skips the most established businesses in a metro.
        # detailing set one at 130 and it cost real leads: the only booked call to
        # date (2026-07-28) was a 222-review business. The ceiling is gone as a
        # concept, not just as a number, so this pins the absence of the field
        # rather than each profile's value — re-adding one has to fail here first.
        self.assertNotIn("max_reviews", VerticalProfile.__dataclass_fields__)
        for key, profile in VERTICAL_PROFILES.items():
            with self.subTest(vertical=key):
                self.assertFalse(hasattr(profile, "max_reviews"))

    def test_every_review_floor_matches_its_own_json_profile(self) -> None:
        # Two consumers, two files: generate_search_config writes
        # vertical_profiles/<key>.json's min_reviews into the search config
        # (the search-time floor) while icp.hard_icp_gate reads
        # VerticalProfile.min_reviews (the score-time floor). epoxy and roofing
        # shipped 10 in the JSON and 8 here, so a lead with 8 reviews was
        # searched for and then skipped. Any global change to the floor has to
        # move both surfaces together.
        for path in sorted((REPO_ROOT / "vertical_profiles").glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            profile = VERTICAL_PROFILES.get(_normalize_key(str(data.get("vertical") or "")))
            if profile is None:
                continue
            with self.subTest(profile=path.name):
                self.assertEqual(data["min_reviews"], profile.min_reviews)


class RegistryGrowthReclassifiesLeadsTest(unittest.TestCase):
    """Registering epoxy and roofing MOVED leads between verticals. On purpose.

    `infer_vertical` substring-matches every registry member's `batch_terms` and
    takes the answer when exactly one member matches. Two new members are two new
    ways for that list to become length-1, so a lead that previously fell through
    to its batch's vertical can now be relabelled. The declared-vertical guard
    does not cover this: `places_search.Lead` has no vertical field, so the
    `run_pipeline` path reaches here with `declared` empty.

    These tests exist because that shift was silent. It is a real change to what
    lands in Airtable's Vertical column and it should fail loudly if it moves
    again, in either direction.
    """

    def test_a_roofer_found_by_a_hardscaping_batch_is_now_reported_as_roofing(self) -> None:
        # Previously "Hardscaping": no roofing profile existed, so no batch term
        # matched and the lead kept its batch's vertical.
        record = _record(name="Northcoast Roofing And Exteriors", source_query="hardscaping Cleveland OH")

        self.assertEqual(infer_vertical(record, "hardscaping"), "Roofing")

    def test_a_floor_coater_found_by_a_hardscaping_batch_is_now_reported_as_epoxy(self) -> None:
        record = _record(name="Lakeside Garage Floor Coating", source_query="hardscaping Cleveland OH")

        self.assertEqual(infer_vertical(record, "hardscaping"), "Epoxy Flooring")

    def test_scraped_copy_naming_the_trade_reclassifies_too(self) -> None:
        # The sharpest edge of the change: site copy, not just the business name.
        # It is safe because term matching requires a left word boundary (see
        # `TermBoundaryMatchingTest`) and because incidental copy like "roof
        # rails" matches nothing — see
        # `test_incidental_roof_copy_no_longer_relabels_the_lead`.
        record = _record(name="Northcoast Exteriors", text="we handle roof replacement and siding")

        self.assertEqual(infer_vertical(record, "hardscaping"), "Roofing")

    def test_a_declared_vertical_still_outranks_the_two_new_members(self) -> None:
        # The batch CSV path declares a vertical, so it is unaffected. Only the
        # run_pipeline path, whose Lead carries no vertical, can be reclassified.
        record = _record(
            name="Northcoast Roofing And Exteriors",
            vertical="hardscaping",
            source_query="hardscaping Cleveland OH",
        )

        self.assertEqual(infer_vertical(record, "hardscaping"), "Hardscaping")

    def test_copy_matching_two_verticals_still_keeps_the_batchs_own(self) -> None:
        # "concrete coating" is an epoxy batch term and "patio" a hardscaping
        # one. Two matches is not length-1, so the tie-break holds and the new
        # members cannot take a batch that also names its own trade.
        record = _record(
            name="Premier Patios And Concrete Coatings",
            source_query="patio contractor Cleveland OH",
        )

        self.assertEqual(infer_vertical(record, "hardscaping"), "Hardscaping")

    def test_the_reclassified_labels_are_writable_Vertical_choices(self) -> None:
        # The reclassification only stays harmless while every emittable label
        # maps back to a slug the live Airtable single-select already offers.
        from lead_machine.airtable_sync import resolve_vertical_label

        for key, profile in VERTICAL_PROFILES.items():
            with self.subTest(vertical=key):
                self.assertEqual(resolve_vertical_label(profile.label), key)


class TermBoundaryMatchingTest(unittest.TestCase):
    """A vertical term must not match when it is only the TAIL of a longer word.

    Registering `roofing` made this live: "roofing" is a suffix of
    "waterproofing", so under plain substring matching every basement and
    foundation waterproofing business resolved to roofing — scored against
    roofing's deny/franchise lists and written to Airtable as `roofing`.
    """

    # Real Cleveland-market waterproofing phrasing. None of these is roofing.
    WATERPROOFING = (
        "basement waterproofing cleveland",
        "foundation waterproofing",
        "crawl space waterproofing",
        "interior waterproofing systems",
        "exterior waterproofing contractor",
    )

    def test_waterproofing_is_not_roofing(self) -> None:
        for query in self.WATERPROOFING:
            with self.subTest(query=query):
                self.assertNotEqual(_infer_profile_key(query), "roofing")

    def test_a_waterproofing_business_name_is_not_relabelled_roofing(self) -> None:
        for name in ("Ohio Waterproofing", "Everdry Waterproofing", "B-Dry Waterproofing"):
            with self.subTest(name=name):
                self.assertNotEqual(infer_vertical(_record(name=name), ""), "Roofing")

    def test_genuine_roofing_still_resolves(self) -> None:
        for query in (
            "roofing contractor cleveland",
            "roof repair akron",
            "metal roofing",
            "shingle roof replacement",
            "commercial roofing company",
        ):
            with self.subTest(query=query):
                self.assertEqual(_infer_profile_key(query), "roofing")

    def test_suffix_growth_still_matches(self) -> None:
        """Left-boundary-only is deliberate: these lists are written singular
        and rely on the plural still matching."""
        for text, term in (
            ("we install gutters", "gutter"),
            ("brick pavers and walls", "paver"),
            ("roofings", "roofing"),
        ):
            with self.subTest(text=text, term=term):
                self.assertTrue(matches_term(text, term))

    def test_no_batch_term_is_the_tail_of_another_verticals_term(self) -> None:
        """The collision audit. `roofing` inside `waterproofing` was found by
        hand; this fails if a future term reintroduces the same class."""
        collisions = []
        for key, profile in VERTICAL_PROFILES.items():
            for term in profile.batch_terms:
                for other_key, other in VERTICAL_PROFILES.items():
                    if other_key == key:
                        continue
                    for other_term in other.batch_terms:
                        if term != other_term and other_term.endswith(term) and other_term != term:
                            collisions.append((key, term, other_key, other_term))
        self.assertEqual(collisions, [])

    def test_known_adjacent_trades_do_not_capture_a_registered_vertical(self) -> None:
        """Trades we do not prospect but which share word tails with ones we do.
        Each must resolve to something other than the vertical it shadows."""
        for phrase, must_not_be in (
            ("basement waterproofing", "roofing"),
            ("deck waterproofing", "roofing"),
            ("sunroofing installation", "roofing"),
            ("auto sunroof replacement", "roofing"),
        ):
            with self.subTest(phrase=phrase):
                self.assertNotEqual(_infer_profile_key(phrase), must_not_be)


if __name__ == "__main__":
    unittest.main()
