"""A semicolon inside a claim must not steal another claim's citation.

`flatten_safe_outreach_claims` joins claims with `"; "` and
`_parse_flattened_safe_claims` splits them back apart. With no escaping, a claim
that itself contains a semicolon splits in two on the way back: the leading
fragment loses its citation, and — the dangerous half — the trailing fragment
inherits `[ev_1]`, which was evidence for a *different* assertion. That is
affirmative misattribution: the claim a rep reads looks backed, and is not.

The gate itself was never wrong — `apply_sendability` evaluates intact claim
objects and only then flattens — so the corruption lives in the artifact a rep
reads, not in the verdict. These tests pin the artifact: escape on write, split
on the unescaped delimiter on read, and leave legacy rows (raw `;`, no escapes)
parsing exactly as they always have.
"""
from __future__ import annotations

import unittest
from typing import Any, Dict, List

from lead_machine.sendability import (
    flatten_safe_outreach_claims,
    normalize_safe_outreach_claims,
)


class SafeClaimRoundTripTests(unittest.TestCase):
    def _round_trip(self, claims: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return normalize_safe_outreach_claims(flatten_safe_outreach_claims(claims))

    def test_audit_repro_semicolon_claim_stays_one_claim(self) -> None:
        """The exact row from the 2026-08 audit: one claim, one citation."""
        claims = [{"claim": "Your site loads in 8s; competitors load in 2s", "evidence_refs": ["ev_1"]}]

        parsed = self._round_trip(claims)

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["claim"], "Your site loads in 8s; competitors load in 2s")
        self.assertEqual(parsed[0]["evidence_refs"], ["ev_1"])

    def test_semicolon_claim_does_not_hand_its_citation_to_a_neighbour(self) -> None:
        """The misattribution half: no fragment may inherit another claim's ref."""
        claims = [
            {"claim": "Your site loads in 8s; competitors load in 2s", "evidence_refs": ["ev_1"]},
            {"claim": "Your quote form is broken on mobile.", "evidence_refs": ["ev_2"]},
        ]

        parsed = self._round_trip(claims)

        self.assertEqual(parsed, claims)
        self.assertNotIn("competitors load in 2s", [claim["claim"] for claim in parsed[1:]])

    def test_round_trip_survives_delimiters_escapes_brackets_and_unicode(self) -> None:
        cases: List[List[Dict[str, Any]]] = [
            # Plain claims — the common case must not regress.
            [
                {"claim": "Your quote path is hard to find on mobile.", "evidence_refs": ["E3", "E7"]},
                {"claim": "Your best photos are buried.", "evidence_refs": ["E9"]},
            ],
            # Semicolons, in one claim and in several.
            [
                {"claim": "Slow; unresponsive; dated", "evidence_refs": ["ev_1"]},
                {"claim": "No pricing; no CTA", "evidence_refs": ["ev_2", "ev_3"]},
            ],
            # Backslashes, including a trailing one adjacent to the delimiter.
            [
                {"claim": r"Photos live at C:\gallery\\", "evidence_refs": ["ev_1"]},
                {"claim": r"A literal escape \; is not a delimiter", "evidence_refs": ["ev_2"]},
            ],
            # Brackets, which the `[refs]` regex also uses.
            [
                {"claim": "[Urgent] your form 404s", "evidence_refs": ["ev_1"]},
                {"claim": "Reviews say [sic] slow response", "evidence_refs": ["ev_2"]},
                {"claim": "Unbalanced ] bracket", "evidence_refs": ["ev_3"]},
            ],
            # Unicode, including a full-width semicolon that must stay untouched.
            [
                {"claim": "Página sin teléfono — y sin formulario；de verdad", "evidence_refs": ["ev_1"]},
                {"claim": "レビュー対応なし", "evidence_refs": ["ev_2"]},
            ],
            # Empty refs, single ref, many refs.
            [
                {"claim": "No citation; still one claim", "evidence_refs": []},
                {"claim": "One ref", "evidence_refs": ["ev_1"]},
                {"claim": "Many refs; still many", "evidence_refs": ["ev_1", "ev_2", "ev_3"]},
            ],
            # Everything at once.
            [
                {"claim": r"Load 8s; [sic] path C:\x — 遅い", "evidence_refs": ["ev_1", "ev_2"]},
                {"claim": "Second claim keeps its own ref", "evidence_refs": ["ev_9"]},
            ],
        ]

        for claims in cases:
            with self.subTest(claims=claims):
                self.assertEqual(self._round_trip(claims), claims)

    def test_round_trip_holds_through_the_airtable_field_name(self) -> None:
        """Rows come back off the CSV/Airtable keyed by the display field name."""
        claims = [{"claim": "8s load; 2s for rivals", "evidence_refs": ["ev_1"]}]

        flattened = flatten_safe_outreach_claims(claims)

        self.assertEqual(normalize_safe_outreach_claims({"Safe Outreach Claims": flattened}), claims)

    def test_flat_string_stays_human_readable(self) -> None:
        """Reps read this field in Airtable, so the format stays flat, not JSON."""
        flattened = flatten_safe_outreach_claims(
            [
                {"claim": "Your site loads in 8s; competitors load in 2s", "evidence_refs": ["ev_1"]},
                {"claim": "Your best photos are buried.", "evidence_refs": ["ev_2", "ev_3"]},
            ]
        )

        self.assertEqual(
            flattened,
            r"Your site loads in 8s\; competitors load in 2s [ev_1]; Your best photos are buried. [ev_2,ev_3]",
        )

    def test_claims_without_special_characters_flatten_exactly_as_before(self) -> None:
        self.assertEqual(
            flatten_safe_outreach_claims(
                [
                    {"claim": "Your quote path is hard to find on mobile.", "evidence_refs": ["E3", "E7"]},
                    {"claim": "Your best photos are buried.", "evidence_refs": "E9"},
                ]
            ),
            "Your quote path is hard to find on mobile. [E3,E7]; Your best photos are buried. [E9]",
        )


class LegacySafeClaimParsingTests(unittest.TestCase):
    """Rows written before escaping shipped must parse exactly as they did."""

    def test_legacy_row_with_raw_semicolon_still_splits_as_it_did(self) -> None:
        legacy = "Your site loads in 8s; competitors load in 2s [ev_1]"

        self.assertEqual(
            normalize_safe_outreach_claims(legacy),
            [
                {"claim": "Your site loads in 8s", "evidence_refs": []},
                {"claim": "competitors load in 2s", "evidence_refs": ["ev_1"]},
            ],
        )

    def test_legacy_multi_claim_row_parses_unchanged(self) -> None:
        legacy = "Your quote path is hard to find on mobile. [E3,E7]; Your best photos are buried. [E9]"

        self.assertEqual(
            normalize_safe_outreach_claims(legacy),
            [
                {"claim": "Your quote path is hard to find on mobile.", "evidence_refs": ["E3", "E7"]},
                {"claim": "Your best photos are buried.", "evidence_refs": ["E9"]},
            ],
        )

    def test_legacy_row_without_refs_or_with_blank_segments_parses_unchanged(self) -> None:
        self.assertEqual(
            normalize_safe_outreach_claims("A bare claim with no bracket; ; Another one []"),
            [
                {"claim": "A bare claim with no bracket", "evidence_refs": []},
                {"claim": "Another one", "evidence_refs": []},
            ],
        )

    def test_legacy_backslash_that_is_not_an_escape_survives(self) -> None:
        self.assertEqual(
            normalize_safe_outreach_claims(r"Photos live at C:\gallery [ev_1]"),
            [{"claim": r"Photos live at C:\gallery", "evidence_refs": ["ev_1"]}],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
