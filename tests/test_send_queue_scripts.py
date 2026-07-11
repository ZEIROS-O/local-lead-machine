"""Offline request/response tests for the send-queue runtime scripts.

Covers query_ready_to_send.py (read-only Airtable query) and mark_sent.py
(status write-back). All Airtable traffic is mocked; no test touches the
network or a real base.
"""
import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch

import mark_sent
import query_ready_to_send as qrs


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
            "AND({Sendable}='Yes',{Packet Eligible}='Yes',"
            "NOT({Status}='Sent'),NOT({Status}='Closed'))",
        )

    def test_vertical_clause_added_and_quotes_escaped(self):
        formula = qrs.build_formula("Bob's Epoxy")
        self.assertIn(",{Vertical}='Bob\\'s Epoxy')", formula)


class TestFetchReadyRecords(unittest.TestCase):
    def test_paginates_and_sorts_by_priority_then_score(self):
        page1 = {
            "records": [
                {"id": "recB", "fields": {"Business Name": "B Co", "Lead Priority": "B", "Score Total": 10}},
                {"id": "recA5", "fields": {"Business Name": "A5 Co", "Lead Priority": "A", "Score Total": 5}},
            ],
            "offset": "next",
        }
        page2 = {
            "records": [
                {"id": "recA9", "fields": {"Business Name": "A9 Co", "Lead Priority": "A", "Score Total": 9}},
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

    def test_patches_status_channel_and_date_with_typecast(self):
        with patch.object(mark_sent, "load_dotenv", lambda: False), \
             patch.dict(os.environ, self.ENV, clear=True), \
             patch("requests.patch", return_value=_response(200, {})) as mock_patch, \
             patch("sys.argv", ["mark_sent.py", "--record-id", "rec42", "--channel", "SMS", "--date", "2026-07-10"]), \
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
        with patch.object(mark_sent, "load_dotenv", lambda: False), \
             patch.dict(os.environ, self.ENV, clear=True), \
             patch("requests.patch", return_value=_response(200, {})) as mock_patch, \
             patch("sys.argv", ["mark_sent.py", "--record-id", "rec42", "--date", "2026-07-10"]), \
             patch("sys.stdout", io.StringIO()):
            self.assertEqual(mark_sent.main(), 0)
        self.assertNotIn("Outreach Channel", mock_patch.call_args.kwargs["json"]["fields"])

    def test_api_error_returns_nonzero(self):
        with patch.object(mark_sent, "load_dotenv", lambda: False), \
             patch.dict(os.environ, self.ENV, clear=True), \
             patch("requests.patch", return_value=_response(422, text="INVALID_VALUE")), \
             patch("sys.argv", ["mark_sent.py", "--record-id", "rec42"]), \
             patch("sys.stderr", io.StringIO()):
            self.assertEqual(mark_sent.main(), 1)

    def test_missing_credentials_fails_before_network(self):
        with patch.object(mark_sent, "load_dotenv", lambda: False), \
             patch.dict(os.environ, {}, clear=True), \
             patch("requests.patch") as mock_patch, \
             patch("sys.argv", ["mark_sent.py", "--record-id", "rec42"]), \
             patch("sys.stderr", io.StringIO()):
            self.assertEqual(mark_sent.main(), 1)
        self.assertFalse(mock_patch.called)


if __name__ == "__main__":
    unittest.main()
