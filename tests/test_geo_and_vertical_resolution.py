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

import unittest

from lead_machine.scoring_profiles import market_service_area_terms
from lead_machine.verticals import VERTICAL_PROFILES, resolve_profile
from score_leads import (
    GENERIC_SERVICE_AREA_TERMS,
    infer_vertical,
    is_corporate_location_name,
)


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


if __name__ == "__main__":
    unittest.main()
