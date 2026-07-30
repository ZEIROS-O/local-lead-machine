"""Offline request/response tests for mark_outcome.py.

Mirrors test_send_queue_scripts.py's mocked-requests pattern: no test
touches the network or a real base.

Also carries the repo-wide "no Airtable write may set typecast" invariant
(TestNoAirtableWritePathTypecasts). It lives here because mark_outcome.py held
the last typecast in the codebase, and the rule is not per-script: a typecast
anywhere mints select choices in the same shared base.
"""
import ast
import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import unquote

import mark_outcome
from lead_machine import queue_state


REPO_ROOT = Path(__file__).resolve().parent.parent


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
        for refused in ("New", "Mockup Needed", "Manual Review", "Won", "Lost", ""):
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

        activity_fields = session.post.call_args_list[0].kwargs["json"]["records"][0]["fields"]
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

    def test_closed_lost_patches_lost_and_creates_no_deal(self):
        session = self._session(patch_status="Lost", patch_follow_up=None)
        receipt = mark_outcome.apply_mark_outcome(event="closed_lost", session=session, **self.ARGS)
        self.assertEqual(receipt["status"], "Lost")
        self.assertNotIn("deal_record_id", receipt)
        self.assertEqual(session.post.call_count, 1)
        activity_fields = session.post.call_args_list[0].kwargs["json"]["records"][0]["fields"]
        self.assertEqual(activity_fields["Outcome"], "Lost")

    def test_the_two_closing_events_land_on_distinguishable_statuses(self):
        # The whole reason for the split. A shared `Closed` made a win and a
        # dead lead the same cell, so nothing downstream — a rollup, a view, a
        # conversion rate — could ever tell them apart after the fact.
        applied = {}
        for event in sorted(mark_outcome.CLOSING_EVENTS):
            with self.subTest(event=event):
                target = mark_outcome.TARGET_STATUS[event]
                session = self._session(patch_status=target, patch_follow_up=None)
                receipt = mark_outcome.apply_mark_outcome(event=event, session=session, **self.ARGS)
                self.assertEqual(session.patch.call_args.kwargs["json"]["fields"]["Status"], target)
                applied[event] = receipt["status"]
        self.assertEqual(applied, {"closed_won": "Won", "closed_lost": "Lost"})

    def test_closing_clears_next_follow_up_and_open_events_rearm_it(self):
        for event in ("closed_won", "closed_lost"):
            with self.subTest(event=event):
                session = self._session(
                    patch_status=mark_outcome.TARGET_STATUS[event], patch_follow_up=None
                )
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

    def test_no_write_path_sets_typecast(self):
        # Reads the bodies off the session rather than naming today's three
        # writes, so a fourth one added later is covered without editing this.
        for event in sorted(mark_outcome.OUTCOME_EVENTS):
            with self.subTest(event=event):
                closing = event in mark_outcome.CLOSING_EVENTS
                session = self._session(
                    patch_status=mark_outcome.TARGET_STATUS[event],
                    patch_follow_up=None if closing else "2026-07-31",
                )
                mark_outcome.apply_mark_outcome(event=event, session=session, **self.ARGS)
                bodies = [
                    call.kwargs["json"]
                    for call in [*session.patch.call_args_list, *session.post.call_args_list]
                ]
                # Guards against the assertion loop passing on an empty list if
                # the write path ever stops going through the session.
                self.assertGreaterEqual(len(bodies), 2)
                for body in bodies:
                    self.assertNotIn("typecast", body)

    # Every single-select field the write paths put on the wire. Hand-kept: a new
    # select field written by mark_outcome has to be added here, or the
    # reconciliation below silently stops covering it.
    SELECT_FIELDS_ON_THE_WIRE = {
        "Prospects": ("Status",),
        "Sales Activities": ("Activity Type", "Outcome"),
        "Deals": ("Deal Status",),
    }

    @classmethod
    def _select_writes_on_the_wire(cls, session):
        """(table, field, choice) triples this session was actually asked to send."""
        writes = set()
        for call in [*session.patch.call_args_list, *session.post.call_args_list]:
            # .../v0/{base}/{table}[/{record}] — the table is what says which
            # field vocabulary a value had to come from.
            table = unquote(call.args[0].split("/")[5])
            body = call.kwargs["json"]
            records = body["records"] if "records" in body else [body]
            for record in records:
                for field, value in record["fields"].items():
                    if field in cls.SELECT_FIELDS_ON_THE_WIRE.get(table, ()):
                        writes.add((table, field, value))
        return writes

    def test_declared_vocabulary_matches_what_the_write_paths_send(self):
        # REQUIRED_SELECT_CHOICES is what the Airtable runbook step gets generated
        # from, so it is spelled out in the module rather than derived from the
        # maps — which means nothing but this test stops it drifting from reality.
        # Reconciled against four real runs' request bodies, not against the maps.
        observed = set()
        for event in sorted(mark_outcome.OUTCOME_EVENTS):
            with self.subTest(event=event):
                closing = event in mark_outcome.CLOSING_EVENTS
                session = self._session(
                    patch_status=mark_outcome.TARGET_STATUS[event],
                    patch_follow_up=None if closing else "2026-07-31",
                )
                mark_outcome.apply_mark_outcome(event=event, session=session, **self.ARGS)
                on_the_wire = self._select_writes_on_the_wire(session)
                self.assertTrue(on_the_wire)
                # The preflight can only vet what it predicts, so its prediction
                # for this event has to be exactly what the event then sent.
                self.assertEqual(set(mark_outcome.pending_select_writes(event)), on_the_wire)
                observed |= on_the_wire

        declared = {
            (table, field, value)
            for table, fields in mark_outcome.REQUIRED_SELECT_CHOICES.items()
            for field, values in fields.items()
            for value in values
        }
        # Exact, not a subset: a fifth event that needs a new choice fails here
        # instead of 422ing in production, and a choice left declared after the
        # code stopped writing it fails too rather than lingering in the runbook.
        self.assertEqual(observed, declared)

    def test_preflight_names_the_undeclared_choice_and_the_runbook(self):
        # The realistic mistake: an event mapping is renamed or added and the
        # matching choice never gets created in the base. Without this the run
        # dies on a bare Airtable 422 that reads like a transient API fault.
        session = self._session(patch_status="Follow-Up")
        with patch.dict(mark_outcome.ACTIVITY_TYPE, {"replied": "Reply Received"}):
            with self.assertRaises(mark_outcome.MarkOutcomeError) as caught:
                mark_outcome.apply_mark_outcome(event="replied", session=session, **self.ARGS)
        message = str(caught.exception)
        self.assertIn("'Reply Received'", message)
        self.assertIn("Sales Activities.Activity Type", message)
        self.assertIn(mark_outcome.RUNBOOK_DOC, message)
        # Fires before the first request, so a rejected event cannot leave the
        # prospect PATCHed to Follow-Up with no activity row explaining it.
        session.get.assert_not_called()
        session.patch.assert_not_called()
        session.post.assert_not_called()

    def test_preflight_blocks_a_new_event_whose_choices_are_undeclared(self):
        # A fifth event wired into the three maps, with choices nobody created:
        # "Follow-Up" is already declared, "Note" is not, and the first
        # undeclared one is what has to be named.
        with patch.dict(mark_outcome.TARGET_STATUS, {"ghosted": "Follow-Up"}), \
             patch.dict(mark_outcome.ACTIVITY_TYPE, {"ghosted": "Note"}), \
             patch.dict(mark_outcome.ACTIVITY_OUTCOME, {"ghosted": "No Reply"}):
            with self.assertRaisesRegex(
                mark_outcome.MarkOutcomeError, r"Sales Activities\.Activity Type choice 'Note'"
            ):
                mark_outcome.preflight_select_values("ghosted")

    def test_preflight_passes_for_every_shipped_event(self):
        # Guards the inverse failure: a typo in REQUIRED_SELECT_CHOICES that makes
        # the preflight reject work the base is perfectly able to accept.
        for event in sorted(mark_outcome.OUTCOME_EVENTS):
            with self.subTest(event=event):
                mark_outcome.preflight_select_values(event)

    def test_written_select_vocabulary_is_closed(self):
        # Without typecast these strings must already exist as choices in the
        # base or the write 422s. Changing one is a schema change, not a rename.
        self.assertEqual(set(mark_outcome.TARGET_STATUS.values()), {"Follow-Up", "Won", "Lost"})
        self.assertEqual(set(mark_outcome.ACTIVITY_TYPE.values()), {"Reply", "Call", "Close"})
        self.assertEqual(
            set(mark_outcome.ACTIVITY_OUTCOME.values()),
            {"Replied", "Meeting Booked", "Won", "Lost"},
        )
        self.assertEqual(mark_outcome.CLOSED_WON_DEAL_STATUS, "Deposit Pending")

    def test_every_status_written_or_accepted_exists_in_the_canonical_vocabulary(self):
        # REQUIRED_SELECT_CHOICES is deliberately spelled out rather than derived,
        # so this is the only thing tying it to queue_state's Status vocabulary.
        # Half-splitting the two is worse than either whole one: mark_outcome
        # would write a status the send queue's gate has never heard of.
        vocabulary = set(queue_state.PROSPECT_STATUS_CHOICES)
        self.assertLessEqual(
            mark_outcome.REQUIRED_SELECT_CHOICES["Prospects"]["Status"], vocabulary
        )
        self.assertLessEqual(set(mark_outcome.TARGET_STATUS.values()), vocabulary)
        # The source gate matches on a casefolded status, so a rename in the
        # vocabulary would make it accept nothing without raising anything.
        self.assertLessEqual(
            mark_outcome.OUTCOME_SOURCE_STATUSES, {value.casefold() for value in vocabulary}
        )

    def test_closed_won_creates_deal_with_optional_fields(self):
        session = self._session(patch_status="Won", patch_follow_up=None)
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
        session = self._session(patch_status="Won", patch_follow_up=None)
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

    def test_unknown_choice_422_names_the_choice_and_the_runbook(self):
        # The live failure mode for the whole window between this code shipping
        # and the Airtable runbook running: Won does not exist in the base yet.
        # Airtable's reply names neither the field nor the value, so untreated it
        # reads as a transient API fault worth retrying — forever.
        session = self._session(patch_status="Won", patch_follow_up=None)
        session.patch.return_value = _response(
            422, text='{"error":{"type":"INVALID_MULTIPLE_CHOICE_OPTIONS"}}'
        )
        with self.assertRaises(mark_outcome.MarkOutcomeError) as caught:
            mark_outcome.apply_mark_outcome(event="closed_won", session=session, **self.ARGS)
        message = str(caught.exception)
        self.assertIn("PATCH prospect failed 422", message)
        self.assertIn("Prospects.Status='Won'", message)
        self.assertIn(mark_outcome.RUNBOOK_DOC, message)
        # Only the failed request's own choices, not every choice the event needs:
        # the Deals write never happened and naming it sends the operator to the
        # wrong runbook line.
        self.assertNotIn("Deal Status", message)

    def test_unknown_choice_diagnosis_is_scoped_to_the_failing_table(self):
        session = self._session(patch_status="Follow-Up")
        session.post.side_effect = [
            _response(422, text='{"error":{"type":"INVALID_MULTIPLE_CHOICE_OPTIONS"}}')
        ]
        with self.assertRaises(mark_outcome.MarkOutcomeError) as caught:
            mark_outcome.apply_mark_outcome(event="replied", session=session, **self.ARGS)
        message = str(caught.exception)
        self.assertIn("Sales Activities.Activity Type='Reply'", message)
        self.assertIn("Sales Activities.Outcome='Replied'", message)
        self.assertNotIn("Prospects.Status", message)

    def test_other_failures_are_not_diagnosed_as_a_missing_choice(self):
        # A wrong diagnosis is worse than none: it would send the operator to
        # edit the base over a token or rate-limit problem.
        for status_code, text in ((401, "AUTHENTICATION_REQUIRED"), (429, "RATE_LIMIT")):
            with self.subTest(status_code=status_code):
                session = self._session(patch_status="Won", patch_follow_up=None)
                session.patch.return_value = _response(status_code, text=text)
                with self.assertRaises(mark_outcome.MarkOutcomeError) as caught:
                    mark_outcome.apply_mark_outcome(event="closed_won", session=session, **self.ARGS)
                self.assertNotIn(mark_outcome.RUNBOOK_DOC, str(caught.exception))


