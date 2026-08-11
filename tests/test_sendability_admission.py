"""`Sendable=Yes` on a lead the hard ICP gate skipped has to be unreachable.

It was not. `enrich_leads` wrote `derived.hard_icp_skip` and `apply_sendability`
never read it, so the two decisions could disagree — and did, on 31 rows across
14 businesses in `output/scored_*.csv`. Twenty-nine of those carried
`high_review_count`, a reason that no longer exists now the review ceiling is
gone; the two `too_few_reviews` rows were the genuine leak.

Asserting the combination is absent today would not stop the next path from
reintroducing it, so these tests are about shape rather than values: one
`admit()` owns the verdict, nothing else may spell one, and the ICP check is
admit's own rather than something a caller supplies.
"""
from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path
from typing import Any

from lead_machine.buyer_signals import REVIEW_DEMAND_THRESHOLDS
from lead_machine.manual_scores import merge_manual_score
from lead_machine.sendability import (
    CLAIM_INTEGRITY_BLOCKERS,
    EVIDENCE_BLOCKERS,
    HARD_DISQUALIFIER_BLOCKER,
    HARD_ELIGIBILITY_BLOCKERS,
    HARD_ICP_SKIP_BLOCKER,
    INVALID_REF_BLOCKER,
    MISSING_REVIEW_DATA_BLOCKER,
    OUT_OF_COUNTRY_BLOCKER,
    UNSUPPORTED_ABSENCE_CLAIM_BLOCKER,
    Admission,
    admit,
    apply_sendability,
    evaluate_packet_tier,
    evaluate_sendability,
    is_contact_ready,
    readiness_flags,
)
from refresh_packet_tiers import refresh_row

REPO_ROOT = Path(__file__).resolve().parent.parent

# The row key that reaches the CSV and Airtable, plus its lowercase alias.
SENDABLE_KEYS = frozenset({"Sendable", "sendable"})
# The verdict itself. The same keys also carry counts on the dashboard and
# booleans on a lead card, which are readings of a verdict rather than one being
# minted; only the "Yes"/"No" string is the declaration.
VERDICT_LITERALS = frozenset({"Yes", "No"})


def production_sources() -> list[Path]:
    """Every module the pipeline runs. Tests are excluded deliberately.

    A fixture row that hardcodes `Sendable: Yes` is an input to the gate, not a
    path around it, so scanning `tests/` would only forbid writing tests.
    """
    paths = sorted(REPO_ROOT.glob("*.py"))
    paths.extend(sorted((REPO_ROOT / "lead_machine").rglob("*.py")))
    return paths


def _mints_a_verdict(value: ast.AST) -> bool:
    """True when *value* is a hardcoded "Yes"/"No", directly or via a ternary."""
    if isinstance(value, ast.Constant):
        return value.value in VERDICT_LITERALS
    if isinstance(value, ast.IfExp):
        return _mints_a_verdict(value.body) or _mints_a_verdict(value.orelse)
    return False


def sendable_verdict_writes(source: str, *, label: str) -> list[tuple[str, str, ast.AST]]:
    """Return (label:function, key, value_node) for every minted verdict.

    Covers the three ways a value lands under the key: subscript assignment,
    a dict literal (which also covers `row.update({...})` and `{**row, ...}`),
    and `setdefault`.
    """
    tree = ast.parse(source)
    enclosing: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                enclosing.setdefault(child, node.name)

    found: list[tuple[str, str, ast.AST]] = []

    def record(node: ast.AST, key: str, value: ast.AST) -> None:
        if _mints_a_verdict(value):
            found.append((f"{label}:{enclosing.get(node, '<module>')}", key, value))

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value in SENDABLE_KEYS
                ):
                    record(node, target.slice.value, node.value)
        elif isinstance(node, ast.Dict):
            for key_node, value_node in zip(node.keys, node.values):
                if isinstance(key_node, ast.Constant) and key_node.value in SENDABLE_KEYS:
                    record(node, key_node.value, value_node)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setdefault"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in SENDABLE_KEYS
        ):
            record(node, node.args[0].value, node.args[1] if len(node.args) > 1 else node)

    return found


