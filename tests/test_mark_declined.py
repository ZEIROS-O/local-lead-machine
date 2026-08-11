"""Offline tests for the pre-dial decline write path.

All Airtable traffic is mocked; no test touches the network or a real base.

The property that matters most here is the write order. The ledger row is the
new fact and the Airtable status change is a convenience, so a half-applied
decline must leave the lead visible and re-decidable rather than silently gone.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import mark_declined
from lead_machine import dial_decisions


def _response(status_code=200, payload=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text or (json.dumps(payload) if payload is not None else "")
    resp.json.return_value = payload if payload is not None else {}
    return resp


def _session(*, current_status="New", patch_response=None, patch_status=200):
    session = MagicMock()
    session.get.return_value = _response(
        payload={
            "id": "rec1",
            "fields": {
                "Status": current_status,
                "Business Name": "Acme Epoxy",
                "Vertical": "epoxy",
            },
        }
    )
    session.patch.return_value = _response(
        status_code=patch_status,
        payload=patch_response
        if patch_response is not None
        else {"id": "rec1", "fields": {"Status": "Not Fit"}},
    )
    return session


class DecliningALead(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "decisions.jsonl"

    def _apply(self, session, **overrides):
        kwargs = dict(
            record_id="rec1",
            reason="no owner name to ask for",
            decisions_source=str(self.ledger),
            base_id="base",
            table="Prospects",
            token="tok",
            session=session,
        )
        kwargs.update(overrides)
        return mark_declined.apply_mark_declined(**kwargs)

    def test_a_decline_records_the_reason_and_retires_the_lead(self):
        session = _session()
        receipt = self._apply(session)

        self.assertEqual(receipt["result"], "applied")
        self.assertEqual(
            session.patch.call_args.kwargs["json"], {"fields": {"Status": "Not Fit"}}
        )
        rows = dial_decisions.load_decisions(self.ledger)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision"], dial_decisions.DECISION_DECLINE)
        self.assertEqual(rows[0]["reason"], "no owner name to ask for")
        self.assertEqual(rows[0]["business_name"], "Acme Epoxy")

    def test_the_rank_served_is_carried_into_the_ledger(self):
        # Without it nothing can check whether a high rank predicts approval,
        # which is the only falsifier the invented weights have.
        self._apply(_session(), rank=61.5)
        self.assertEqual(dial_decisions.load_decisions(self.ledger)[0]["rank"], 61.5)

    def test_a_reasonless_decline_is_refused_before_any_airtable_call(self):
        session = _session()
        with self.assertRaises(mark_declined.MarkDeclinedError):
            self._apply(session, reason="   ")
        session.get.assert_not_called()
        session.patch.assert_not_called()

    def test_an_unconfigured_ledger_is_refused_rather_than_writing_only_airtable(self):
        # Retiring the lead with no ledger row is the exact state this command
        # exists to end: gone from the queue, with no record of why.
        session = _session()
        with self.assertRaises(mark_declined.MarkDeclinedError):
            self._apply(session, decisions_source="")
        session.patch.assert_not_called()

    def test_an_already_worked_lead_cannot_be_declined(self):
        # A decline is a judgement made before the call. After a contact is
        # logged the lead was accepted, and what follows is an outcome.
        session = _session(current_status="Follow-Up")
        with self.assertRaises(mark_declined.MarkDeclinedError):
            self._apply(session)
        session.patch.assert_not_called()
        self.assertEqual(dial_decisions.load_decisions(self.ledger), [])

    def test_declining_twice_is_idempotent_and_does_not_double_count(self):
        session = _session(current_status="Not Fit")
        receipt = self._apply(session)
        self.assertEqual(receipt["result"], "already_applied")
        session.patch.assert_not_called()
        self.assertEqual(dial_decisions.load_decisions(self.ledger), [])

    def test_a_failed_status_write_still_leaves_the_decision_recorded(self):
        # Ledger first, on purpose. The lead comes back tomorrow and can be
        # decided again; what must not happen is a lead vanishing untraced.
        session = _session(patch_status=500, patch_response={})
        with self.assertRaises(mark_declined.MarkDeclinedError):
            self._apply(session)
        self.assertEqual(len(dial_decisions.load_decisions(self.ledger)), 1)

    def test_an_unconfirmed_status_write_is_an_error(self):
        session = _session(patch_response={"id": "rec1", "fields": {"Status": "New"}})
        with self.assertRaises(mark_declined.MarkDeclinedError):
            self._apply(session)


if __name__ == "__main__":
    unittest.main()
