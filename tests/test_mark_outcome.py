"""Offline request/response tests for mark_outcome.py.

Mirrors test_send_queue_scripts.py's mocked-requests pattern: no test
touches the network or a real base.
"""
import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch

import mark_outcome


def _response(status_code=200, payload=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text or (json.dumps(payload) if payload is not None else "")
    resp.json.return_value = payload if payload is not None else {}
    return resp


class TestApplyMarkOutcome(unittest.TestCase):
    ARGS = dict(
        record_id="rec42",
        notes="they called back",
        occurred_at="2026-07-27T12:00:00+00:00",
        base_id="app123",
        prospects_table="Prospects",
        activities_table="Sales Activities",
        deals_table="Deals",
        token="tok",
    )

    def _session(
        self,
        *,
        current_status="Sent",
        patch_status=None,
        patch_follow_up="2026-07-31",
        post_payloads=None,
    ):
        session = MagicMock()
        session.get.return_value = _response(
            200, {"id": "rec42", "fields": {"Status": current_status, "Business Name": "Acme Epoxy"}}
        )
        patched = {"Status": patch_status}
        # Airtable omits a date field entirely once it is cleared.
        if patch_follow_up is not None:
            patched["Next Follow-Up"] = patch_follow_up
        session.patch.return_value = _response(200, {"id": "rec42", "fields": patched})
        session.post.side_effect = post_payloads or [
            _response(200, {"records": [{"id": "recAct1"}]}),
            _response(200, {"records": [{"id": "recDeal1"}]}),
        ]
        return session

    def test_unknown_event_rejected_before_any_network_call(self):
        session = self._session()
        with self.assertRaisesRegex(mark_outcome.MarkOutcomeError, "unknown event"):
            mark_outcome.apply_mark_outcome(event="not_a_real_event", session=session, **self.ARGS)
        session.get.assert_not_called()

    def test_source_status_gate(self):
        for allowed in ("Sent", "Follow-Up", "  sent  "):
            with self.subTest(status=allowed):
                session = self._session(current_status=allowed, patch_status="Follow-Up")
                receipt = mark_outcome.apply_mark_outcome(event="replied", session=session, **self.ARGS)
                self.assertEqual(receipt["result"], "applied")
        for refused in ("New", "Mockup Needed", "Manual Review", "Closed", ""):
            with self.subTest(status=refused):
                session = self._session(current_status=refused)
                with self.assertRaisesRegex(mark_outcome.MarkOutcomeError, "not eligible"):
                    mark_outcome.apply_mark_outcome(event="replied", session=session, **self.ARGS)
                session.patch.assert_not_called()

    def test_replied_patches_follow_up_and_logs_activity(self):
        session = self._session(patch_status="Follow-Up")
        receipt = mark_outcome.apply_mark_outcome(event="replied", session=session, **self.ARGS)

        self.assertEqual(receipt["status"], "Follow-Up")
        self.assertEqual(receipt["activity_record_id"], "recAct1")
        self.assertNotIn("deal_record_id", receipt)

        patch_fields = session.patch.call_args.kwargs["json"]["fields"]
        self.assertEqual(patch_fields, {"Status": "Follow-Up", "Next Follow-Up": "2026-07-31"})
        self.assertEqual(receipt["next_follow_up"], "2026-07-31")

        activity_payload = session.post.call_args_list[0].kwargs["json"]
        self.assertTrue(activity_payload["typecast"])
        activity_fields = activity_payload["records"][0]["fields"]
        self.assertEqual(activity_fields["Activity Type"], "Reply")
        self.assertEqual(activity_fields["Outcome"], "Replied")
        self.assertEqual(activity_fields["Linked Prospect"], ["rec42"])
        self.assertEqual(activity_fields["Activity Name"], "Reply — Acme Epoxy")

    def test_meeting_booked_stays_follow_up(self):
        session = self._session(patch_status="Follow-Up")
        receipt = mark_outcome.apply_mark_outcome(event="meeting_booked", session=session, **self.ARGS)
        self.assertEqual(receipt["status"], "Follow-Up")
        activity_fields = session.post.call_args_list[0].kwargs["json"]["records"][0]["fields"]
        self.assertEqual(activity_fields["Activity Type"], "Call")
        self.assertEqual(activity_fields["Outcome"], "Meeting Booked")

    def test_closed_lost_patches_closed_and_creates_no_deal(self):
        session = self._session(patch_status="Closed", patch_follow_up=None)
        receipt = mark_outcome.apply_mark_outcome(event="closed_lost", session=session, **self.ARGS)
        self.assertEqual(receipt["status"], "Closed")
        self.assertNotIn("deal_record_id", receipt)
        self.assertEqual(session.post.call_count, 1)
        activity_fields = session.post.call_args_list[0].kwargs["json"]["records"][0]["fields"]
        self.assertEqual(activity_fields["Outcome"], "Lost")

    def test_closing_clears_next_follow_up_and_open_events_rearm_it(self):
        for event in ("closed_won", "closed_lost"):
            with self.subTest(event=event):
                session = self._session(patch_status="Closed", patch_follow_up=None)
                receipt = mark_outcome.apply_mark_outcome(event=event, session=session, **self.ARGS)
                self.assertIsNone(session.patch.call_args.kwargs["json"]["fields"]["Next Follow-Up"])
                self.assertIsNone(receipt["next_follow_up"])

        for event in ("replied", "meeting_booked"):
            with self.subTest(event=event):
                session = self._session(patch_status="Follow-Up")
                receipt = mark_outcome.apply_mark_outcome(event=event, session=session, **self.ARGS)
                # occurred_at is 2026-07-27 + the 4-day default cadence step.
                self.assertEqual(receipt["next_follow_up"], "2026-07-31")

    def test_next_follow_up_honours_the_shared_cadence_setting(self):
        session = self._session(patch_status="Follow-Up", patch_follow_up="2026-08-03")
        with patch.dict(os.environ, {"FOLLOW_UP_CADENCE_DAYS": "7"}, clear=True):
            receipt = mark_outcome.apply_mark_outcome(event="replied", session=session, **self.ARGS)
        self.assertEqual(receipt["next_follow_up"], "2026-08-03")

    def test_patch_not_confirming_next_follow_up_raises(self):
        session = self._session(patch_status="Follow-Up", patch_follow_up="2026-09-09")
        with self.assertRaisesRegex(mark_outcome.MarkOutcomeError, "Next Follow-Up"):
            mark_outcome.apply_mark_outcome(event="replied", session=session, **self.ARGS)

    def test_prospect_patch_and_deal_create_never_typecast(self):
        # Only the Sales Activities create may mint a select choice; a bad
        # Status or Deal Status has to 422 instead of drifting the schema.
        session = self._session(patch_status="Closed", patch_follow_up=None)
        mark_outcome.apply_mark_outcome(event="closed_won", session=session, **self.ARGS)
        self.assertNotIn("typecast", session.patch.call_args.kwargs["json"])
        self.assertNotIn("typecast", session.post.call_args_list[1].kwargs["json"])

    def test_written_select_vocabulary_is_closed(self):
        # typecast on the activity create means these strings become live
        # Airtable choices. Changing one is a schema change, not a rename.
        self.assertEqual(set(mark_outcome.TARGET_STATUS.values()), {"Follow-Up", "Closed"})
        self.assertEqual(set(mark_outcome.ACTIVITY_TYPE.values()), {"Reply", "Call", "Close"})
        self.assertEqual(
            set(mark_outcome.ACTIVITY_OUTCOME.values()),
            {"Replied", "Meeting Booked", "Won", "Lost"},
        )
        self.assertEqual(mark_outcome.CLOSED_WON_DEAL_STATUS, "Deposit Pending")

    def test_closed_won_creates_deal_with_optional_fields(self):
        session = self._session(patch_status="Closed", patch_follow_up=None)
        receipt = mark_outcome.apply_mark_outcome(
            event="closed_won",
            deal_package="Growth",
            deal_setup_price=3500.0,
            deal_retainer_price=500.0,
            session=session,
            **self.ARGS,
        )
        self.assertEqual(receipt["deal_record_id"], "recDeal1")
        self.assertEqual(session.post.call_count, 2)
        deal_payload = session.post.call_args_list[1].kwargs["json"]
        deal_fields = deal_payload["records"][0]["fields"]
        self.assertEqual(deal_fields["Deal Status"], "Deposit Pending")
        self.assertEqual(deal_fields["Prospect"], ["rec42"])
        self.assertEqual(deal_fields["Package"], "Growth")
        self.assertEqual(deal_fields["Setup Price"], 3500.0)
        self.assertEqual(deal_fields["Retainer Price"], 500.0)

    def test_closed_won_omits_optional_deal_fields_but_never_the_primary(self):
        session = self._session(patch_status="Closed", patch_follow_up=None)
        mark_outcome.apply_mark_outcome(event="closed_won", session=session, **self.ARGS)
        deal_fields = session.post.call_args_list[1].kwargs["json"]["records"][0]["fields"]
        # Package is the Deals primary field — an empty one renders as a blank
        # chip on the prospect that just closed.
        self.assertEqual(deal_fields["Package"], "Acme Epoxy")
        self.assertNotIn("Setup Price", deal_fields)
        self.assertNotIn("Retainer Price", deal_fields)

    def test_patch_not_confirming_status_raises(self):
        session = self._session(patch_status="Follow-Up")
        session.patch.return_value = _response(200, {"id": "rec42", "fields": {"Status": "New"}})
        with self.assertRaisesRegex(mark_outcome.MarkOutcomeError, "does not confirm"):
            mark_outcome.apply_mark_outcome(event="replied", session=session, **self.ARGS)

    def test_activity_post_error_raises(self):
        session = self._session(patch_status="Follow-Up")
        session.post.side_effect = [_response(422, text="INVALID_VALUE")]
        with self.assertRaisesRegex(mark_outcome.MarkOutcomeError, "POST activity failed"):
            mark_outcome.apply_mark_outcome(event="replied", session=session, **self.ARGS)


class TestMain(unittest.TestCase):
    ENV = {
        "AIRTABLE_BASE_ID": "app123",
        "AIRTABLE_PROSPECTS_TABLE": "Prospects",
        "AIRTABLE_PAT": "tok",
    }

    def test_missing_credentials_fails_before_network(self):
        with patch.object(mark_outcome, "load_dotenv", lambda: False), \
             patch.dict(os.environ, {}, clear=True), \
             patch("requests.get") as mock_get, \
             patch("sys.argv", ["mark_outcome.py", "--record-id", "rec42", "--event", "replied"]), \
             patch("sys.stderr", io.StringIO()):
            self.assertEqual(mark_outcome.main(), 1)
        self.assertFalse(mock_get.called)

    def test_bad_date_rejected(self):
        with patch.object(mark_outcome, "load_dotenv", lambda: False), \
             patch.dict(os.environ, self.ENV, clear=True), \
             patch(
                 "sys.argv",
                 ["mark_outcome.py", "--record-id", "rec42", "--event", "replied", "--date", "not-a-date"],
             ), \
             patch("sys.stderr", io.StringIO()):
            self.assertEqual(mark_outcome.main(), 1)

    def test_happy_path_replied(self):
        get_resp = _response(200, {"id": "rec42", "fields": {"Status": "Sent", "Business Name": "Acme"}})
        patch_resp = _response(
            200, {"id": "rec42", "fields": {"Status": "Follow-Up", "Next Follow-Up": "2026-07-31"}}
        )
        post_resp = _response(200, {"records": [{"id": "recAct1"}]})
        with patch.object(mark_outcome, "load_dotenv", lambda: False), \
             patch.dict(os.environ, self.ENV, clear=True), \
             patch("requests.get", return_value=get_resp), \
             patch("requests.patch", return_value=patch_resp), \
             patch("requests.post", return_value=post_resp), \
             patch(
                 "sys.argv",
                 ["mark_outcome.py", "--record-id", "rec42", "--event", "replied", "--date", "2026-07-27"],
             ), \
             patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(mark_outcome.main(), 0)
        receipt = json.loads(out.getvalue())
        self.assertEqual(receipt["result"], "applied")
        self.assertEqual(receipt["status"], "Follow-Up")


if __name__ == "__main__":
    unittest.main()