def verdict_spellers(source: str) -> list[str]:
    """Functions that bind a verdict literal to something named for sendability.

    Covers `sendable = "Yes"`, `self.sendable = "No"`, `d["sendable_flag"] = ...`
    and a bare `return "Yes"` from a function whose own name says sendable.
    """
    tree = ast.parse(source)
    spellers: list[str] = []

    def names(target: ast.AST) -> str:
        if isinstance(target, ast.Name):
            return target.id
        if isinstance(target, ast.Attribute):
            return target.attr
        if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant):
            return str(target.slice.value)
        return ""

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Assign) and _mints_a_verdict(child.value):
                if any("sendable" in names(target).lower() for target in child.targets):
                    spellers.append(node.name)
                    break
            if (
                isinstance(child, ast.Return)
                and child.value is not None
                and _mints_a_verdict(child.value)
                and "sendable" in node.name.lower()
            ):
                spellers.append(node.name)
                break

    return spellers


def apply_sendability_call_sites() -> set[str]:
    sites: set[str] = set()
    for path in production_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        enclosing: dict[ast.AST, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    enclosing.setdefault(child, node.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "apply_sendability":
                label = path.relative_to(REPO_ROOT).as_posix()
                sites.add(f"{label}:{enclosing.get(node, '<module>')}")
    return sites


class AdmissionIsTheOnlyRouteToSendableTest(unittest.TestCase):
    def test_no_production_module_mints_a_sendable_verdict(self) -> None:
        # The structural half of the invariant, and it has no exemptions —
        # `sendability.py` included. `hard_icp_skip` was only ever advisory
        # because any module could write its own `Sendable`, and two of them did:
        # `manual_lead_insert.build_manual_lead_card` turned an operator boolean
        # straight into a verdict, and `upload_airtable.is_sendable_record`
        # trusted a packet card's stored one. Neither passed through the gate.
        # A new sendable path has to fail here before it can ship, and the only
        # way to make it pass is to get the string from an Admission.
        offenders = sorted(
            f"{path.relative_to(REPO_ROOT).as_posix()} line {value.lineno} ({site})"
            for path in production_sources()
            for site, _, value in sendable_verdict_writes(
                path.read_text(encoding="utf-8"),
                label=path.relative_to(REPO_ROOT).as_posix(),
            )
        )

        self.assertEqual(offenders, [])

    def test_exactly_one_function_spells_the_verdict(self) -> None:
        # The counterpart to the scan above. That scan only sees the moment a
        # verdict lands on a row, so `flag = "Yes"` followed by
        # `row["Sendable"] = flag` would slip past it. This closes the honest
        # version of that laundering — anything named for sendability — and
        # leaves one sanctioned speller, so a reviewer has one place to look.
        #
        # Not a blanket ban on the string: "Yes"/"No" is how this codebase spells
        # every Airtable boolean, and Packet Eligible is not this decision.
        self.assertEqual(
            sorted(
                f"{path.relative_to(REPO_ROOT).as_posix()}:{name}"
                for path in production_sources()
                for name in verdict_spellers(path.read_text(encoding="utf-8"))
            ),
            ["lead_machine/sendability.py:sendable_flag"],
        )

    def test_the_scan_actually_catches_a_new_sendable_path(self) -> None:
        # Guards the guard. A scanner that silently matches nothing would let
        # every test above pass while the invariant rotted.
        smuggled = (
            "def ship(row):\n"
            "    row['Sendable'] = 'Yes'\n"
            "    row.update({'sendable': 'Yes' if ok else 'No'})\n"
            "    row.setdefault('Sendable', 'Yes')\n"
            "    return row\n"
            "\n"
            "def launder(row):\n"
            "    sendable = 'Yes'\n"
            "    row['Sendable'] = sendable\n"
            "    return row\n"
        )

        self.assertEqual(
            [site for site, _, _ in sendable_verdict_writes(smuggled, label="smuggled")],
            ["smuggled:ship"] * 3,
        )
        # The two scanners overlap on a direct write and only `verdict_spellers`
        # sees the laundered one, which is the point of running both.
        self.assertEqual(verdict_spellers(smuggled), ["ship", "launder"])

    def test_every_apply_sendability_caller_is_a_known_scoring_path(self) -> None:
        # The choke point only chokes what flows through it. A new caller is a
        # new way for a lead to become sendable and has to be named here, where
        # the reviewer is looking at the invariant.
        self.assertEqual(
            apply_sendability_call_sites(),
            {
                "score_leads.py:score_record",
                "lead_machine/manual_scores.py:merge_manual_score",
                "refresh_packet_tiers.py:refresh_row",
            },
        )


class AdmitRefusesAHardIcpSkipTest(unittest.TestCase):
    def test_admit_refuses_however_clean_the_blockers_the_caller_hands_it(self) -> None:
        # The check belongs to admit, not to the caller's blocker list. A caller
        # that computed nothing wrong still cannot get an admission.
        row = {
            "Raw Evidence": json.dumps(
                {"derived": {"hard_icp_skip": True, "hard_icp_reasons": ["too_few_reviews"]}}
            )
        }

        admission = admit(row, [])

        self.assertFalse(admission.admitted)
        self.assertEqual(admission.sendable_flag, "No")
        self.assertEqual(admission.blockers, (f"{HARD_ICP_SKIP_BLOCKER}: too_few_reviews",))

    def test_admit_reads_the_verdict_wherever_the_row_still_carries_it(self) -> None:
        derived = {"derived": {"hard_icp_skip": True, "hard_icp_reasons": ["dnc_suppressed"]}}
        for label, row in (
            ("raw evidence json", {"Raw Evidence": json.dumps(derived)}),
            ("raw evidence dict", {"raw_evidence": derived}),
            ("top level derived", dict(derived)),
        ):
            with self.subTest(carrier=label):
                self.assertFalse(admit(row).admitted)

    def test_an_unreadable_verdict_fails_closed(self) -> None:
        # `make_llm_batch` raises on a malformed flag because stopping a batch is
        # cheap. Raising here would take down a whole scoring run over one
        # hand-edited row, and refusing to send is the same answer for less.
        for value in ("string", [1, 2], 1, "false"):
            with self.subTest(value=value):
                self.assertFalse(admit({"derived": {"hard_icp_skip": value}}).admitted)

        for value in (None, False, ""):
            with self.subTest(value=value):
                self.assertTrue(admit({"derived": {"hard_icp_skip": value}}).admitted)

    def test_an_admission_with_no_blockers_is_the_only_yes(self) -> None:
        self.assertEqual(Admission(()).sendable_flag, "Yes")
        self.assertEqual(Admission(("anything",)).sendable_flag, "No")


class NoScoringPathEmitsSendableYesOnASkippedLeadTest(unittest.TestCase):
    """End-to-end proof through the real scorers, not just the gate in isolation."""

    def _enriched(self, **derived: Any) -> dict:
        enriched = {
            "google": {
                "business_name": "A+ Landscaping LLC",
                "city": "Kirtland",
                "phone": "(440) 637-5848",
                "website_url": "https://example.com",
                "google_maps_url": "https://maps.google.com/?cid=123",
                "rating": "4.9",
                "reviews": "18",
                "place_id": "ChIJ123",
            },
            "buyer_intent": {
                "money_signal": "yes",
                "demand_signal": {
                    "state": "yes",
                    "source": "review_count",
                    "velocity_band": "unknown",
                    "demand_band": "ideal_growth",
                    "evidence": ["Lifetime review count places this in the ideal_growth demand band."],
                },
                "crm_booking_stack": {"detected_tools": ["ServiceTitan"], "has_paid_crm": True},
            },
            "evidence": {
                "site_status": "weak",
                "screenshots": {"desktop": "desktop.png", "mobile": "mobile.png"},
                "visible_pain_score": 35,
                "visible_pain_reasons": ["No clear estimate CTA"],
                "send_trigger": "No clear estimate CTA",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 18 reviews"},
                    {"id": "E2", "field": "site_issue_flags", "value": "no_clear_cta_detected"},
                    {"id": "E3", "field": "send_trigger", "value": "No clear estimate CTA"},
                ],
            },
        }
        if derived:
            enriched["derived"] = derived
        return enriched

    def _manual_score(self) -> dict:
        return {
            "score_schema_version": 2,
            "subscores": {
                "icp_fit": 14,
                "reputation_demand_proof": 12,
                "web_conversion_pain": 18,
                "mockup_leverage": 10,
                "reachability": 7,
                "offer_angle_clarity": 9,
                "evidence_confidence": 5,
            },
            "confidence": "medium",
            "hard_disqualifiers": [],
            "best_angle": "Estimate Funnel",
            "commercial_fit_state": "commercially-sendable",
            "commercial_materiality": "Premium estimate requests can fail to reach a clear conversion path.",
            "recurring_consequence": "The same path affects each new high-intent visitor.",
            "root_cause_confidence": "Fresh rendered evidence shows the CTA-path weakness.",
            "offer_component": "Get the Lead",
            "packageable_solution": "Rebuild the estimate path and trust placement.",
            "value_mechanism": "Reduce friction between existing demand and estimate requests.",
            "beyond_free_fix": "The CTA, mobile path, trust proof, and routing require one coherent system.",
            "self_fix_risk": "The existing multi-step path has persisted and needs coordinated implementation.",
            "intentionality": "The verified customer path contradicts an intentional low-conversion design.",
            "contact_eligibility": "contact-now",
            "asset_state": "none",
            "promise_state": "asset-free",
            "asset_id": "",
            "asset_verifier_verdict": "",
            "recommended_action": "Build mockup then SMS owner",
            "why_this_lead": ["Strong reviews", "Visible quote-path issue"],
            "why_this_might_be_wrong": [],
            "opener": "Your reviews are strong, but the quote path looks hard to find. Want me to send a cleaner version?",
            "evidence_refs": ["E1", "E2"],
            "safe_outreach_claims": [
                {"claim": "Your quote path looks hard to find.", "evidence_refs": ["E2"]},
                {"claim": "Your Google profile shows strong reviews.", "evidence_refs": ["E1"]},
            ],
            "unsafe_claims": ["Do not claim they are losing jobs."],
            "airtable_fields": {},
        }

    def _scored(self, enriched: dict) -> dict:
        return merge_manual_score(enriched, self._manual_score(), fallback_vertical="hardscaping")

    def test_the_control_lead_really_is_sendable(self) -> None:
        # Without this the tests below prove nothing: every one of them would
        # also pass on a lead blocked for some unrelated reason.
        self.assertEqual(self._scored(self._enriched())["Sendable"], "Yes")

    def test_the_scorer_refuses_a_skipped_lead(self) -> None:
        row = self._scored(
            self._enriched(hard_icp_skip=True, hard_icp_reasons=["too_few_reviews"])
        )

        self.assertEqual(row["Sendable"], "No")
        self.assertIn(HARD_ICP_SKIP_BLOCKER, row["Send Blockers"])

    def test_a_skipped_lead_gets_no_packet_rather_than_a_manual_review_one(self) -> None:
        # `hard_icp_skip` joins the evidence blockers for the same reason
        # suppression does: a lead we are not prospecting should not come back
        # through the manual-review door.
        row = self._scored(
            self._enriched(hard_icp_skip=True, hard_icp_reasons=["too_few_reviews"])
        )

        self.assertEqual(row["Minimum Evidence Passed"], "No")
        self.assertEqual(row["Packet Tier"], "No Packet")
        self.assertEqual(row["Packet Eligible"], "")

    def test_regoverning_an_already_sendable_row_withdraws_it(self) -> None:
        # The re-scoring paths matter as much as the first pass: a row can be
        # sendable, get suppressed or fall under the floor, and be governed again.
        sendable = self._scored(self._enriched())
        self.assertEqual(sendable["Sendable"], "Yes")

        raw = json.loads(sendable["Raw Evidence"])
        raw["derived"] = {"hard_icp_skip": True, "hard_icp_reasons": ["dnc_suppressed"]}
        sendable["Raw Evidence"] = json.dumps(raw)

        self.assertEqual(apply_sendability(sendable)["Sendable"], "No")
        self.assertEqual(refresh_row(sendable)["Sendable"], "No")

    def test_the_review_ceiling_no_longer_withdraws_the_profile_that_booked(self) -> None:
        # 2026-07-28's booked call was a 222-review business. Under the old
        # ceiling that lead carried hard_icp_skip, so closing this gate while the
        # ceiling still stood would have made the one profile that has ever
        # converted unsendable — which is why the ceiling came out first.
        #
        # The demand band used to govern such a lead as a second gate: `mature`
        # demanded a visible pain score of 40 rather than 16. That threshold is a
        # rank input now — it says how far down the queue a lead sits, not
        # whether it is in it — so a 222-review business with a named leak is
        # sendable at either pain score, and the band still travels with it.
        enriched = self._enriched()
        enriched["google"]["reviews"] = "222"

        low_pain = self._scored(enriched)
        self.assertEqual(low_pain["Sendable"], "Yes")
        self.assertEqual(low_pain["Send Blockers"], "")
        self.assertNotIn(HARD_ICP_SKIP_BLOCKER, low_pain["Send Blockers"])

        enriched["evidence"]["visible_pain_score"] = 45
        higher_pain = self._scored(enriched)

        self.assertEqual(higher_pain["Review Demand Band"], "mature")
        self.assertEqual(higher_pain["Sendable"], "Yes")
        self.assertEqual(higher_pain["Send Blockers"], "")

    def test_absent_review_data_is_not_read_as_a_business_with_no_reviews(self) -> None:
        # A blank Reviews cell means the collector never got an answer, and
        # `_review_count` renders that as the integer 0 — so "we did not look"
        # and "nobody has reviewed them" arrive at the gate identically.
        #
        # The old `reviews < 5` gate hid this by refusing both. Demoting it left
        # the wrong half showing: 50 of the 1,092 re-gated sendables had no
        # review data at all, and a lead we cannot confirm is a live business is
        # not one to hand a rep. This is the `money_signal` rule in the other
        # direction — unknown is an unanswered question, not a pass.
        #
        # Claim integrity rather than hard eligibility, because a re-enrich fixes
        # it: the lead routes to a human, not to the bin.
        enriched = self._enriched()
        enriched["google"]["reviews"] = ""
        enriched["google"]["rating"] = ""

        row = self._scored(enriched)

        self.assertEqual(row["Sendable"], "No")
        self.assertIn(MISSING_REVIEW_DATA_BLOCKER, row["Send Blockers"])
        # Reachable by a human, not written off.
        self.assertEqual(row["Minimum Evidence Passed"], "Yes")

    def test_a_genuine_zero_review_business_is_told_apart_from_a_blank(self) -> None:
        # The distinction only means something if a real 0 is allowed through.
        # A brand-new business with a named leak is a legitimate lead; it simply
        # ranks low. Reading the explicit 0 as "missing" would re-create the
        # conflation this blocker exists to end.
        enriched = self._enriched()
        enriched["google"]["reviews"] = "0"

        row = self._scored(enriched)

        self.assertEqual(row["Sendable"], "Yes")
        self.assertNotIn(MISSING_REVIEW_DATA_BLOCKER, row["Send Blockers"])

    def test_a_suppressed_lead_gets_no_packet_not_a_manual_review_one(self) -> None:
        # The DNC chain documented in `compliance.py` runs: suppression writes
        # `dnc_suppressed` into hard_disqualifiers, `has_hard_disqualifier` reads
        # it, and the resulting blocker must force `minimum_evidence_passed`
        # False so the lead lands on `No Packet` rather than `Manual Review`.
        #
        # Nothing pinned the last step. It holds only because
        # HARD_DISQUALIFIER_BLOCKER sits in HARD_ELIGIBILITY_BLOCKERS, and that
        # bucket is heterogeneous — it also carries "out of business" and "weak
        # reputation", which look like claim-integrity concerns. Moving it would
        # write a drafted packet, with an opener, for a business that said no.
        row = self._scored(self._enriched())
        row["Hard Disqualifiers"] = "dnc_suppressed"
        result = evaluate_sendability(row)

        self.assertFalse(result["sendable"])
        self.assertFalse(result["minimum_evidence_passed"])
        tier, _reason = evaluate_packet_tier(row, result)
        self.assertEqual(tier, "No Packet")

    def test_hard_disqualifiers_stay_on_the_hard_eligibility_side(self) -> None:
        # The membership the test above depends on, asserted directly so a move
        # fails here with the reason rather than only there with a symptom.
        self.assertIn(HARD_DISQUALIFIER_BLOCKER, HARD_ELIGIBILITY_BLOCKERS)
        self.assertNotIn(HARD_DISQUALIFIER_BLOCKER, CLAIM_INTEGRITY_BLOCKERS)

    def test_evaluate_sendability_reports_the_same_verdict_as_the_admission(self) -> None:
        row = self._scored(
            self._enriched(hard_icp_skip=True, hard_icp_reasons=["too_few_reviews"])
        )
        result = evaluate_sendability(row)

        self.assertFalse(result["sendable"])
        self.assertEqual(result["sendable_flag"], "No")


class ReviewDemandBandsAreASeparateAxisTest(unittest.TestCase):
    def test_the_demand_bands_did_not_move_when_the_icp_ceiling_was_removed(self) -> None:
        # `buyer_signals` reads review count too, and detailing's numbers (126 /
        # 401) are close enough to the deleted ICP ceiling of 130 to look like
        # the same knob. They are not: these decide how much visible pain an
        # established business must show, not whether it is in the ICP at all.
        # Pinned so a future ceiling-shaped edit cannot drift them in passing.
        self.assertEqual(
            REVIEW_DEMAND_THRESHOLDS,
            {
                "detailing": {
                    "early_proof_min": 5,
                    "ideal_growth_min": 15,
                    "established_min": 126,
                    "mature_min": 401,
                },
                "default": {
                    "early_proof_min": 5,
                    "ideal_growth_min": 15,
                    "established_min": 76,
                    "mature_min": 201,
                },
            },
        )


class AStoredSendableYesCannotOutrankTheIcpGateTest(unittest.TestCase):
    """The packet-tier derivation, which is where the invariant is enforced.

    Total Airtable mirroring no longer filters rows in the uploader, so rep
    readiness depends on the derived packet tier. `readiness_flags` used to
    return a stored `Sendable: Yes` verbatim, allowing a row scored before it
    acquired a hard skip to keep a stale Sendable Packet tier. The Airtable
    send queue's readiness clause is
    `Packet Tier = 'Sendable Packet'` (queue_state.py).

    Tightening is still allowed to skip the gate: a human writing "No" never
    needs permission.
    """

    def _row(self, *, sendable: str, skipped: bool) -> dict[str, Any]:
        raw = {
            "derived": {
                "hard_icp_skip": skipped,
                "hard_icp_reasons": ["too_few_reviews"] if skipped else [],
            }
        }
        return {
            "Business Name": "Lorain Garage Door Repair Co",
            "Sendable": sendable,
            "Packet Eligible": "Yes",
            "Packet Tier": "Sendable Packet",
            "Commercial Fit State": "commercially-sendable",
            "Contact Eligibility": "contact-now",
            # Reachability, so the ICP-skip assertions below are testing the ICP
            # gate rather than riding a fixture that has no number to call.
            "Phone": "(440) 555-0188",
            "Reviews": "37",
            "Raw Evidence": json.dumps(raw),
        }

    def test_a_stored_yes_is_withdrawn_when_the_lead_is_skipped(self) -> None:
        self.assertEqual(readiness_flags(self._row(sendable="Yes", skipped=True))[0], "No")

    def test_contact_readiness_drops_the_skipped_lead(self) -> None:
        # Before the gate fix this returned True and packet-tier derivation
        # incorrectly left the lead in the rep's sendable queue.
        self.assertFalse(is_contact_ready(self._row(sendable="Yes", skipped=True)))

    def test_an_unskipped_lead_is_still_uploadable(self) -> None:
        # Without this the test above would pass for any reason at all.
        self.assertTrue(is_contact_ready(self._row(sendable="Yes", skipped=False)))

    def test_a_skipped_lead_cannot_hold_the_sendable_packet_tier(self) -> None:
        row = self._row(sendable="Yes", skipped=True)
        tier, _ = evaluate_packet_tier(row, evaluate_sendability(row))

        self.assertNotEqual(tier, "Sendable Packet")

    def test_a_hand_edited_no_is_still_honoured(self) -> None:
        # Tightening never goes through the gate; the pre-existing contract.
        self.assertEqual(readiness_flags(self._row(sendable="No", skipped=False))[0], "No")


class AnEvidenceRefSeparatorTypoIsNotAFabricatedRefTest(unittest.TestCase):
    """`ev-visible_pain-reasons-15` and `ev-visible_pain_reasons-15` are the
    same citation written two ways.

    The generator emits `ev-<field>-<n>` with the field's own underscores
    intact; a model writing the reference back sometimes hyphenates them. The
    membership test was exact, so the id failed to resolve and
    `INVALID_REF_BLOCKER` fired — indistinguishable, in the output, from a
    model that invented a citation.

    Twelve leads in one 194-row corpus were rejected this way, every one of
    them a separator difference and none a fabrication. Two were A-grade with
    no other blocker (scores 95 and 93). The blocker exists to catch invented
    evidence; spending real leads on punctuation is not what it is for.
    """

    def _row(self, cited: str) -> dict[str, Any]:
        return {
            "Business Name": "Dynasty Epoxy Louisville",
            "Raw Evidence": json.dumps(
                {
                    "evidence": {
                        "evidence_items": [
                            {"id": "ev-visible_pain_reasons-15", "field": "visible_pain_reasons", "value": "x"},
                            {"id": "ev-google_rating-1", "field": "google_rating", "value": "5.0"},
                        ]
                    }
                }
            ),
            "Evidence Refs": json.dumps(["ev-google_rating-1", cited]),
            "Safe Outreach Claims": f"The scan recorded the visible pain. [{cited}]",
        }

    def test_a_hyphenated_field_separator_still_resolves(self) -> None:
        blockers = evaluate_sendability(self._row("ev-visible_pain-reasons-15"))["send_blockers"]
        self.assertNotIn(INVALID_REF_BLOCKER, blockers)

    def test_the_exact_spelling_still_resolves(self) -> None:
        # The control: normalizing must not break the ids that already worked.
        blockers = evaluate_sendability(self._row("ev-visible_pain_reasons-15"))["send_blockers"]
        self.assertNotIn(INVALID_REF_BLOCKER, blockers)

    def test_a_genuinely_invented_ref_is_still_blocked(self) -> None:
        # The blocker's actual job, and the reason this is normalization rather
        # than a relaxation: a citation to evidence that does not exist in any
        # spelling must still fail.
        blockers = evaluate_sendability(self._row("ev-fabricated_metric-99"))["send_blockers"]
        self.assertIn(INVALID_REF_BLOCKER, blockers)


class ALeadOutsideTheCountryIsNotInIcpTest(unittest.TestCase):
    """An Ohio batch admitted a business in Kolkata.

    `Urban Interior - 3D and Metallic Epoxy flooring`, address
    "…Behala, Kolkata, West Bengal 700041, India", phone "091230 69950", came
    back from the query "metallic epoxy flooring Toledo OH" tagged
    `Metro Context: toledo`, scored Grade A, and cleared for contact with
    `hard_icp_skip: False`. No gate between the search and the rep looked at
    what country the business was in.

    The check is deliberately narrow — country only, not metro distance. A
    business two metros over is a judgment call about reach; one on another
    continent is not one this pipeline ever wants to make.
    """

    def _row(self, address: str) -> dict[str, Any]:
        return {
            "Business Name": "Urban Interior",
            "Raw Evidence": json.dumps({"google": {"address": address}}),
        }

    def _blockers(self, row: dict[str, Any]) -> list[str]:
        return evaluate_sendability(row)["send_blockers"]

    def _has_out_of_country(self, row: dict[str, Any]) -> bool:
        # The blocker names the country it saw, the way the ICP one names its
        # reasons, so match the prefix rather than the bare constant.
        return any(b.startswith(OUT_OF_COUNTRY_BLOCKER) for b in self._blockers(row))

    def test_a_foreign_address_blocks_the_lead(self) -> None:
        for address in (
            "173/h, PN Mitra Ln, Tara Park, Behala, Kolkata, West Bengal 700041, India",
            "2219 Deer Run Rd, Wheatley, ON N0P 2P0, Canada",
        ):
            with self.subTest(address=address):
                self.assertTrue(self._has_out_of_country(self._row(address)))

    def test_a_us_address_is_not_blocked(self) -> None:
        for address in (
            "1121 N Meridian Rd, Youngstown, OH 44509, USA",
            "500 Main St, Akron, OH 44311, United States",
        ):
            with self.subTest(address=address):
                self.assertFalse(self._has_out_of_country(self._row(address)))

    def test_an_address_with_no_country_is_not_blocked(self) -> None:
        # Fails open. Plenty of records carry a bare street address, and a
        # missing country is not evidence of a foreign one — inferring a skip
        # from it would delete real leads to close a four-row hole.
        for address in ("", "500 Main St, Akron, OH 44311", "Akron, OH"):
            with self.subTest(address=address):
                self.assertFalse(self._has_out_of_country(self._row(address)))

    def test_the_blocker_survives_a_caller_supplied_clean_list(self) -> None:
        # It belongs to the choke point, not the caller — same contract as the
        # hard ICP verdict.
        self.assertFalse(admit(self._row("Kolkata, West Bengal 700041, India")).admitted)


class AnAbsenceClaimNeedsAPageBodyTest(unittest.TestCase):
    """A "they have no contact form" claim must be traceable to a page we read.

    `FORM_ABSENCE_CLAIM_BLOCKER` cannot do this. It is a *contradiction*
    detector — `strong_estimate_form AND has_form_absence_claim` — and on a
    lead whose body came back empty `estimate_form_detected` is "No" (or
    "Unknown"), so the first conjunct is false and the claim rides through
    unchallenged. `sendability` never read `form_detection_method` at all.

    This is defence in depth, not the fix. The enricher no longer mints the
    claim; this stops one that arrives by another route — a stale row scored
    before that change, a manual card, an LLM reason written off a blank crawl
    — from being sent to a business whose form is sitting on a page our static
    parser could not read.
    """

    def _row(self, website: dict[str, Any], claim: str = "No contact form detected") -> dict[str, Any]:
        return {
            "Business Name": "Door Works & Construction",
            "visible_pain_reasons": claim,
            "Raw Evidence": json.dumps({"website": website}),
        }

    _READABLE = {
        "site_status": "Loaded",
        "site_issue_flags": ["no_form_detected"],
        "form_detection_method": "static_html",
    }
    _NO_BODY_METHOD = {
        "site_status": "Loaded",
        "site_issue_flags": [],
        "form_detection_method": "no_body",
    }
    _EMPTY_BODY_FLAG = {
        "site_status": "Loaded",
        "site_issue_flags": ["empty_body_on_200"],
        "form_detection_method": "none",
    }

    def _blockers(self, row: dict[str, Any]) -> list[str]:
        return evaluate_sendability(row)["send_blockers"]

    def test_no_body_blocks_the_claim(self) -> None:
        self.assertIn(UNSUPPORTED_ABSENCE_CLAIM_BLOCKER, self._blockers(self._row(self._NO_BODY_METHOD)))

    def test_the_empty_body_flag_blocks_the_claim(self) -> None:
        self.assertIn(UNSUPPORTED_ABSENCE_CLAIM_BLOCKER, self._blockers(self._row(self._EMPTY_BODY_FLAG)))

    def test_a_claim_off_a_page_we_read_is_not_blocked(self) -> None:
        # The control: the blocker is about provenance, not about disliking
        # absence claims. A form absence found on a page we parsed is the
        # signal the pipeline runs on.
        self.assertNotIn(UNSUPPORTED_ABSENCE_CLAIM_BLOCKER, self._blockers(self._row(self._READABLE)))

    def test_no_body_without_an_absence_claim_is_not_blocked(self) -> None:
        # The other control: an unreadable body is not itself disqualifying.
        # Plenty of these leads are sendable on a leak that has nothing to do
        # with forms.
        row = self._row(self._NO_BODY_METHOD, claim="Website looks outdated")

        self.assertNotIn(UNSUPPORTED_ABSENCE_CLAIM_BLOCKER, self._blockers(row))

    def test_the_blocker_is_an_evidence_blocker(self) -> None:
        # Minimum-evidence failures are what keep a lead out of the packet
        # tier, not just out of the send flag.
        self.assertIn(UNSUPPORTED_ABSENCE_CLAIM_BLOCKER, EVIDENCE_BLOCKERS)

    _HARD_FAILURES = {
        "dns_domain_failure": {"site_status": "broken", "site_issue_flags": ["dns_domain_failure"]},
        "static_fetch_failed": {
            "site_status": "broken",
            "site_issue_flags": ["static_fetch_failed", "rendered_load_failed"],
        },
        "homepage_http_hard_failure": {
            "site_status": "broken",
            "site_issue_flags": ["homepage_http_hard_failure"],
        },
        "ssl_cert_hard_failure": {"site_status": "broken", "site_issue_flags": ["ssl_cert_hard_failure"]},
    }

    def test_a_hard_fetch_failure_blocks_the_claim(self) -> None:
        """`empty_body_on_200` is only the 200-with-blank-body spelling. A DNS
        failure, a 404 or a dead TLS handshake never reached a body either, and
        those set none of the three markers this gate used to recognise — so
        the blocker was unreachable on the entire broken-site lane, which was 8
        of 56 sends in the recovery corpus.
        """
        for name, website in self._HARD_FAILURES.items():
            with self.subTest(flag=name):
                self.assertIn(
                    UNSUPPORTED_ABSENCE_CLAIM_BLOCKER, self._blockers(self._row(website))
                )

    def test_a_cta_or_phone_absence_claim_needs_a_body_too(self) -> None:
        """The form family was gated; the CTA/phone family was not, and it is
        the one that actually shipped. 46 of 56 recovery-corpus sendables
        asserted "Phone number hard to find" — every one of them had a phone
        number on the record, and 28 had no website at all, so there was no
        page on which a number could be buried.
        """
        claims = [
            "No clear estimate CTA",
            "Phone number hard to find",
            "The crawl flagged no clear estimate CTA and a hard-to-find phone number.",
        ]
        for claim in claims:
            with self.subTest(claim=claim):
                self.assertIn(
                    UNSUPPORTED_ABSENCE_CLAIM_BLOCKER,
                    self._blockers(self._row(self._EMPTY_BODY_FLAG, claim=claim)),
                )

    def test_a_cta_claim_off_a_page_we_read_is_still_allowed(self) -> None:
        # The control for the widened family: a weak CTA found on a page we
        # actually parsed is a real leak and the pipeline runs on it.
        self.assertNotIn(
            UNSUPPORTED_ABSENCE_CLAIM_BLOCKER,
            self._blockers(self._row(self._READABLE, claim="No clear estimate CTA")),
        )

    def test_a_missing_website_claim_survives_an_empty_body(self) -> None:
        # The leak that carries the no-site lane. "No owned website was found"
        # is a statement about the search, not about a page we failed to read,
        # and gating it would delete the largest sendable category.
        self.assertNotIn(
            UNSUPPORTED_ABSENCE_CLAIM_BLOCKER,
            self._blockers(self._row(self._EMPTY_BODY_FLAG, claim="No owned website was found")),
        )


if __name__ == "__main__":
    unittest.main()
