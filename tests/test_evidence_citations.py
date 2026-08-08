"""The scorer must never mint an evidence citation the model declined to make.

`EVIDENCE_COUNT_BLOCKER` exists to stop a lead that carries fewer than two
evidence references from being declared sendable. A deterministic backfill that
copies ids off `evidence_ref_options` when the model returned none manufactures
that gate's own input: the model's refusal to cite is the signal, and filling it
in turns a lead with zero real citations into one that clears the gate.

These tests pin the fail-closed behaviour: an uncited score is never repaired
into a cited one, and the batch fails loudly instead.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import score_leads
from lead_machine import llm_client, sendability


def _valid_score(**overrides: object) -> dict[str, object]:
    score: dict[str, object] = {
        "score_schema_version": 2,
        "subscores": {
            "icp_fit": 15,
            "reputation_demand_proof": 10,
            "web_conversion_pain": 20,
            "mockup_leverage": 10,
            "reachability": 8,
            "offer_angle_clarity": 8,
            "evidence_confidence": 4,
        },
        "total_score": 75,
        "grade": "B",
        "confidence": "high",
        "hard_disqualifiers": [],
        "best_angle": "Website Cleanup",
        "recommended_action": "Verify the issue before outreach.",
        "why_this_lead": ["The evidence shows a concrete conversion issue."],
        "why_this_might_be_wrong": ["The configuration may be intentional."],
        "opener": "I noticed a specific issue on the public site.",
        "evidence_refs": [],
        "safe_outreach_claims": [],
        "unsafe_claims": ["The issue is definitely losing revenue."],
        "airtable_fields": {},
        "commercial_fit_state": "minor-fix/nurture",
        "commercial_materiality": "",
        "recurring_consequence": "",
        "root_cause_confidence": "",
        "offer_component": "",
        "packageable_solution": "",
        "value_mechanism": "",
        "beyond_free_fix": "",
        "self_fix_risk": "",
        "intentionality": "ambiguous",
        "contact_eligibility": "hold-commercial-case",
        "asset_state": "none",
        "promise_state": "asset-free",
        "asset_id": "",
        "asset_verifier_verdict": "",
    }
    score.update(overrides)
    return score


def _compact_records() -> list[dict[str, object]]:
    return [
        {
            "score_file": "chij123.json",
            "lead": {"business_name": "Uncited Garage Doors"},
            "evidence_ref_options": [
                {"id": "google_profile", "label": "Google profile"},
                {"id": "site_home", "label": "Site home page"},
                {"id": "site_contact", "label": "Site contact page"},
                {"id": "site_gallery", "label": "Site gallery"},
            ],
        }
    ]


def _uncited_bundle() -> dict[str, object]:
    return {
        "batch_id": "batch_001",
        "scores": [{"score_file": "chij123.json", "score": _valid_score()}],
    }


class UncitedScoresAreNeverBackfilledTest(unittest.TestCase):
    def _run_batch(self, *, first: Exception, second: Exception) -> object:
        calls = {"n": 0}

        def fake_score_batch(prompt, *, model, max_tokens):  # noqa: ANN001
            calls["n"] += 1
            raise first if calls["n"] == 1 else second

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(score_leads.llm_client, "score_batch", side_effect=fake_score_batch), \
                 patch.object(score_leads, "build_validation_repair_prompt", return_value="repair"), \
                 patch.object(
                     score_leads,
                     "_write_validation_failed_llm_response",
                     return_value=Path(tmp) / "dbg.json",
                 ):
                with self.assertRaises(RuntimeError) as caught:
                    score_leads._score_batch_with_debug(
                        "prompt",
                        batch_id="batch_001",
                        bundle_dir=Path(tmp),
                        compact_records=_compact_records(),
                        model="m",
                        max_tokens=4096,
                    )
        return caught.exception

    def test_empty_evidence_refs_are_not_filled_from_evidence_ref_options(self) -> None:
        # The model returned an otherwise-perfect bundle and simply declined to
        # cite. That refusal must survive: the batch fails rather than becoming
        # a scored lead carrying three ids the model never chose.
        bundle = _uncited_bundle()
        raw_text = json.dumps(bundle)
        failure = (
            "Validation failures:\n"
            "- scores[0] (chij123.json): evidence_refs must be a non-empty list"
        )
        first = llm_client.InvalidLlmResponse(
            failure,
            raw_text=raw_text,
            parsed_json=copy.deepcopy(bundle),
            usage={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
        )
        second = llm_client.InvalidLlmResponse(
            failure,
            raw_text=raw_text,
            parsed_json=copy.deepcopy(bundle),
            usage={"input_tokens": 700, "output_tokens": 100, "total_tokens": 800},
        )

        error = self._run_batch(first=first, second=second)

        self.assertIn("batch_001", str(error))
        self.assertIn("evidence_refs must be a non-empty list", str(error))

    def test_failed_citation_batch_still_charges_both_paid_calls(self) -> None:
        # Both attempts are real paid calls; the caps must see them even though
        # the batch produced no bundle.
        bundle = _uncited_bundle()
        raw_text = json.dumps(bundle)
        first = llm_client.InvalidLlmResponse(
            "validation failed",
            raw_text=raw_text,
            parsed_json=copy.deepcopy(bundle),
            usage={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
        )
        second = llm_client.InvalidLlmResponse(
            "still bad",
            raw_text=raw_text,
            parsed_json=copy.deepcopy(bundle),
            usage={"input_tokens": 700, "output_tokens": 100, "total_tokens": 800},
        )

        error = self._run_batch(first=first, second=second)

        self.assertEqual(
            getattr(error, "usage", None),
            {"input_tokens": 1700, "output_tokens": 300, "total_tokens": 2000},
        )
        self.assertEqual(getattr(error, "call_count", None), 2)

    def test_no_deterministic_evidence_ref_backfill_exists(self) -> None:
        # Pin the removal itself. Any helper that writes evidence_refs the model
        # did not produce re-opens the same hole, so the name must stay gone.
        self.assertFalse(
            hasattr(score_leads, "_fallback_fill_empty_evidence_refs"),
            "machine-minted evidence citations were reintroduced",
        )

    def test_uncited_lead_is_blocked_and_three_minted_refs_would_clear_the_gate(self) -> None:
        # Why the backfill mattered: filling three ids is exactly enough to
        # silence the blocker that exists to catch an uncited lead.
        uncited = {"Place Key": "chij123", "Business Name": "X", "Evidence Refs": json.dumps([])}
        minted = {
            "Place Key": "chij123",
            "Business Name": "X",
            "Evidence Refs": json.dumps(["google_profile", "site_home", "site_contact"]),
        }

        self.assertIn(
            sendability.EVIDENCE_COUNT_BLOCKER,
            sendability.evaluate_sendability(uncited)["send_blockers"],
        )
        self.assertNotIn(
            sendability.EVIDENCE_COUNT_BLOCKER,
            sendability.evaluate_sendability(minted)["send_blockers"],
        )


if __name__ == "__main__":
    unittest.main()
