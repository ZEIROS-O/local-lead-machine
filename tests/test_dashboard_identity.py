from __future__ import annotations

import unittest

from lead_machine.dashboard.identity import reconcile_rows


class DashboardIdentityTest(unittest.TestCase):
    def test_place_key_wins_and_partial_scoring_is_reconciled(self) -> None:
        enriched = [
            {"google": {"place_key": "p1", "business_name": "Alpha", "city": "Columbus"}},
            {"google": {"place_key": "p2", "business_name": "Beta", "city": "Columbus"}},
        ]

        result = reconcile_rows(
            enriched,
            [{"Place Key": "p2", "Business Name": "Wrong Name"}],
        )

        self.assertEqual(result.status, "reconciled")
        self.assertEqual(result.matched, 1)
        self.assertEqual(result.unmatched_enriched, 1)
        self.assertEqual(result.unmatched_scored, 0)
        self.assertEqual(result.scored_by_enriched_index, {1: 0})

    def test_unique_phone_domain_and_name_city_are_ordered_fallbacks(self) -> None:
        enriched = [
            {"google": {"phone": "(614) 555-0100", "business_name": "Phone Co", "city": "Dublin"}},
            {
                "google": {
                    "website_url": "https://domain.example/path",
                    "business_name": "Domain Co",
                    "city": "Hilliard",
                }
            },
            {"google": {"business_name": "Name Co", "city": "Westerville"}},
        ]
        scored = [
            {"Phone": "+1 614-555-0100"},
            {"Website URL": "https://www.domain.example/other"},
            {"Business Name": " Name Co ", "City": "WESTERVILLE"},
        ]

        result = reconcile_rows(enriched, scored)

        self.assertEqual(result.status, "reconciled")
        self.assertEqual(result.matched, 3)
        self.assertEqual(result.scored_by_enriched_index, {0: 0, 1: 1, 2: 2})

    def test_duplicate_fallback_is_unknown_not_zero(self) -> None:
        enriched = [
            {"google": {"business_name": "Same Co", "city": "Columbus"}},
            {"google": {"business_name": "Same Co", "city": "Columbus"}},
        ]

        result = reconcile_rows(
            enriched,
            [{"Business Name": "Same Co", "City": "Columbus"}],
        )

        self.assertEqual(result.status, "unknown")
        self.assertIsNone(result.as_dict()["unmatched_enriched"])
        self.assertEqual(result.ambiguous, 3)


if __name__ == "__main__":
    unittest.main()