class TestNoAirtableWritePathTypecasts(unittest.TestCase):
    """Repo-wide source invariant, not a per-script assertion.

    typecast is how Airtable was told to invent a select choice for any string
    it received, which is what put a phone number into `Lead Priority` as a live
    `(440) 537-2459` option. Catching that by asserting on three known request
    bodies only holds until someone adds a fourth writer, so this scans the
    source instead: no shipped module may hand Airtable a typecast flag at all.
    """

    # Tests are excluded because they legitimately name typecast to prove its
    # absence; .venv and caches are third-party or generated.
    SKIP_DIRS = frozenset({".venv", ".git", "__pycache__", "node_modules", "tests"})

    def _repo_sources(self):
        for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
            dirnames[:] = [d for d in dirnames if d not in self.SKIP_DIRS and not d.startswith(".")]
            for name in sorted(filenames):
                if name.endswith(".py"):
                    yield Path(dirpath) / name

    @staticmethod
    def _typecast_lines(tree: ast.AST) -> list[int]:
        # Three spellings reach Airtable: a "typecast" key in whatever dict ends
        # up as the request body, a typecast= kwarg on a client helper, and the
        # body being built first and keyed afterwards (`body["typecast"] = True`).
        # The third is matched on the subscript itself rather than on the
        # assignment, so the augmented and deleted forms are covered by the same
        # rule; a *read* of body["typecast"] can only exist where the flag does.
        lines = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                lines.extend(
                    key.lineno
                    for key in node.keys
                    if isinstance(key, ast.Constant) and key.value == "typecast"
                )
            elif isinstance(node, ast.Call):
                lines.extend(node.lineno for kw in node.keywords if kw.arg == "typecast")
            elif isinstance(node, ast.Subscript):
                if isinstance(node.slice, ast.Constant) and node.slice.value == "typecast":
                    lines.append(node.lineno)
        return sorted(lines)

    def test_no_module_hands_airtable_a_typecast_flag(self):
        scanned = 0
        offenders = []
        for path in self._repo_sources():
            scanned += 1
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            offenders.extend(
                f"{path.relative_to(REPO_ROOT)}:{line}" for line in self._typecast_lines(tree)
            )
        # A pruned walk that silently found nothing would pass vacuously.
        self.assertGreater(scanned, 20)
        self.assertEqual(offenders, [], f"typecast reintroduced at: {', '.join(offenders)}")

    def test_scan_detects_a_typecast_write_path(self):
        # Proves the invariant above can actually fail, since its passing state
        # is an empty list either way.
        planted = ast.parse('requests.patch(url, json={"fields": f, "typecast": True})')
        self.assertEqual(self._typecast_lines(planted), [1])
        kwarg_form = ast.parse("table.update(record_id, fields, typecast=True)")
        self.assertEqual(self._typecast_lines(kwarg_form), [1])
        # The literal-dict form is the one review catches anyway. Building the
        # body and keying it afterwards is the reintroduction that reads as
        # innocuous, so it is the one the scanner most has to see.
        subscript_form = ast.parse('body = {"fields": f}\nbody["typecast"] = True\n')
        self.assertEqual(self._typecast_lines(subscript_form), [2])


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
