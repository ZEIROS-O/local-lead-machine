"""Every base-reading eval must name the column the base actually has.

`completeness` and `reproducibility` each carried their own copy of this
assertion and were migrated together in dc3bbae. `accept_rate` did not have one,
so it kept pointing at `Packet Tier` after step 4 of the verdict cutover deleted
that column — 422ing on every run while the full suite stayed green, because the
only test of its read path pinned the deleted name in a fixture.

So the assertion is discovered here rather than written per-eval: a fifth
base-reading eval is covered the moment it declares `DEFAULT_VERDICT_FIELD`,
without anyone remembering to add a test.
"""

from __future__ import annotations

import importlib
import unittest
from pathlib import Path

from lead_machine import verdict as verdict_module


REPO_ROOT = Path(__file__).resolve().parents[1]
EVALS_DIR = REPO_ROOT / "evals"
# The three that read the base today. A floor, not a list: discovery below is
# what actually drives the test, and this only stops a broken glob from passing
# by finding nothing at all.
MINIMUM_DECLARING_MODULES = 3


def _eval_modules() -> list[tuple[str, object]]:
    modules: list[tuple[str, object]] = []
    for path in sorted(EVALS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        modules.append((path.stem, importlib.import_module(f"evals.{path.stem}")))
    return modules


class EvalVerdictFieldContractTests(unittest.TestCase):
    def test_every_eval_declaring_a_verdict_field_names_the_live_column(self) -> None:
        declaring = [
            (name, module)
            for name, module in _eval_modules()
            if hasattr(module, "DEFAULT_VERDICT_FIELD")
        ]

        self.assertGreaterEqual(
            len(declaring),
            MINIMUM_DECLARING_MODULES,
            "discovery found fewer base-reading evals than exist; the glob is broken",
        )
        for name, module in declaring:
            with self.subTest(eval=name):
                self.assertEqual(
                    module.DEFAULT_VERDICT_FIELD,
                    verdict_module.VERDICT_FIELD,
                    f"evals/{name}.py reads a column the base does not have",
                )

    def test_a_proposed_verdict_is_a_member_of_the_live_vocabulary(self) -> None:
        """A correct field name with a stale value is the same 422, one layer in.

        `accept_rate` filtered on the packet tier `Sendable Packet`, which no row
        can carry now — repointing the field alone would have matched zero rows
        and reported an empty KPI as a real one.
        """
        for name, module in _eval_modules():
            proposed = getattr(module, "DEFAULT_PROPOSED_VERDICT", None)
            if proposed is None:
                continue
            with self.subTest(eval=name):
                self.assertIn(proposed, verdict_module.VERDICT_CHOICES)


if __name__ == "__main__":
    unittest.main()
