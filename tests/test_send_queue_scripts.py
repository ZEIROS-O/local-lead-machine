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
from lead_machine import sendability


READY_FIELDS = {
    "Packet Tier": "Sendable Packet",
    "Commercial Fit State": "commercially-sendable",
    "Contact Eligibility": "contact-now",
    "Status": "New",
    "Consent Posture": "express written consent",
}


def _response(status_code=200, payload=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text or (json.dumps(payload) if payload is not None else "")
    resp.json.return_value = payload if payload is not None else {}
    return resp


class TestBuildFormula(unittest.TestCase):
    def test_base_formula_without_vertical(self):
        formula = qrs.build_formula("")
        self.assertEqual(
            formula,
            "AND({Packet Tier}='Sendable Packet',"
            "{Commercial Fit State}='commercially-sendable',{Contact Eligibility}='contact-now',"
            "OR({Status}='New',{Status}='Mockup Needed'))",
        )

    def test_vertical_clause_added_and_quotes_escaped(self):
        formula = qrs.build_formula("Bob's Epoxy")
        self.assertIn(",{Vertical}='Bob\\'s Epoxy')", formula)


class TestFetchReadyRecords(unittest.TestCase):
    def test_paginates_and_sorts_by_priority_then_score(self):
        page1 = {
            "records": [
                {"id": "recB", "fields": {**READY_FIELDS, "Business Name": "B Co", "Lead Priority": "B", "Score Total": 10}},
                {"id": "recA5", "fields": {**READY_FIELDS, "Business Name": "A5 Co", "Lead Priority": "A", "Score Total": 5}},
            ],
            "offset": "next",
        }
        page2 = {
            "records": [
                {"id": "recA9", "fields": {**READY_FIELDS, "Business Name": "A9 Co", "Lead Priority": "A", "Score Total": 9}},
            ],
        }
        probe = _response(200, {"records": []})
        with patch("requests.get", side_effect=[probe, _response(200, page1), _response(200, page2)]) as mock_get:
            records = qrs.fetch_ready_records("app123", "Prospects", "tok")

        self.assertEqual([r["record_id"] for r in records], ["recA9", "recA5", "recB"])
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
            records = qrs.fetch_ready_records("app123", "Prospects", "tok")

        self.assertEqual(records, [])
        self.assertIn("Safe Outreach Claims", captured.getvalue())
        final_fields = [v for k, v in mock_get.call_args.kwargs.get("params", mock_get.call_args[1]["params"]) if k == "fields[]"]
        self.assertNotIn("Safe Outreach Claims", final_fields)

    def test_non_field_error_raises(self):
        with patch("requests.get", return_value=_response(401, text="unauthorized")):
            with self.assertRaises(RuntimeError):
                qrs.fetch_ready_records("app123", "Prospects", "tok")

    def test_missing_required_commercial_field_fails_closed(self):
        response = _response(
            422,
            text='{"error": {"message": "Unknown field name: \\"Contact Eligibility\\""}}',
        )
        with patch("requests.get", return_value=response) as mock_get:
            with self.assertRaises(RuntimeError):
                qrs.fetch_ready_records("app123", "Prospects", "tok")
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
            records = qrs.fetch_ready_records("app123", "Prospects", "tok")
        self.assertEqual([record["record_id"] for record in records], ["recReady"])

    def test_defensive_post_fetch_check_rejects_unsafe_status(self):
        page = {
            "records": [
                {"id": "recReady", "fields": dict(READY_FIELDS)},
                {"id": "recReview", "fields": {**READY_FIELDS, "Status": "Manual Review"}},
            ]
        }
        with patch("requests.get", side_effect=[_response(200, {"records": []}), _response(200, page)]):
            records = qrs.fetch_ready_records("app123", "Prospects", "tok")
        self.assertEqual([record["record_id"] for record in records], ["recReady"])


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
        confirmed = patch_fields or {**fields, "Status": "Sent", "Last Contacted": "2026-07-10", "Outreach Channel": channel}
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

    def test_direct_sms_requires_current_consent_and_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppression = self._suppression(tmp)
            with self.assertRaisesRegex(mark_sent.MarkSentError, "Consent Posture"):
                self._apply(suppression, {**READY_FIELDS, "Consent Posture": "unknown"})
            with self.assertRaisesRegex(mark_sent.MarkSentError, "Packet Eligible"):
                self._apply(suppression, {**READY_FIELDS, "Packet Eligible": "No"}, channel="Email")
            with self.assertRaisesRegex(mark_sent.MarkSentError, "Sendable"):
                self._apply(suppression, {**READY_FIELDS, "Packet Tier": "Watchlist"}, channel="Email")

    def test_only_new_and_mockup_needed_may_transition_to_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppression = self._suppression(tmp)
            for allowed in ("New", "Mockup Needed", "  new  "):
                receipt, session = self._apply(suppression, {**READY_FIELDS, "Status": allowed}, channel="Email")
                self.assertEqual(receipt["result"], "applied")
                session.patch.assert_called_once()
            for refused in ("", "Manual Review", "Follow-Up", "Contacted", "Closed", "Unknown"):
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

    def test_conflicting_sent_refused_and_exact_sent_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppression = self._suppression(tmp)
            with self.assertRaisesRegex(mark_sent.MarkSentError, "conflicting"):
                self._apply(
                    suppression,
                    {**READY_FIELDS, "Status": "Sent", "Last Contacted": "2026-07-09", "Outreach Channel": "SMS"},
                )
            receipt, session = self._apply(
                suppression,
                {**READY_FIELDS, "Status": "Sent", "Last Contacted": "2026-07-10", "Outreach Channel": "SMS"},
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
                    patch_fields={**READY_FIELDS, "Status": "Sent", "Last Contacted": "2026-07-09", "Outreach Channel": "SMS"},
                )

    def test_patches_status_channel_and_date_with_typecast(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppression = self._suppression(tmp)
            current = _response(200, {"id": "rec42", "fields": dict(READY_FIELDS)})
            applied = _response(200, {"id": "rec42", "fields": {**READY_FIELDS, "Status": "Sent", "Last Contacted": "2026-07-10", "Outreach Channel": "SMS"}})
            with patch.object(mark_sent, "load_dotenv", lambda: False), \
                 patch.dict(os.environ, self.ENV, clear=True), \
                 patch("requests.get", return_value=current), \
                 patch("requests.patch", return_value=applied) as mock_patch, \
                 patch("sys.argv", ["mark_sent.py", "--record-id", "rec42", "--channel", "SMS", "--date", "2026-07-10", "--suppression-source", suppression]), \
                 patch("sys.stdout", io.StringIO()):
                self.assertEqual(mark_sent.main(), 0)

        self.assertIn("/rec42", mock_patch.call_args[0][0])
        payload = mock_patch.call_args.kwargs["json"]
        self.assertTrue(payload["typecast"])
        self.assertEqual(
            payload["fields"],
            {"Status": "Sent", "Last Contacted": "2026-07-10", "Outreach Channel": "SMS"},
        )

    def test_channel_omitted_when_not_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            suppression = self._suppression(tmp)
            with patch.object(mark_sent, "load_dotenv", lambda: False), \
                 patch.dict(os.environ, self.ENV, clear=True), \
                 patch("requests.get", return_value=_response(200, {"id": "rec42", "fields": dict(READY_FIELDS)})), \
                 patch("requests.patch", return_value=_response(200, {"id": "rec42", "fields": {**READY_FIELDS, "Status": "Sent", "Last Contacted": "2026-07-10"}})) as mock_patch, \
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

    def test_explicit_columns_win_over_the_tier(self):
        row = {"Packet Tier": "Sendable Packet", "Sendable": "No"}
        self.assertEqual(sendability.readiness_flags(row), ("No", "Yes"))
        self.assertFalse(sendability.is_contact_ready({**READY_FIELDS, "Sendable": "No"}))

    def test_no_tier_and_no_columns_is_not_ready(self):
        self.assertEqual(sendability.readiness_flags({}), ("", ""))
        self.assertFalse(sendability.is_contact_ready({}))

    def test_sendable_tier_alone_satisfies_the_readiness_contract(self):
        # The other three clauses of the contract are cross-checks, so a row
        # whose tier says Sendable Packet still has to carry them explicitly.
        self.assertTrue(sendability.is_contact_ready(READY_FIELDS))
        self.assertFalse(
            sendability.is_contact_ready(
                {**READY_FIELDS, "Commercial Fit State": "unverified"}
            )
        )
