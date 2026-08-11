"""The capsule parser must not resurrect the bug the tri-state fixed.

`money_signal_from_summary` recovers the money signal from the
`Buyer Intent Signals` text, because the base has no `Money Signal` column and
the dial queue would otherwise rank every lead as `unknown`.

The hazard is that the capsule's *meaning* is version-dependent while the text
is not. Before 14687fb ("stop reporting unobserved buyer intent as a negative")
the summary was written from a boolean:

    money_signal={'yes' if _buyer_intent_has_money_signal(...) else 'no'}

so a legacy `no` means "no paid CRM detected **or** there was no crawlable site
text to look at" — exactly the conflation `buyer_signals` records as fixed.
141 such capsules exist in `output/*.csv`, 32 of them in rows already uploaded
to the base as sendable.

The discriminator is `demand_signal=`, which landed in the same commit as the
tri-state: a capsule carrying it is post-fix and its `no` is a real `no`.
"""

from __future__ import annotations

import unittest

from lead_machine.buyer_signals import money_signal_from_summary

LEGACY_NO = "money_signal=no; tools=none; review_demand_band=ideal_growth; ads=unknown"
MODERN_NO = (
    "money_signal=no; demand_signal=yes; tools=none; "
    "review_demand_band=ideal_growth; ads=unknown"
)


def capsule(text: str) -> dict:
    return {"Buyer Intent Signals": text}


class ALegacyCapsuleCannotAssertANegative(unittest.TestCase):
    def test_a_legacy_no_reads_as_unknown(self) -> None:
        # The whole point of the tri-state: absence of observation is not
        # evidence the business does not spend.
        self.assertEqual(money_signal_from_summary(capsule(LEGACY_NO)), "unknown")

    def test_a_modern_no_still_reads_as_no(self) -> None:
        # The fix must not blunt the real signal it is protecting.
        self.assertEqual(money_signal_from_summary(capsule(MODERN_NO)), "no")

    def test_a_legacy_yes_is_still_a_yes(self) -> None:
        # `yes` meant the same thing in both formats, so it needs no rescue.
        self.assertEqual(
            money_signal_from_summary(capsule("money_signal=yes; tools=hubspot")), "yes"
        )


class TheParserReadsOnlyItsOwnKey(unittest.TestCase):
    def test_a_missing_capsule_is_unknown(self) -> None:
        self.assertEqual(money_signal_from_summary({}), "unknown")

    def test_a_missing_key_is_unknown(self) -> None:
        self.assertEqual(
            money_signal_from_summary(capsule("demand_signal=yes; tools=none")), "unknown"
        )

    def test_a_nested_equals_in_another_value_cannot_be_read_as_the_money_key(self) -> None:
        # `ads=google_ad_transparency=yes, meta_ad_library=no` carries `=` inside
        # its value; an inexact key match would read the wrong token.
        text = (
            "money_signal=yes; demand_signal=yes; tools=none; "
            "ads=google_ad_transparency=yes, meta_ad_library=no"
        )
        self.assertEqual(money_signal_from_summary(capsule(text)), "yes")

    def test_an_unparseable_value_is_unknown_not_no(self) -> None:
        self.assertEqual(
            money_signal_from_summary(capsule("money_signal=; demand_signal=yes")), "unknown"
        )
        self.assertEqual(
            money_signal_from_summary(capsule("money_signal=maybe; demand_signal=yes")),
            "unknown",
        )

    def test_the_snake_case_column_is_read_too(self) -> None:
        self.assertEqual(
            money_signal_from_summary(
                {"buyer_intent_signals": "money_signal=yes; demand_signal=yes"}
            ),
            "yes",
        )


class TheRankHonoursTheRescue(unittest.TestCase):
    def test_a_legacy_no_scores_as_unknown_not_zero(self) -> None:
        # The rank is where the penalty would actually land: 0.0 of 15 rather
        # than the 7.5 an unanswered question earns.
        from lead_machine.dial_rank import rank_components

        legacy = rank_components({"Buyer Intent Signals": LEGACY_NO})["money"]
        modern = rank_components({"Buyer Intent Signals": MODERN_NO})["money"]
        self.assertGreater(legacy, modern)
        self.assertEqual(modern, 0.0)


if __name__ == "__main__":
    unittest.main()
