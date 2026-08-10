from __future__ import annotations

import unittest
from unittest.mock import patch


class BuyerSignalsTest(unittest.TestCase):
    def _record(self, *, text: str = "", rating: str = "4.9", reviews: str = "130") -> dict:
        return {
            "google": {
                "business_name": "Mirror Finish Auto Spa",
                "rating": rating,
                "reviews": reviews,
                "website_url": "https://example.com",
            },
            "website": {
                "final_url": "https://example.com",
                "combined_text": text,
                "homepage": {"title": "", "meta": "", "text": text},
                "pages": {},
                "social_links": ["https://facebook.com/mirrorfinish"],
            },
        }

    def test_servicetitan_text_sets_paid_crm_money_signal(self) -> None:
        from lead_machine.buyer_signals import build_buyer_intent

        record = self._record(text="Schedule service through ServiceTitan.")

        buyer_intent = build_buyer_intent(record, ads_check=False)

        self.assertIs(record["buyer_intent"], buyer_intent)
        self.assertIn("ServiceTitan", buyer_intent["crm_booking_stack"]["detected_tools"])
        self.assertTrue(buyer_intent["crm_booking_stack"]["has_paid_crm"])
        self.assertTrue(buyer_intent["money_signal"])

    def test_detailing_crm_tools_set_paid_crm_money_signal(self) -> None:
        from lead_machine.buyer_signals import build_buyer_intent

        for tool in ("Urable", "Mobile Tech RX"):
            with self.subTest(tool=tool):
                buyer_intent = build_buyer_intent(self._record(text=f"Book through {tool}."), ads_check=False)

                self.assertIn(tool, buyer_intent["crm_booking_stack"]["detected_tools"])
                self.assertTrue(buyer_intent["crm_booking_stack"]["has_paid_crm"])
                self.assertTrue(buyer_intent["money_signal"])

    def test_marketing_copy_saying_durable_does_not_detect_urable(self) -> None:
        """`urable` is a suffix of `durable` — the `roofing` ⊂ `waterproofing`
        bug, reintroduced in the CRM detector. Bare substring matching read
        "durable, high-performance coatings" as a paid booking stack on 263 of
        625 recovery-corpus leads (42%), and seven of them shipped
        "Urable was detected as a paid CRM/booking tool" as an outreach claim
        about a business that runs no such thing.
        """
        from lead_machine.buyer_signals import build_buyer_intent

        record = self._record(text="Durable, high-performance concrete coatings built to last.")

        buyer_intent = build_buyer_intent(record, ads_check=False)

        self.assertNotIn("Urable", buyer_intent["crm_booking_stack"]["detected_tools"])
        self.assertFalse(buyer_intent["crm_booking_stack"]["has_paid_crm"])

    def test_third_party_tool_terms_have_no_left_boundary_collisions(self) -> None:
        """The collision audit, mirroring the vertical-term one. A term that is
        the tail of another term's word is a false positive waiting to ship."""
        from lead_machine.evidence import THIRD_PARTY_TOOLS
        from lead_machine.verticals import matches_term

        collisions = []
        for label, terms in THIRD_PARTY_TOOLS.items():
            for term in terms:
                for other_label, other_terms in THIRD_PARTY_TOOLS.items():
                    if other_label == label:
                        continue
                    for other in other_terms:
                        if term != other and term in other and not matches_term(other, term):
                            collisions.append((label, term, other_label, other))
        self.assertEqual(collisions, [])

    def test_ads_check_false_sets_unknown_ads_without_network_calls(self) -> None:
        from lead_machine.buyer_signals import build_buyer_intent

        with patch("requests.get") as get:
            buyer_intent = build_buyer_intent(self._record(), ads_check=False)

        get.assert_not_called()
        self.assertEqual(
            buyer_intent["ads"],
            {
                "google_ad_transparency": "unknown",
                "meta_ad_library": "unknown",
                "lsa_badge": "unknown",
                "errors": [],
            },
        )

    def test_ads_check_true_makes_no_network_call_and_says_which_checks_are_missing(self) -> None:
        # The two remote checks used to fetch a page, throw the response away and
        # return "unknown" on every branch. An unqualified "unknown" is
        # indistinguishable from a check that ran and found no ads, so the
        # unimplemented state has to be stated in the record.
        from lead_machine.buyer_signals import build_buyer_intent

        with patch("requests.get") as get:
            buyer_intent = build_buyer_intent(self._record(), ads_check=True)

        get.assert_not_called()
        self.assertEqual(buyer_intent["ads"]["google_ad_transparency"], "unknown")
        self.assertEqual(buyer_intent["ads"]["meta_ad_library"], "unknown")
        self.assertEqual(
            sorted(buyer_intent["ads"]["errors"]),
            ["google_ad_transparency: unimplemented", "meta_ad_library: unimplemented"],
        )

    def test_lifetime_count_does_not_invent_review_velocity(self) -> None:
        from lead_machine.buyer_signals import build_buyer_intent

        buyer_intent = build_buyer_intent(self._record(rating="3.9", reviews="2"), ads_check=False)

        self.assertEqual(buyer_intent["review_velocity"]["velocity_band"], "unknown")
        self.assertEqual(buyer_intent["review_velocity"]["source"], "insufficient_history")
        self.assertNotEqual(buyer_intent["money_signal"], "yes")

    def test_strong_lifetime_count_still_has_unknown_velocity(self) -> None:
        from lead_machine.buyer_signals import build_buyer_intent

        buyer_intent = build_buyer_intent(self._record(rating="4.8", reviews="120"), ads_check=False)

        self.assertEqual(buyer_intent["review_velocity"]["velocity_band"], "unknown")
        self.assertEqual(buyer_intent["review_demand_signal"]["demand_band"], "established")
        self.assertNotEqual(buyer_intent["money_signal"], "yes")

    def test_review_count_alone_does_not_create_money_signal(self) -> None:
        from lead_machine.buyer_signals import build_buyer_intent

        buyer_intent = build_buyer_intent(
            self._record(rating="5.0", reviews="100", text="We wash cars."), ads_check=False
        )

        self.assertEqual(buyer_intent["review_profile"]["source"], "count_only")
        self.assertEqual(buyer_intent["review_profile"]["demand_band"], "established")
        self.assertEqual(buyer_intent["money_signal"], "no")

    def test_readable_site_without_a_paid_stack_is_a_no_not_an_unknown(self) -> None:
        from lead_machine.buyer_signals import build_buyer_intent, has_money_signal

        record = self._record(text="Call us for a free estimate on ceramic coating.")

        buyer_intent = build_buyer_intent(record, ads_check=False)

        self.assertEqual(buyer_intent["money_signal"], "no")
        self.assertFalse(has_money_signal(record))

    def test_unreadable_site_makes_money_signal_unknown_not_no(self) -> None:
        # The whole point of the tri-state: paid-CRM detection reads scraped site
        # text, so a business with nothing to scrape has an unanswered question,
        # not a negative answer. Reporting "no" told the rubric this business does
        # not buy leads, which is a claim the pipeline never had evidence for.
        from lead_machine.buyer_signals import build_buyer_intent, has_money_signal

        record = self._record(text="")
        record["website"] = {"site_status": "No Website"}

        buyer_intent = build_buyer_intent(record, ads_check=False)

        self.assertEqual(buyer_intent["money_signal"], "unknown")
        self.assertFalse(has_money_signal(record))
        self.assertTrue(
            any("not observable" in str(item) for item in buyer_intent["evidence"]),
            buyer_intent["evidence"],
        )

    def test_unknown_money_signal_is_never_truthy_for_a_gate(self) -> None:
        # required_visible_pain_score drops the mature threshold from 40 to 35
        # when money_signal is truthy. An "unknown" reaching that as True would
        # loosen the gate on the leads with the least evidence behind them.
        from lead_machine.buyer_signals import has_money_signal
        from lead_machine.sendability import required_visible_pain_score

        record = {"Money Signal": "Unknown"}

        self.assertFalse(has_money_signal(record))
        self.assertEqual(
            required_visible_pain_score(300, review_band="mature", money_signal=has_money_signal(record)),
            40,
        )

    def test_legacy_boolean_money_signal_still_reads_correctly(self) -> None:
        from lead_machine.buyer_signals import money_signal_state

        self.assertEqual(money_signal_state({"money_signal": True}), "yes")
        self.assertEqual(money_signal_state({"Money Signal": "No"}), "no")
        self.assertEqual(money_signal_state({}), "unknown")

    def test_demand_signal_is_reported_separately_from_spend(self) -> None:
        # Demand and willingness to pay are different claims. Only money_signal
        # feeds required_visible_pain_score, so folding review pull into it would
        # quietly relax that gate for any busy business.
        from lead_machine.buyer_signals import build_buyer_intent

        record = self._record(rating="4.9", reviews="130")
        record["website"] = {"site_status": "No Website"}

        buyer_intent = build_buyer_intent(record, ads_check=False)

        self.assertEqual(buyer_intent["money_signal"], "unknown")
        self.assertEqual(buyer_intent["demand_signal"]["state"], "yes")
        self.assertEqual(buyer_intent["demand_signal"]["source"], "review_count")

    def test_demand_signal_prefers_measured_velocity_over_lifetime_count(self) -> None:
        from lead_machine.buyer_signals import build_buyer_intent

        record = self._record(rating="4.9", reviews="130")
        record["review_count_history"] = [
            {"observed_at": "2026-05-01T00:00:00Z", "review_count": 100},
            {"observed_at": "2026-06-01T00:00:00Z", "review_count": 130},
        ]

        buyer_intent = build_buyer_intent(record, ads_check=False)

        self.assertEqual(buyer_intent["review_velocity"]["source"], "snapshot_delta")
        self.assertEqual(buyer_intent["review_velocity"]["reviews_added"], 30)

    def test_thin_review_count_reports_no_demand_rather_than_unknown(self) -> None:
        # Review counts are always observable from the Google profile, so a thin
        # count is a real negative — unlike money_signal, there is no "could not
        # look" case to protect here.
        from lead_machine.buyer_signals import build_buyer_intent

        buyer_intent = build_buyer_intent(self._record(rating="4.2", reviews="1"), ads_check=False)

        self.assertEqual(buyer_intent["demand_signal"]["state"], "no")
        self.assertEqual(buyer_intent["demand_signal"]["demand_band"], "thin")

    def test_old_review_only_money_signal_is_rebuilt(self) -> None:
        from lead_machine.buyer_signals import BUYER_INTENT_LOGIC_VERSION, ensure_buyer_intent

        record = self._record(rating="4.8", reviews="120")
        old_buyer_intent = {
            "crm_booking_stack": {
                "detected_tools": [],
                "has_paid_crm": False,
                "evidence": [],
            },
            "review_velocity": {
                "velocity_band": "live",
                "source": "count_only",
                "evidence": ["120 Google reviews with 4.8 rating"],
            },
            "ads": {
                "google_ad_transparency": "unknown",
                "meta_ad_library": "unknown",
                "lsa_badge": "unknown",
                "errors": [],
            },
            "money_signal": True,
            "evidence": ["120 Google reviews with 4.8 rating"],
        }
        record["buyer_intent"] = old_buyer_intent

        buyer_intent = ensure_buyer_intent(record, ads_check=False)

        self.assertIsNot(buyer_intent, old_buyer_intent)
        self.assertEqual(buyer_intent["logic_version"], BUYER_INTENT_LOGIC_VERSION)
        self.assertNotEqual(buyer_intent["money_signal"], "yes")
        self.assertEqual(buyer_intent["review_velocity"]["velocity_band"], "unknown")
        self.assertEqual(buyer_intent["review_demand_signal"]["demand_band"], "established")

    def test_known_ad_yes_creates_money_signal(self) -> None:
        from lead_machine.buyer_signals import build_buyer_intent

        buyer_intent = build_buyer_intent(
            self._record(text="Google Guaranteed local services ads"),
            ads_check=True,
        )

        self.assertEqual(buyer_intent["ads"]["lsa_badge"], "yes")
        self.assertTrue(buyer_intent["money_signal"])

    def test_detailing_review_demand_bands_are_vertical_specific(self) -> None:
        from lead_machine.buyer_signals import build_buyer_intent

        early = build_buyer_intent(
            self._record(rating="4.7", reviews="20") | {"vertical": "detailing"},
            ads_check=False,
        )
        mature = build_buyer_intent(
            self._record(rating="4.9", reviews="401") | {"vertical": "detailing"},
            ads_check=False,
        )

        self.assertEqual(early["review_demand_signal"]["demand_band"], "ideal_growth")
        self.assertEqual(mature["review_demand_signal"]["demand_band"], "mature")


if __name__ == "__main__":
    unittest.main()
