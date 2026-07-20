from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from audit_dashboard_batches import audit_repository
from tests.test_dashboard import make_root


class DashboardAuditTest(unittest.TestCase):
    def test_clean_repository_has_no_unexplained_discrepancies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_root(root)

            report = audit_repository(root)

        self.assertTrue(report["ok"])
        self.assertEqual(report["discrepancies"], [])
        self.assertEqual(report["batch_count"], 1)
        self.assertEqual(
            report["batch_count"],
            report["lifecycle_counts"]["active"]
            + report["lifecycle_counts"]["legacy"],
        )


if __name__ == "__main__":
    unittest.main()
