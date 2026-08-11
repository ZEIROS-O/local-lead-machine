"""Offline request/response tests for the send-queue runtime scripts.

Covers query_ready_to_send.py (read-only Airtable query) and mark_sent.py
(status write-back). All Airtable traffic is mocked; no test touches the
network or a real base.
"""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import mark_sent
import query_ready_to_send as qrs
from lead_machine import compliance
from lead_machine import queue_state
from lead_machine import sendability


# A base row as it reads back after the collapse: one verdict column plus the
# human-owned Status. The three readiness columns it replaces are gone.
#
# `Phone` and `Reviews` are here because a real readback always carries them —
# `Phone` is in `REQUIRED_READINESS_FIELDS`, so the query refuses to serve a
# queue without it — and `contact_readiness_failures` now re-derives
# reachability from them rather than trusting the stored verdict. A fixture
# without them is not a base row, it is a shape the base cannot produce.
READY_FIELDS = {
    "Verdict": "send",
    "Status": "New",
    "Consent Posture": "express written consent",
    "Phone": "(216) 555-0123",
    "Reviews": "42",
}


def _response(status_code=200, payload=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text or (json.dumps(payload) if payload is not None else "")
    resp.json.return_value = payload if payload is not None else {}
    return resp


class TestBuildFormula(unittest.TestCase):
    def test_formula_reads_the_shared_static_queue_expression(self):
        with patch.object(queue_state, "QUEUE_FILTER_CLAUSES", "CANONICAL_QUEUE", create=True):
            self.assertEqual(
                qrs.build_formula(""),
                "AND(CANONICAL_QUEUE)",
            )

    def test_base_formula_without_vertical(self):
        formula = qrs.build_formula("")
        self.assertEqual(
            formula,
            "AND({Verdict}='send',{Status}='New')",
        )

    def test_vertical_clause_added_and_quotes_escaped(self):
        formula = qrs.build_formula("Bob's Epoxy")
        self.assertIn(",{Vertical}='Bob\\'s Epoxy')", formula)


class TestFetchReadyRecords(unittest.TestCase):
    def test_display_fields_exclude_removed_machine_reasoning_columns(self):
        removed = {
            "Commercial Materiality",
            "Recurring Consequence",
            "Root Cause Confidence",
            "Packageable Solution",
            "Value Mechanism",
            "Beyond Free Fix",
            "Self Fix Risk",
            "Intentionality",
            "Evidence Snapshot",
            "Form Strength",
            "Recommended Action",
            "Asset ID",
            "Asset Verifier Verdict",
        }
        self.assertFalse(removed & set(qrs.DISPLAY_FIELDS))

    def test_paginates_and_sorts_best_lead_first(self):
        # Grade no longer leads the sort. It was a gate as well as a sort key,
        # so ordering by it only ever separated leads the gate had already
        # blessed; now a C lead with a real leak reaches the queue, and putting
        # the model's holistic guess ahead of the evidence would bury it.
        # `dial_rank` orders on the signals the demoted gates used to enforce —
        # here the leak severity and the demand proof behind it.
        strong = {"Visible Pain Score": 80, "Reviews": 200, "Rating": "4.9", "AI Confidence": "high"}
        middling = {"Visible Pain Score": 40, "Reviews": 60, "Rating": "4.6", "AI Confidence": "medium"}
        weak = {"Visible Pain Score": 5, "Reviews": 9, "Rating": "4.1", "AI Confidence": "low"}
        page1 = {
            "records": [
                # A grade with the weakest evidence: it must not lead any more.
                {"id": "recWeak", "fields": {**READY_FIELDS, "Business Name": "Weak Co", "Lead Priority": "A", "Score Total": 10, **weak}},
                {"id": "recMid", "fields": {**READY_FIELDS, "Business Name": "Mid Co", "Lead Priority": "A", "Score Total": 5, **middling}},
            ],
            "offset": "next",
        }
        page2 = {
            # C grade, strongest evidence — the lead the old sort put last.
            "records": [
                {"id": "recStrong", "fields": {**READY_FIELDS, "Business Name": "Strong Co", "Lead Priority": "C", "Score Total": 9, **strong}},
            ],
        }
        probe = _response(200, {"records": []})
        with patch("requests.get", side_effect=[probe, _response(200, page1), _response(200, page2)]) as mock_get:
            records = qrs.fetch_ready_records("app123", "Prospects", "tok", suppression=set())

        self.assertEqual([r["record_id"] for r in records], ["recStrong", "recMid", "recWeak"])
        # Missing display fields degrade to empty strings, not KeyErrors.
        self.assertEqual(records[0]["Opener"], "")
        self.assertEqual(mock_get.call_count, 3)

    def test_unknown_field_is_dropped_with_warning_instead_of_dead_digest(self):
        bad_probe = _response(422, text='{"error": {"message": "Unknown field name: \\"Safe Outreach Claims\\""}}')
        good_probe = _response(200, {"records": []})
        page = _response(200, {"records": []})
        captured = io.StringIO()
        with patch("requests.get", side_effect=[bad_probe, good_probe, page]) as mock_get, \
             patch("sys.stderr", captured):
            records = qrs.fetch_ready_records("app123", "Prospects", "tok", suppression=set())

        self.assertEqual(records, [])
        self.assertIn("Safe Outreach Claims", captured.getvalue())
        self.assertIn("Airtable schema mismatch", captured.getvalue())
        final_fields = [v for k, v in mock_get.call_args.kwargs.get("params", mock_get.call_args[1]["params"]) if k == "fields[]"]
        self.assertNotIn("Safe Outreach Claims", final_fields)

    def test_non_field_error_raises(self):
        with patch("requests.get", return_value=_response(401, text="unauthorized")):
            with self.assertRaises(RuntimeError):
                qrs.fetch_ready_records("app123", "Prospects", "tok", suppression=set())

    def test_missing_required_commercial_field_fails_closed(self):
        response = _response(
            422,
            text='{"error": {"message": "Unknown field name: \\"Contact Eligibility\\""}}',
        )
        with patch("requests.get", return_value=response) as mock_get:
            with self.assertRaises(RuntimeError):
                qrs.fetch_ready_records("app123", "Prospects", "tok", suppression=set())
        self.assertEqual(mock_get.call_count, 1)

    def test_missing_queue_status_field_fails_closed(self):
        response = _response(
            422,
            text='{"error": {"message": "Unknown field name: \\"Status\\""}}',
        )
        with patch("requests.get", return_value=response) as mock_get:
            with self.assertRaises(RuntimeError):
                qrs.fetch_ready_records("app123", "Prospects", "tok", suppression=set())
        self.assertEqual(mock_get.call_count, 1)

    def test_defensive_post_fetch_check_rejects_non_ready_rows(self):
        page = {
            "records": [
                {
                    "id": "recReady",
                    "fields": dict(READY_FIELDS),
                },
                {
                    "id": "recHold",
                    "fields": {
                        "Commercial Fit State": "minor-fix/nurture",
                        "Contact Eligibility": "contact-now",
                    },
                },
            ]
        }
        with patch("requests.get", side_effect=[_response(200, {"records": []}), _response(200, page)]):
            records = qrs.fetch_ready_records("app123", "Prospects", "tok", suppression=set())
        self.assertEqual([record["record_id"] for record in records], ["recReady"])

    def test_defensive_post_fetch_check_rejects_unsafe_status(self):
        page = {
            "records": [
                {"id": "recReady", "fields": dict(READY_FIELDS)},
                {"id": "recReview", "fields": {**READY_FIELDS, "Status": "Manual Review"}},
            ]
        }
        with patch("requests.get", side_effect=[_response(200, {"records": []}), _response(200, page)]):
            records = qrs.fetch_ready_records("app123", "Prospects", "tok", suppression=set())
        self.assertEqual([record["record_id"] for record in records], ["recReady"])


class TestQueryRespectsSuppression(unittest.TestCase):
    """The queue must not offer a rep a business that has opted out.

    The pipeline's three fail-closed gates (search, enrichment, scoring) stop a
    re-discovered opted-out lead before it reaches the base. What they cannot
    stop is a *duplicate* record already in the base under a different record id
    when the opt-out is logged — deduping is why `lead_machine/corpus_index.py`
    exists, so duplicates are a fact. mark_sent refuses such a record, but only after the
    rep has already been handed it and dialled it by hand.
    """

    def _page(self):
        return {
            "records": [
                {"id": "recClear", "fields": {**READY_FIELDS, "Phone": "216-555-0001"}},
                {"id": "recDupe", "fields": {**READY_FIELDS, "Phone": "(216) 555-0100"}},
            ]
        }

    def test_a_rediscovered_lead_with_the_same_phone_is_excluded(self):
        index = {"phone:2165550100"}
        with patch("requests.get", side_effect=[_response(200, {"records": []}), _response(200, self._page())]), \
             patch("sys.stderr", io.StringIO()) as err:
            records = qrs.fetch_ready_records("app123", "Prospects", "tok", suppression=index)
        self.assertEqual([r["record_id"] for r in records], ["recClear"])
        self.assertIn("recDupe", err.getvalue())
        self.assertIn("suppressed", err.getvalue())

    def test_the_opted_out_record_itself_is_excluded_by_its_record_id(self):
        # Identifiers are casefolded on both sides, which is what makes an
        # Airtable id written into the ledger by hand still match.
        index = {"record_id:recdupe"}
        with patch("requests.get", side_effect=[_response(200, {"records": []}), _response(200, self._page())]), \
             patch("sys.stderr", io.StringIO()):
            records = qrs.fetch_ready_records("app123", "Prospects", "tok", suppression=index)
        self.assertEqual([r["record_id"] for r in records], ["recClear"])

    def test_main_loads_the_configured_index_and_fails_closed_without_one(self):
        env = {"AIRTABLE_BASE_ID": "app", "AIRTABLE_PROSPECTS_TABLE": "Prospects", "AIRTABLE_PAT": "tok"}
        with patch.object(qrs, "load_dotenv", lambda: False), \
             patch.dict(os.environ, env, clear=True), \
             patch("requests.get") as mock_get, \
             patch("sys.argv", ["query_ready_to_send.py"]), \
             patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(qrs.main(), 1)
        self.assertFalse(mock_get.called)
        self.assertIn("suppression source", err.getvalue())

    def test_main_passes_the_configured_index_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "outcomes.jsonl"
            ledger.write_text(
                json.dumps({"event": "opt_out", "phone": "(216) 555-0100"}) + "\n", encoding="utf-8"
            )
            env = {
                "AIRTABLE_BASE_ID": "app",
                "AIRTABLE_PROSPECTS_TABLE": "Prospects",
                "AIRTABLE_PAT": "tok",
                "SUPPRESSION_SOURCE": str(ledger),
            }
            with patch.object(qrs, "load_dotenv", lambda: False), \
                 patch.dict(os.environ, env, clear=True), \
                 patch("requests.get", side_effect=[_response(200, {"records": []}), _response(200, self._page())]), \
                 patch("sys.argv", ["query_ready_to_send.py"]), \
                 patch("sys.stderr", io.StringIO()), \
                 patch("sys.stdout", io.StringIO()) as out:
                self.assertEqual(qrs.main(), 0)
            self.assertEqual([row["record_id"] for row in json.loads(out.getvalue())], ["recClear"])


class TestQueryMain(unittest.TestCase):
    def test_missing_credentials_fails_before_network(self):
        with patch.object(qrs, "load_dotenv", lambda: False), \
             patch.dict(os.environ, {}, clear=True), \
             patch("requests.get") as mock_get, \
             patch("sys.argv", ["query_ready_to_send.py"]), \
             patch("sys.stderr", io.StringIO()):
            self.assertEqual(qrs.main(), 1)
        self.assertFalse(mock_get.called)


class TestMarkSent(unittest.TestCase):
    ENV = {
        "AIRTABLE_BASE_ID": "app123",
        "AIRTABLE_PROSPECTS_TABLE": "Prospects",
        "AIRTABLE_PAT": "tok",
    }

    def _suppression(self, directory: str) -> str:
        path = Path(directory) / "outcomes.jsonl"
        path.write_text("", encoding="utf-8")
        return str(path)

    def _apply(self, suppression: str, fields: dict, *, channel: str = "SMS", patch_fields: dict | None = None):
        session = MagicMock()
        session.get.return_value = _response(200, {"id": "rec42", "fields": fields})
        confirmed = patch_fields or {
            **fields,
            "Status": "Follow-Up",
            "Last Contacted": "2026-07-10",
            "Outreach Channel": channel,
            "Next Follow-Up": "2026-07-14",
        }
        session.patch.return_value = _response(200, {"id": "rec42", "fields": confirmed})
        receipt = mark_sent.apply_mark_sent(
            record_id="rec42",
            channel=channel,
            last_contacted="2026-07-10",
            base_id="app123",
            table="Prospects",
            token="tok",
            suppression_source=suppression,
            session=session,
        )
        return receipt, session

    def test_mark_sent_source_gate_tracks_the_queue_gate_and_the_vocabulary(self):
        # Three declarations describe the same Status field: the queue gate, this
        # casefolded copy in compliance, and mark_outcome's write list. The
        # Won/Lost split showed they can move independently — so a status that
        # query_ready_to_send hands Lou has to be one mark_sent will still accept,
        # or the morning queue offers a record the write-back then refuses.
        self.assertEqual(
            compliance.MARK_SENT_ALLOWED_SOURCE_STATUSES,
            {value.casefold() for value in queue_state.QUEUE_STATUS_CLAUSE.allowed},
        )
        for value in queue_state.QUEUE_STATUS_CLAUSE.allowed:
            with self.subTest(status=value):
                self.assertIn(value, queue_state.PROSPECT_STATUS_CHOICES)

    def test_direct_sms_requires_current_consent_and_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppression = self._suppression(tmp)
            with self.assertRaisesRegex(mark_sent.MarkSentError, "Consent Posture"):
                self._apply(suppression, {**READY_FIELDS, "Consent Posture": "unknown"})
            with self.assertRaisesRegex(mark_sent.MarkSentError, "Packet Eligible"):
                self._apply(suppression, {**READY_FIELDS, "Packet Eligible": "No"}, channel="Email")
            with self.assertRaisesRegex(mark_sent.MarkSentError, "Verdict must be send"):
                self._apply(suppression, {**READY_FIELDS, "Verdict": "watch"}, channel="Email")

    def test_unknown_outreach_channel_is_refused_before_any_airtable_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = MagicMock()
            with self.assertRaisesRegex(mark_sent.MarkSentError, "Outreach Channel"):
                mark_sent.apply_mark_sent(
                    record_id="rec42",
                    channel="TikTok DM",
                    last_contacted="2026-07-10",
                    base_id="app123",
                    table="Prospects",
                    token="tok",
                    suppression_source=self._suppression(tmp),
                    session=session,
                )
            session.get.assert_not_called()
            session.patch.assert_not_called()

    def test_only_new_may_transition_to_follow_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppression = self._suppression(tmp)
            for allowed in ("New", "  new  "):
                receipt, session = self._apply(suppression, {**READY_FIELDS, "Status": allowed}, channel="Email")
                self.assertEqual(receipt["result"], "applied")
                session.patch.assert_called_once()
            for refused in ("", "Mockup Needed", "Manual Review", "Sent", "Contacted", "Won", "Lost", "Unknown"):
                with self.subTest(status=refused):
                    session = MagicMock()
                    session.get.return_value = _response(
                        200, {"id": "rec42", "fields": {**READY_FIELDS, "Status": refused}}
                    )
                    with self.assertRaisesRegex(mark_sent.MarkSentError, "not eligible"):
                        mark_sent.apply_mark_sent(
                            record_id="rec42",
                            channel="Email",
                            last_contacted="2026-07-10",
                            base_id="app123",
                            table="Prospects",
                            token="tok",
                            suppression_source=suppression,
                            session=session,
                        )
                    session.patch.assert_not_called()

    def test_missing_malformed_and_matching_suppression_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(mark_sent.MarkSentError, "missing or unreadable"):
                self._apply(str(Path(tmp) / "missing.jsonl"), dict(READY_FIELDS))
            malformed = Path(tmp) / "malformed.jsonl"
            malformed.write_text("{bad\n", encoding="utf-8")
            with self.assertRaisesRegex(mark_sent.MarkSentError, "malformed JSON"):
                self._apply(str(malformed), dict(READY_FIELDS))
            opted_out = Path(tmp) / "opted-out.jsonl"
            opted_out.write_text(json.dumps({"event": "opt_out", "record_id": "rec42"}) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(mark_sent.MarkSentError, "suppressed or opted out"):
                self._apply(str(opted_out), dict(READY_FIELDS))

    def test_conflicting_follow_up_refused_and_exact_follow_up_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppression = self._suppression(tmp)
            with self.assertRaisesRegex(mark_sent.MarkSentError, "conflicting"):
                self._apply(
                    suppression,
                    {**READY_FIELDS, "Status": "Follow-Up", "Last Contacted": "2026-07-09", "Outreach Channel": "SMS"},
                )
            receipt, session = self._apply(
                suppression,
                {
                    **READY_FIELDS,
                    "Status": "Follow-Up",
                    "Last Contacted": "2026-07-10",
                    "Outreach Channel": "SMS",
                    "Next Follow-Up": "2026-07-14",
                },
            )
            self.assertEqual(receipt["result"], "already_applied")
            session.patch.assert_not_called()

    def test_patch_response_must_confirm_requested_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppression = self._suppression(tmp)
            with self.assertRaisesRegex(mark_sent.MarkSentError, "does not confirm"):
                self._apply(
                    suppression,
                    dict(READY_FIELDS),
                    patch_fields={**READY_FIELDS, "Status": "Follow-Up", "Last Contacted": "2026-07-09", "Outreach Channel": "SMS"},
                )

    def test_patches_status_channel_and_date_without_typecast(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppression = self._suppression(tmp)
            current = _response(200, {"id": "rec42", "fields": dict(READY_FIELDS)})
            applied = _response(
                200,
                {
                    "id": "rec42",
                    "fields": {
                        **READY_FIELDS,
                        "Status": "Follow-Up",
                        "Last Contacted": "2026-07-10",
                        "Outreach Channel": "SMS",
                        "Next Follow-Up": "2026-07-14",
                    },
                },
            )
            with patch.object(mark_sent, "load_dotenv", lambda: False), \
                 patch.dict(os.environ, self.ENV, clear=True), \
                 patch("requests.get", return_value=current), \
                 patch("requests.patch", return_value=applied) as mock_patch, \
                 patch("sys.argv", ["mark_sent.py", "--record-id", "rec42", "--channel", "SMS", "--date", "2026-07-10", "--suppression-source", suppression]), \
                 patch("sys.stdout", io.StringIO()):
                self.assertEqual(mark_sent.main(), 0)

        self.assertIn("/rec42", mock_patch.call_args[0][0])
        payload = mock_patch.call_args.kwargs["json"]
        # An Outreach Channel Airtable has never heard of must 422, not become
        # the select field's newest choice.
        self.assertNotIn("typecast", payload)
        self.assertEqual(
            payload["fields"],
            {
                "Status": "Follow-Up",
                "Last Contacted": "2026-07-10",
                "Next Follow-Up": "2026-07-14",
                "Outreach Channel": "SMS",
            },
        )

    def test_channel_omitted_when_not_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppression = self._suppression(tmp)
            with patch.object(mark_sent, "load_dotenv", lambda: False), \
                 patch.dict(os.environ, self.ENV, clear=True), \
                 patch("requests.get", return_value=_response(200, {"id": "rec42", "fields": dict(READY_FIELDS)})), \
                 patch("requests.patch", return_value=_response(200, {"id": "rec42", "fields": {**READY_FIELDS, "Status": "Follow-Up", "Last Contacted": "2026-07-10", "Next Follow-Up": "2026-07-14"}})) as mock_patch, \
                 patch("sys.argv", ["mark_sent.py", "--record-id", "rec42", "--date", "2026-07-10", "--suppression-source", suppression]), \
                 patch("sys.stdout", io.StringIO()):
                self.assertEqual(mark_sent.main(), 0)
        self.assertNotIn("Outreach Channel", mock_patch.call_args.kwargs["json"]["fields"])

    def test_api_error_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppression = self._suppression(tmp)
            with patch.object(mark_sent, "load_dotenv", lambda: False), \
                 patch.dict(os.environ, self.ENV, clear=True), \
                 patch("requests.get", return_value=_response(200, {"id": "rec42", "fields": dict(READY_FIELDS)})), \
                 patch("requests.patch", return_value=_response(422, text="INVALID_VALUE")), \
                 patch("sys.argv", ["mark_sent.py", "--record-id", "rec42", "--suppression-source", suppression]), \
                 patch("sys.stderr", io.StringIO()):
                self.assertEqual(mark_sent.main(), 1)

    def test_missing_credentials_fails_before_network(self):
        with patch.object(mark_sent, "load_dotenv", lambda: False), \
             patch.dict(os.environ, {}, clear=True), \
             patch("requests.patch") as mock_patch, \
             patch("sys.argv", ["mark_sent.py", "--record-id", "rec42", "--suppression-source", "/tmp/missing.jsonl"]), \
             patch("sys.stderr", io.StringIO()):
            self.assertEqual(mark_sent.main(), 1)
        self.assertFalse(mock_patch.called)

    def test_next_follow_up_defaults_to_four_days_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppression = self._suppression(tmp)
            with patch.dict(os.environ, {}, clear=True):
                receipt, session = self._apply(suppression, dict(READY_FIELDS), channel="Email")
        self.assertEqual(receipt["next_follow_up"], "2026-07-14")
        self.assertEqual(
            session.patch.call_args.kwargs["json"]["fields"]["Next Follow-Up"], "2026-07-14"
        )

    def test_next_follow_up_cadence_is_configurable(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppression = self._suppression(tmp)
            with patch.dict(os.environ, {"FOLLOW_UP_CADENCE_DAYS": "7"}, clear=True):
                receipt, _session = self._apply(
                    suppression,
                    dict(READY_FIELDS),
                    channel="Email",
                    patch_fields={
                        **READY_FIELDS,
                        "Status": "Follow-Up",
                        "Last Contacted": "2026-07-10",
                        "Outreach Channel": "Email",
                        "Next Follow-Up": "2026-07-17",
                    },
                )
        self.assertEqual(receipt["next_follow_up"], "2026-07-17")


if __name__ == "__main__":
    unittest.main()


class TestReadinessFlagsFromPacketTier(unittest.TestCase):
    """Packet Tier replaced Sendable + Packet Eligible on the Airtable surface.

    Rows read back from the base carry only the tier, so the readiness contract
    has to reconstruct the two booleans from it — and an explicit column, which
    a hand-edit in the base can still produce, must always win.
    """

    def test_tier_derives_both_booleans(self):
        cases = {
            "Sendable Packet": ("Yes", "Yes"),
            "Manual Review Packet": ("No", "Yes"),
            "Watchlist": ("No", "No"),
            "No Packet": ("No", "No"),
        }
        for tier, expected in cases.items():
            with self.subTest(tier=tier):
                self.assertEqual(sendability.readiness_flags({"Packet Tier": tier}), expected)

    def test_a_verdict_only_row_is_what_the_base_returns_now(self):
        """The reconstruct-from-tier path looks dead and is not. Do not remove it.

        After step 4 of the verdict cutover a base row carries `Verdict` and
        none of `Packet Tier`, `Sendable` or `Packet Eligible` — those columns
        were deleted. The verdict maps back to a tier and the tier derives the
        booleans, so this branch is the *only* thing that resolves a row read
        back from Airtable. Delete it as "dead now the tier column is gone" and
        every base row silently becomes `("", "")` — not sendable — and
        `is_contact_ready` starts refusing the leads the queue is built on.
        """
        cases = {
            "send": ("Yes", "Yes"),
            "review": ("No", "Yes"),
            "watch": ("No", "No"),
            "reject": ("No", "No"),
        }
        for verdict, expected in cases.items():
            with self.subTest(verdict=verdict):
                self.assertEqual(
                    sendability.readiness_flags({"Verdict": verdict}), expected
                )

    def test_explicit_columns_win_over_the_tier(self):
        row = {"Packet Tier": "Sendable Packet", "Sendable": "No"}
        self.assertEqual(sendability.readiness_flags(row), ("No", "Yes"))
        self.assertFalse(sendability.is_contact_ready({**READY_FIELDS, "Sendable": "No"}))

    def test_no_tier_and_no_columns_is_not_ready(self):
        self.assertEqual(sendability.readiness_flags({}), ("", ""))
        self.assertFalse(sendability.is_contact_ready({}))

    def test_sendable_tier_alone_satisfies_the_readiness_contract(self):
        # The commercial-fit cross-check that used to be asserted here is gone
        # with the tribunal — a sendable row now routinely carries `unverified`
        # or `hold-commercial-case`, so keeping it would reject the queue's own
        # output. What the contract still refuses is a row whose own readiness
        # columns disagree with its tier, which `test_explicit_columns_win_over_
        # the_tier` covers.
        self.assertTrue(sendability.is_contact_ready(READY_FIELDS))
        self.assertTrue(
            sendability.is_contact_ready(
                {**READY_FIELDS, "Commercial Fit State": "unverified"}
            )
        )
