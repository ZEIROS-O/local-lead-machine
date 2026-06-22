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

    def test_ads_check_false_sets_unknown_ads_without_network_calls(self) -> None:
        from lead_machine.buyer_signals import build_buyer_intent

        with patch("lead_machine.buyer_signals.requests.get") as get:
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

    def test_low_review_count_and_rating_is_thin(self) -> None:
        from lead_machine.buyer_signals import build_buyer_intent

        buyer_intent = build_buyer_intent(self._record(rating="3.9", reviews="2"), ads_check=False)

        self.assertEqual(buyer_intent["review_velocity"]["velocity_band"], "thin")
        self.assertEqual(buyer_intent["review_velocity"]["source"], "count_only")
        self.assertFalse(buyer_intent["money_signal"])

    def test_strong_count_and_rating_is_live_or_steady(self) -> None:
        from lead_machine.buyer_signals import build_buyer_intent

        buyer_intent = build_buyer_intent(self._record(rating="4.8", reviews="120"), ads_check=False)

        self.assertIn(buyer_intent["review_velocity"]["velocity_band"], {"live", "steady"})
        self.assertEqual(buyer_intent["review_demand_signal"]["demand_band"], "established")
        self.assertFalse(buyer_intent["money_signal"])

    def test_review_count_alone_does_not_create_money_signal(self) -> None:
        from lead_machine.buyer_signals import build_buyer_intent

        buyer_intent = build_buyer_intent(self._record(rating="5.0", reviews="100"), ads_check=False)

        self.assertEqual(buyer_intent["review_profile"]["source"], "count_only")
        self.assertEqual(buyer_intent["review_profile"]["demand_band"], "established")
        self.assertFalse(buyer_intent["money_signal"])

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
        self.assertFalse(buyer_intent["money_signal"])
        self.assertEqual(buyer_intent["review_velocity"]["velocity_band"], "live")
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
