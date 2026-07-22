from __future__ import annotations

import unittest

from lead_machine.score_schema import validate_current_scored_row


class CurrentScoredRowContractTest(unittest.TestCase):
    def test_canonical_not_an_lpm_fit_state_is_case_insensitive(self) -> None:
        row = {
            "Score Schema Version": "2",
            "Commercial Fit State": "not-an-LPM-fit",
            "Commercial Materiality": "",
            "Recurring Consequence": "",
            "Root Cause Confidence": "",
            "LPM Offer Component": "",
            "Packageable Solution": "",
            "Value Mechanism": "",
            "Beyond Free Fix": "",
            "Self Fix Risk": "",
            "Intentionality": "",
            "Contact Eligibility": "hold-commercial-case",
            "Asset State": "none",
            "Promise State": "asset-free",
            "Asset ID": "",
            "Asset Verifier Verdict": "",
        }

        self.assertEqual(validate_current_scored_row(row), [])


if __name__ == "__main__":
    unittest.main()
