from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import upload_airtable
from enrich_leads import detect_form_signals, scrape_site
from lead_machine.airtable_sync import build_airtable_payload, plan_airtable_sync
from lead_machine.buyer_signals import BUYER_INTENT_LOGIC_VERSION
from lead_machine.evidence import build_evidence_profile
from lead_machine.feedback import compute_feedback_metrics
from lead_machine.icp import hard_icp_gate
from lead_machine.lead_card import build_lead_card
from lead_machine.llm_queue import build_llm_prompt, write_review_queue
from lead_machine.manual_scores import load_manual_score, merge_manual_score
from lead_machine.schema import normalize_lead, row_dedupe_keys
from lead_machine.sendability import flatten_safe_outreach_claims, normalize_evidence_refs
from make_packets import is_eligible, main as make_packets_main, packet_markdown
from make_llm_batch import build_batch_prompt, write_llm_batches
from import_llm_score_bundle import import_score_bundle
from score_leads import main as score_leads_main
from score_leads import read_csv_rows, score_record, score_records_manual, write_csv


AIRTABLE_ENV_VARS = (
    "AIRTABLE_API_KEY",
    "AIRTABLE_TOKEN",
    "AIRTABLE_PAT",
    "AIRTABLE_BASE_ID",
    "AIRTABLE_TABLE_NAME",
    "AIRTABLE_TABLE",
    "AIRTABLE_PROSPECTS_TABLE",
)


class LeadMachineContractsTest(unittest.TestCase):
    def _airtable_csv_row(self, **overrides: str) -> dict[str, str]:
        row = {
            "Business Name": "A+ Landscaping LLC",
            "Owner Name": "Alex Owner",
            "Vertical": "hardscaping",
            "City": "Kirtland",
            "Phone": "(440) 637-5848",
            "Email": "owner@example.com",
            "Website URL": "https://example.com",
            "Google Maps URL": "https://maps.google.com/?cid=123",
            "Reviews": "130",
            "Rating": "4.9",
            "Current Website Quality": "Weak",
            "Best Service Angle": "Website Rebuild",
            "Specific Observation": "The site hides estimate requests.",
            "Lead Priority": "A",
            "Score Total": "86",
            "Contact Form Detected": "No",
            "Estimate Form Detected": "No",
            "Form Strength": "None",
            "Form Detection Method": "none",
            "CTA Strength": "Weak",
            "Visible Pain Score": "35",
            "Visible Pain Reasons": "No estimate form detected; No clear estimate CTA",
            "Send Trigger": "No estimate form detected",
            "AI Confidence": "medium",
            "Recommended Action": "Build mockup then SMS owner",
            "Opener": "Want me to send a cleaner estimate-page mockup?",
            "Evidence Refs": "ev-google-rating, ev-site",
            "Safe Outreach Claims": "Strong Google review proof.",
            "Unsafe Claims": "Do not claim they are losing jobs.",
            "Packet Eligible": "Yes",
            "Packet Eligibility Reason": "A lead with Website Rebuild angle.",
            "Sendable": "Yes",
            "Minimum Evidence Passed": "Yes",
            "Send Blockers": "",
            "Place Key": "ChIJ123",
        }
        row.update(overrides)
        return row

    def _write_airtable_csv(self, path: Path, rows: list[dict[str, str]]) -> None:
        fieldnames: list[str] = []
        for row in rows:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def _run_upload(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("sys.argv", argv), redirect_stdout(stdout), redirect_stderr(stderr):
            code = upload_airtable.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def _run_make_packets(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("sys.argv", argv), redirect_stdout(stdout), redirect_stderr(stderr):
            code = make_packets_main()
        return code, stdout.getvalue(), stderr.getvalue()

    def _dry_run_plan(self, stdout: str) -> list[dict[str, object]]:
        lines = stdout.splitlines()
        self.assertTrue(lines and lines[0].startswith("[DRY-RUN]"))
        return json.loads("\n".join(lines[1:]))

    def _write_packet_index(self, packet_dir: Path, rows: list[dict[str, str]]) -> None:
        packet_dir.mkdir(parents=True, exist_ok=True)
        fieldnames = ["Packet Path", "Business Name", "Place Key", "Phone", "Lead Priority", "Offer Fit"]
        with (packet_dir / "packet_index.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    @contextmanager
    def _without_airtable_env(self):
        with patch.dict(os.environ, {}, clear=False), patch("upload_airtable.load_dotenv", return_value=False):
            for key in AIRTABLE_ENV_VARS:
                os.environ.pop(key, None)
            yield

    def test_airtable_csv_dry_run_builds_payload_for_sendable_row_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            self._write_airtable_csv(csv_path, [self._airtable_csv_row(**{"Outreach Channel": ""})])

            with patch("upload_airtable.requests.get") as get:
                code, stdout, stderr = self._run_upload(["upload_airtable.py", "--input", str(csv_path)])

        self.assertEqual(code, 0, stderr)
        get.assert_not_called()
        planned = self._dry_run_plan(stdout)
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["method"], "POST")
        self.assertEqual(planned[0]["upsert_key"], "place:ChIJ123")
        fields = planned[0]["payload"]["fields"]  # type: ignore[index]
        self.assertEqual(fields["Business Name"], "A+ Landscaping LLC")
        self.assertEqual(fields["Best Service Angle"], "Website Rebuild")
        self.assertEqual(fields["Status"], "Mockup Needed")
        self.assertEqual(fields["Next Action"], "Build mockup")
        self.assertEqual(fields["Outreach Channel"], "Google Maps")
        self.assertEqual(fields["Packet Eligible"], "Yes")
        self.assertEqual(fields["Sendable"], "Yes")
        self.assertEqual(fields["Contact Form Detected"], "No")
        self.assertEqual(fields["Estimate Form Detected"], "No")
        self.assertEqual(fields["Form Strength"], "None")
        self.assertEqual(fields["Form Detection Method"], "none")
        self.assertEqual(fields["CTA Strength"], "Weak")
        self.assertEqual(fields["Visible Pain Score"], 35)
        self.assertEqual(fields["Visible Pain Reasons"], "No estimate form detected; No clear estimate CTA")
        self.assertEqual(fields["Send Trigger"], "No estimate form detected")

    def test_scored_csv_suppresses_placeholder_emails_and_prefers_business_domain(self) -> None:
        record = {
            "google": {
                "business_name": "Fresh Shine Detail",
                "city": "Cleveland",
                "phone": "(216) 555-1212",
                "website_url": "https://freshshinedetail.com",
                "google_maps_url": "https://maps.google.com/?cid=detail123",
                "reviews": 48,
                "rating": 4.8,
                "source_query": "ceramic coating cleveland",
            },
            "website": {
                "site_status": "Poor",
                "domain": "freshshinedetail.com",
                "final_url": "https://freshshinedetail.com",
                "emails_found": [
                    "user@domain.com",
                    "team@gmail.com",
                    "hello@freshshinedetail.com",
                    "test@example.com",
                    "noreply@freshshinedetail.com",
                ],
                "phones_found": ["2165551212"],
                "site_issue_flags": ["weak_basic_form", "only_phone_email_path"],
                "contact_form_detected": "No",
                "estimate_form_detected": "No",
                "form_strength": "None",
                "form_detection_method": "none",
                "cta_strength": "Weak",
                "high_ticket_terms": ["ceramic coating"],
            },
            "evidence": {
                "visible_pain_score": 35,
                "visible_pain_reasons": ["Only phone/email estimate path"],
                "visible_pain_categories": ["estimate_path_pain"],
                "send_trigger": "Only phone/email estimate path",
                "evidence_items": [],
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            write_csv(csv_path, [score_record(record, "detailing")])
            with csv_path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Email"], "hello@freshshinedetail.com, team@gmail.com")
        self.assertNotIn("user@domain.com", rows[0]["Raw Evidence"])

    def test_make_packets_suppresses_placeholder_emails_from_packet_artifacts(self) -> None:
        raw_evidence = {
            "website": {
                "domain": "freshshinedetail.com",
                "final_url": "https://freshshinedetail.com",
                "emails_found": [
                    "user@domain.com",
                    "hello@freshshinedetail.com",
                    "team@gmail.com",
                    "donotreply@freshshinedetail.com",
                ],
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            packet_dir = Path(tmp) / "packets"
            self._write_airtable_csv(
                csv_path,
                [
                    self._airtable_csv_row(
                        **{
                            "Business Name": "Fresh Shine Detail",
                            "Email": "user@domain.com, hello@freshshinedetail.com, team@gmail.com",
                            "Website URL": "https://freshshinedetail.com",
                            "Raw Evidence": json.dumps(raw_evidence),
                        }
                    )
                ],
            )

            code, _stdout, stderr = self._run_make_packets(
                ["make_packets.py", "--inputs", str(csv_path), "--output-dir", str(packet_dir)]
            )
            packet_text = next(packet_dir.glob("*/*.md")).read_text(encoding="utf-8")
            index_text = (packet_dir / "packet_index.csv").read_text(encoding="utf-8")

        self.assertEqual(code, 0, stderr)
        self.assertIn("hello@freshshinedetail.com, team@gmail.com", packet_text)
        self.assertIn("hello@freshshinedetail.com, team@gmail.com", index_text)
        self.assertNotIn("user@domain.com", packet_text)
        self.assertNotIn("donotreply@freshshinedetail.com", packet_text)
        self.assertNotIn("user@domain.com", index_text)

    def test_airtable_csv_dry_run_normalizes_lane_labels_for_select_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            self._write_airtable_csv(
                csv_path,
                [
                    self._airtable_csv_row(
                        **{
                            "Best Service Angle": "Social/Jobber Escape",
                            "Offer Fit": "social_jobber_escape",
                            "Send Trigger": "social_jobber_escape lane: no owned website",
                        }
                    )
                ],
            )

            code, stdout, stderr = self._run_upload(["upload_airtable.py", "--input", str(csv_path)])

        self.assertEqual(code, 0, stderr)
        planned = self._dry_run_plan(stdout)
        fields = planned[0]["payload"]["fields"]  # type: ignore[index]
        self.assertEqual(fields["Offer Fit"], "No-Site Launch")
        self.assertEqual(fields["Best Service Angle"], "No-Site Launch")
        self.assertEqual(fields["Send Trigger"], "social_jobber_escape lane: no owned website")

    def test_airtable_payload_normalizes_all_internal_lane_labels_for_select_fields(self) -> None:
        cases = {
            "Social/Jobber Escape": "No-Site Launch",
            "no_site_launch": "No-Site Launch",
            "social_jobber_escape": "No-Site Launch",
            "owned_weak_site_rebuild": "Website Rebuild",
            "premium_perception_gap": "Website Rebuild",
            "portfolio_presentation_gap": "Website Rebuild",
            "estimate_funnel": "Estimate Funnel",
            "optimization_only": "Automation / Follow-Up",
        }
        supported_select_values = set(cases.values())

        for lane, expected in cases.items():
            with self.subTest(lane=lane):
                card = {
                    "place_id_or_unique_key": "ChIJ123",
                    "best_angle": lane,
                    "airtable_fields": {
                        "Business Name": "A+ Landscaping LLC",
                        "Best Service Angle": lane,
                        "Offer Fit": lane,
                        "Specific Observation": f"Internal lane: {lane}",
                        "Visible Pain Reasons": f"Internal lane: {lane}",
                        "Send Trigger": f"Internal lane: {lane}",
                        "Opener": f"Internal lane: {lane}",
                    },
                }

                fields = build_airtable_payload(card)["fields"]

                self.assertEqual(fields["Offer Fit"], expected)
                self.assertEqual(fields["Best Service Angle"], expected)
                self.assertIn(fields["Offer Fit"], supported_select_values)
                self.assertIn(fields["Best Service Angle"], supported_select_values)
                self.assertEqual(fields["Specific Observation"], f"Internal lane: {lane}")
                self.assertEqual(fields["Visible Pain Reasons"], f"Internal lane: {lane}")
                self.assertEqual(fields["Send Trigger"], f"Internal lane: {lane}")
                self.assertEqual(fields["Opener"], f"Internal lane: {lane}")

    def test_airtable_payload_omits_unsupported_vertical_select_values(self) -> None:
        unsupported_card = {
            "place_id_or_unique_key": "ChIJ123",
            "airtable_fields": {
                "Business Name": "Tint Shop",
                "Vertical": "window tinting",
                "Phone": "(614) 555-0101",
            },
        }

        fields = build_airtable_payload(unsupported_card)["fields"]

        self.assertNotIn("Vertical", fields)
        self.assertEqual(fields["Business Name"], "Tint Shop")

        for vertical in ("water restoration", "water_restoration"):
            card = {
                "place_id_or_unique_key": "ChIJ456",
                "airtable_fields": {
                    "Business Name": "Columbus Restoration",
                    "Vertical": vertical,
                    "Phone": "(614) 555-0102",
                },
            }

            fields = build_airtable_payload(card)["fields"]

            self.assertEqual(fields["Vertical"], "water_restoration")

    def test_airtable_plan_reports_dropped_unsupported_vertical_values(self) -> None:
        card = {
            "place_id_or_unique_key": "ChIJ123",
            "airtable_fields": {
                "Business Name": "Tint Shop",
                "Vertical": "window tinting",
                "Phone": "(614) 555-0101",
            },
        }

        planned = plan_airtable_sync([card])

        self.assertEqual(planned[0]["dropped_fields"], [{"field": "Vertical", "value": "window tinting"}])
        self.assertNotIn("Vertical", planned[0]["payload"]["fields"])

    def test_airtable_csv_dry_run_accepts_sparse_scored_row_and_upserts_by_phone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            self._write_airtable_csv(
                csv_path,
                [
                    {
                        "Business Name": "Phone Only Landscaping",
                        "Phone": "(440) 637-5848",
                        "Lead Priority": "A",
                        "Score Total": "86",
                        "Sendable": "Yes",
                        "Packet Eligible": "Yes",
                    }
                ],
            )

            code, stdout, stderr = self._run_upload(["upload_airtable.py", "--input", str(csv_path)])

        self.assertEqual(code, 0, stderr)
        planned = self._dry_run_plan(stdout)
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["method"], "POST")
        self.assertEqual(planned[0]["upsert_key"], "phone:4406375848")
        fields = planned[0]["payload"]["fields"]  # type: ignore[index]
        self.assertEqual(fields["Business Name"], "Phone Only Landscaping")
        self.assertEqual(fields["Phone"], "(440) 637-5848")
        self.assertEqual(fields["Lead Priority"], "A")
        self.assertEqual(fields["Score Total"], 86)
        self.assertEqual(fields["Sendable"], "Yes")
        self.assertEqual(fields["Packet Eligible"], "Yes")
        self.assertNotIn("Recommended Action", fields)
        self.assertNotIn("Place Key", fields)

    def test_airtable_csv_dry_run_preserves_explicit_sendable_operational_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            self._write_airtable_csv(
                csv_path,
                [
                    self._airtable_csv_row(
                        **{
                            "Status": "Contacted",
                            "Next Action": "Follow up",
                        }
                    )
                ],
            )

            code, stdout, stderr = self._run_upload(["upload_airtable.py", "--input", str(csv_path)])

        self.assertEqual(code, 0, stderr)
        planned = self._dry_run_plan(stdout)
        self.assertEqual(len(planned), 1)
        fields = planned[0]["payload"]["fields"]  # type: ignore[index]
        self.assertEqual(fields["Status"], "Contacted")
        self.assertEqual(fields["Next Action"], "Follow up")

    def test_airtable_patch_does_not_send_defaulted_operational_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            self._write_airtable_csv(csv_path, [self._airtable_csv_row(**{"Outreach Channel": ""})])

            records = upload_airtable.read_input_records(csv_path, only_sendable=False, packet_dir=Path(tmp))
            planned = plan_airtable_sync(records, existing_records_by_key={"place:ChIJ123": "rec-existing"})

        self.assertEqual(planned[0]["method"], "PATCH")
        fields = planned[0]["payload"]["fields"]  # type: ignore[index]
        self.assertNotIn("Status", fields)
        self.assertNotIn("Next Action", fields)
        self.assertNotIn("Outreach Channel", fields)

    def test_make_packets_updates_scored_csv_with_current_batch_packet_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                csv_path = Path("output/scored_current_batch_manual.csv")
                csv_path.parent.mkdir(parents=True)
                stale_path = "packets_manual_existing_manual/A_no-site-launch/003-m-m-masonry-and-landscape-llc.md"
                self._write_airtable_csv(
                    csv_path,
                    [
                        self._airtable_csv_row(
                            **{
                                "Business Name": "M & M Masonry and Landscape LLC",
                                "Phone": "(440) 622-0083",
                                "Place Key": "ChIJe1e2bAl1MYgRmYxkuR04D40",
                                "Lead Priority": "A",
                                "Offer Fit": "No-Site Launch",
                                "Packet Path": stale_path,
                            }
                        )
                    ],
                )

                code, _stdout, stderr = self._run_make_packets(
                    [
                        "make_packets.py",
                        "--inputs",
                        str(csv_path),
                        "--output-dir",
                        "packets_current_batch_manual",
                    ]
                )

                with csv_path.open(newline="", encoding="utf-8") as f:
                    rows = list(csv.DictReader(f))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            rows[0]["Packet Path"],
            "packets_current_batch_manual/A_no-site-launch/001-m-m-masonry-and-landscape-llc.md",
        )
        self.assertNotIn("packets_manual_existing_manual", rows[0]["Packet Path"])

    def test_airtable_uses_current_batch_packet_index_instead_of_stale_packet_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                csv_path = Path("output/scored_current_batch_manual.csv")
                csv_path.parent.mkdir(parents=True)
                current_path = "packets_current_batch_manual/A_no-site-launch/004-m-m-masonry-and-landscape-llc.md"
                stale_path = "packets_manual_existing_manual/A_no-site-launch/003-m-m-masonry-and-landscape-llc.md"
                Path(current_path).parent.mkdir(parents=True)
                Path(current_path).write_text("current", encoding="utf-8")
                Path(stale_path).parent.mkdir(parents=True)
                Path(stale_path).write_text("stale", encoding="utf-8")
                self._write_packet_index(
                    Path("packets_current_batch_manual"),
                    [
                        {
                            "Packet Path": current_path,
                            "Business Name": "M & M Masonry and Landscape LLC",
                            "Place Key": "ChIJe1e2bAl1MYgRmYxkuR04D40",
                            "Phone": "(440) 622-0083",
                            "Lead Priority": "A",
                            "Offer Fit": "No-Site Launch",
                        }
                    ],
                )
                self._write_airtable_csv(
                    csv_path,
                    [
                        self._airtable_csv_row(
                            **{
                                "Business Name": "M & M Masonry and Landscape LLC",
                                "Phone": "(440) 622-0083",
                                "Place Key": "ChIJe1e2bAl1MYgRmYxkuR04D40",
                                "Packet Path": stale_path,
                            }
                        )
                    ],
                )

                code, stdout, stderr = self._run_upload(
                    ["upload_airtable.py", "--input", str(csv_path), "--only-sendable"]
                )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(code, 0, stderr)
        planned = self._dry_run_plan(stdout)
        fields = planned[0]["payload"]["fields"]  # type: ignore[index]
        self.assertEqual(fields["Packet Path"], current_path)
        self.assertNotIn("packets_manual_existing_manual", fields["Packet Path"])

    def test_airtable_payload_uses_current_packet_path_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                csv_path = Path("output/scored_current_batch_manual.csv")
                csv_path.parent.mkdir(parents=True)
                current_path = "packets_current_batch_manual/A_website-rebuild/001-a-landscaping-llc.md"
                Path(current_path).parent.mkdir(parents=True)
                Path(current_path).write_text("current", encoding="utf-8")
                self._write_airtable_csv(
                    csv_path,
                    [self._airtable_csv_row(**{"Offer Fit": "Website Rebuild", "Packet Path": current_path})],
                )

                records = upload_airtable.read_input_records(
                    csv_path,
                    only_sendable=True,
                    packet_dir=Path("packets_current_batch_manual"),
                )
                planned = plan_airtable_sync(records, existing_records_by_key={})
            finally:
                os.chdir(old_cwd)

        fields = planned[0]["payload"]["fields"]  # type: ignore[index]
        self.assertEqual(fields["Packet Path"], current_path)

    def test_airtable_only_sendable_excludes_non_sendable_csv_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            rows = [
                self._airtable_csv_row(),
                self._airtable_csv_row(
                    **{"Business Name": "Not Sendable LLC", "Sendable": "No", "Place Key": "ChIJ999"}
                ),
                self._airtable_csv_row(
                    **{"Business Name": "No Packet LLC", "Packet Eligible": "No", "Place Key": "ChIJ888"}
                ),
            ]
            self._write_airtable_csv(csv_path, rows)

            code, stdout, stderr = self._run_upload(
                ["upload_airtable.py", "--input", str(csv_path), "--only-sendable"]
            )

        self.assertEqual(code, 0, stderr)
        planned = self._dry_run_plan(stdout)
        self.assertEqual(len(planned), 1)
        fields = planned[0]["payload"]["fields"]  # type: ignore[index]
        self.assertEqual(fields["Business Name"], "A+ Landscaping LLC")

    def test_airtable_include_manual_review_excludes_watchlist_and_no_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            rows = [
                self._airtable_csv_row(**{"Business Name": "Sendable Co", "Packet Tier": "Sendable Packet", "Place Key": "ChIJ1"}),
                self._airtable_csv_row(**{"Business Name": "Manual Co", "Sendable": "No", "Packet Tier": "Manual Review Packet", "Place Key": "ChIJ2"}),
                self._airtable_csv_row(**{"Business Name": "Watchlist Co", "Sendable": "No", "Packet Eligible": "", "Packet Tier": "Watchlist", "Place Key": "ChIJ3"}),
                self._airtable_csv_row(**{"Business Name": "No Packet Co", "Sendable": "No", "Packet Eligible": "No", "Packet Tier": "No Packet", "Place Key": "ChIJ4"}),
            ]
            self._write_airtable_csv(csv_path, rows)
            code, stdout, stderr = self._run_upload(
                ["upload_airtable.py", "--input", str(csv_path), "--include-manual-review"]
            )
        self.assertEqual(code, 0, stderr)
        planned = self._dry_run_plan(stdout)
        names = {p["payload"]["fields"]["Business Name"] for p in planned}
        self.assertEqual(len(planned), 2)
        self.assertIn("Sendable Co", names)
        self.assertIn("Manual Co", names)
        self.assertNotIn("Watchlist Co", names)
        self.assertNotIn("No Packet Co", names)

    def test_airtable_manual_review_packet_gets_status_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            rows = [
                self._airtable_csv_row(**{"Business Name": "Manual Co", "Sendable": "No", "Packet Tier": "Manual Review Packet", "Place Key": "ChIJ2"}),
            ]
            self._write_airtable_csv(csv_path, rows)
            code, stdout, stderr = self._run_upload(
                ["upload_airtable.py", "--input", str(csv_path), "--include-manual-review"]
            )
        self.assertEqual(code, 0, stderr)
        planned = self._dry_run_plan(stdout)
        self.assertEqual(len(planned), 1)
        fields = planned[0]["payload"]["fields"]
        self.assertEqual(fields.get("Status"), "Manual Review")

    def test_airtable_manual_review_packet_overrides_csv_status_new(self) -> None:
        """Manual Review Packet rows must get Status='Manual Review' even if CSV has Status='New'."""
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            rows = [
                self._airtable_csv_row(**{
                    "Business Name": "Manual Co With Status",
                    "Sendable": "No",
                    "Packet Tier": "Manual Review Packet",
                    "Place Key": "ChIJ9",
                    "Status": "New",
                }),
            ]
            self._write_airtable_csv(csv_path, rows)
            code, stdout, stderr = self._run_upload(
                ["upload_airtable.py", "--input", str(csv_path), "--include-manual-review"]
            )
        self.assertEqual(code, 0, stderr)
        planned = self._dry_run_plan(stdout)
        self.assertEqual(len(planned), 1)
        fields = planned[0]["payload"]["fields"]
        self.assertEqual(fields.get("Status"), "Manual Review")

    def test_airtable_platform_emails_removed_real_gmail_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            rows = [
                self._airtable_csv_row(**{
                    "Business Name": "Zimon Detailing",
                    "Email": "errors@sentry.io, zimon@gmail.com",
                    "Place Key": "ChIJ1",
                }),
                self._airtable_csv_row(**{
                    "Business Name": "Haynes Auto",
                    "Email": "support@webador.com",
                    "Place Key": "ChIJ2",
                }),
            ]
            self._write_airtable_csv(csv_path, rows)
            records = upload_airtable.read_input_records(csv_path, only_sendable=False, packet_dir=Path(tmp))
        zimon = next(r for r in records if r["business_name"] == "Zimon Detailing")
        haynes = next(r for r in records if r["business_name"] == "Haynes Auto")
        self.assertEqual(zimon["airtable_fields"].get("Email"), "zimon@gmail.com")
        self.assertNotIn("Email", haynes["airtable_fields"])

    def test_airtable_csv_upsert_key_prefers_place_key_then_normalized_phone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            rows = [
                self._airtable_csv_row(),
                self._airtable_csv_row(**{"Business Name": "Phone Only LLC", "Place Key": "", "Phone": "+1 440.555.0101"}),
            ]
            self._write_airtable_csv(csv_path, rows)

            records = upload_airtable.read_input_records(csv_path, only_sendable=False, packet_dir=Path(tmp))
            planned = plan_airtable_sync(
                records,
                existing_records_by_key={"place:ChIJ123": "rec-place", "phone:4405550101": "rec-phone"},
            )

        self.assertEqual(planned[0]["method"], "PATCH")
        self.assertEqual(planned[0]["record_id"], "rec-place")
        self.assertEqual(planned[0]["upsert_key"], "place:ChIJ123")
        self.assertEqual(planned[1]["method"], "PATCH")
        self.assertEqual(planned[1]["record_id"], "rec-phone")
        self.assertEqual(planned[1]["upsert_key"], "phone:4405550101")

    def test_airtable_sync_rejects_duplicate_input_upsert_keys(self) -> None:
        first = {
            "upsert_key": "phone:4405550101",
            "airtable_fields": {"Business Name": "First Patio Co", "Phone": "(440) 555-0101"},
        }
        duplicate = {
            "upsert_key": "phone:4405550101",
            "airtable_fields": {"Business Name": "Duplicate Patio Co", "Phone": "(440) 555-0101"},
        }

        with self.assertRaises(ValueError) as caught:
            plan_airtable_sync([first, duplicate], existing_records_by_key={})

        self.assertIn("Duplicate Airtable upsert key in input: phone:4405550101", str(caught.exception))

    def test_airtable_existing_phone_key_matches_incoming_place_key(self) -> None:
        record = {
            "upsert_key": "place:ChIJ-new",
            "place_id_or_unique_key": "ChIJ-new",
            "phone": "(440) 555-0101",
            "airtable_fields": {
                "Business Name": "Flawless Coating and Detailing",
                "Place Key": "ChIJ-new",
                "Phone": "(440) 555-0101",
            },
        }

        planned = plan_airtable_sync([record], existing_records_by_key={"phone:4405550101": "rec-phone"})

        self.assertEqual(planned[0]["method"], "PATCH")
        self.assertEqual(planned[0]["record_id"], "rec-phone")
        self.assertEqual(planned[0]["upsert_key"], "place:ChIJ-new")
        self.assertEqual(planned[0]["match_reason"], "phone")
        self.assertEqual(planned[0]["payload"]["fields"]["Place Key"], "ChIJ-new")

    def test_airtable_existing_domain_key_matches_incoming_place_key(self) -> None:
        record = {
            "upsert_key": "place:ChIJ-new",
            "place_id_or_unique_key": "ChIJ-new",
            "airtable_fields": {
                "Business Name": "Fresh Shine Detail",
                "Place Key": "ChIJ-new",
                "Website URL": "https://www.freshshinedetail.com/services",
            },
        }

        planned = plan_airtable_sync([record], existing_records_by_key={"domain:freshshinedetail.com": "rec-domain"})

        self.assertEqual(planned[0]["method"], "PATCH")
        self.assertEqual(planned[0]["record_id"], "rec-domain")
        self.assertEqual(planned[0]["match_reason"], "domain")

    def test_airtable_generic_social_domain_does_not_match_existing_record(self) -> None:
        record = {
            "upsert_key": "place:ChIJ-new",
            "place_id_or_unique_key": "ChIJ-new",
            "airtable_fields": {
                "Business Name": "Peach Drive Auto",
                "Place Key": "ChIJ-new",
                "Website URL": "https://www.instagram.com/peachdriveauto",
            },
        }

        planned = plan_airtable_sync([record], existing_records_by_key={"domain:instagram.com": "rec-action-tint"})

        self.assertEqual(planned[0]["method"], "POST")
        self.assertNotIn("match_reason", planned[0])

    def test_airtable_existing_business_city_key_matches_when_unique(self) -> None:
        record = {
            "upsert_key": "place:ChIJ-new",
            "place_id_or_unique_key": "ChIJ-new",
            "airtable_fields": {
                "Business Name": "Fresh Shine Detail LLC",
                "Place Key": "ChIJ-new",
                "City": "Cleveland",
            },
        }

        planned = plan_airtable_sync(
            [record],
            existing_records_by_key={"business_city:freshshinedetailllc|cleveland": "rec-name-city"},
        )

        self.assertEqual(planned[0]["method"], "PATCH")
        self.assertEqual(planned[0]["record_id"], "rec-name-city")
        self.assertEqual(planned[0]["match_reason"], "business_city")

    def test_airtable_ambiguous_existing_fallback_match_skips(self) -> None:
        record = {
            "upsert_key": "place:ChIJ-new",
            "place_id_or_unique_key": "ChIJ-new",
            "phone": "(440) 555-0101",
            "airtable_fields": {
                "Business Name": "Fresh Shine Detail",
                "Place Key": "ChIJ-new",
                "Phone": "(440) 555-0101",
                "Website URL": "freshshinedetail.com",
            },
        }

        planned = plan_airtable_sync(
            [record],
            existing_records_by_key={"phone:4405550101": ["rec-one", "rec-two"]},
        )

        self.assertEqual(planned[0]["method"], "SKIP")
        self.assertEqual(planned[0]["record_id"], "")
        self.assertEqual(planned[0]["match_reason"], "phone")
        self.assertIn("Ambiguous Airtable existing match", planned[0]["warning"])

    def test_airtable_true_new_record_posts(self) -> None:
        record = {
            "upsert_key": "place:ChIJ-new",
            "place_id_or_unique_key": "ChIJ-new",
            "phone": "(440) 555-0101",
            "airtable_fields": {
                "Business Name": "Fresh Shine Detail",
                "Place Key": "ChIJ-new",
                "Phone": "(440) 555-0101",
                "Website URL": "freshshinedetail.com",
                "City": "Cleveland",
            },
        }

        planned = plan_airtable_sync([record], existing_records_by_key={})

        self.assertEqual(planned[0]["method"], "POST")
        self.assertEqual(planned[0]["record_id"], "")
        self.assertNotIn("match_reason", planned[0])

    def test_airtable_dry_run_fetches_existing_records_when_env_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            self._write_airtable_csv(csv_path, [self._airtable_csv_row()])

            with (
                patch.dict(
                    os.environ,
                    {
                        "AIRTABLE_BASE_ID": "base123",
                        "AIRTABLE_PROSPECTS_TABLE": "Prospects",
                        "AIRTABLE_PAT": "token123",
                    },
                    clear=False,
                ),
                patch("upload_airtable.load_dotenv", return_value=False),
                patch("upload_airtable.requests.get") as get,
                patch("upload_airtable.requests.post") as post,
                patch("upload_airtable.requests.patch") as patch_request,
            ):
                get.return_value.status_code = 200
                get.return_value.json.return_value = {
                    "records": [
                        {
                            "id": "rec-phone",
                            "fields": {
                                "Business Name": "A+ Landscaping LLC",
                                "Phone": "(440) 637-5848",
                            },
                        }
                    ]
                }
                code, stdout, stderr = self._run_upload(["upload_airtable.py", "--input", str(csv_path), "--dry-run"])

        self.assertEqual(code, 0, stderr)
        self.assertFalse(post.called)
        self.assertFalse(patch_request.called)
        planned = self._dry_run_plan(stdout)
        self.assertEqual(planned[0]["method"], "PATCH")
        self.assertEqual(planned[0]["record_id"], "rec-phone")
        self.assertEqual(planned[0]["match_reason"], "phone")
        self.assertIn("[MATCH] place:ChIJ123 -> PATCH rec-phone via phone", stderr)

    def test_airtable_live_mode_requires_env_vars_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            self._write_airtable_csv(csv_path, [self._airtable_csv_row()])

            with (
                self._without_airtable_env(),
                patch("upload_airtable.requests.get") as get,
                patch("upload_airtable.requests.post") as post,
                patch("upload_airtable.requests.patch") as patch_request,
                patch("upload_airtable.validate_airtable_fields") as validate_fields,
            ):
                code, _stdout, stderr = self._run_upload(["upload_airtable.py", "--input", str(csv_path), "--live"])

        self.assertEqual(code, 1)
        get.assert_not_called()
        post.assert_not_called()
        patch_request.assert_not_called()
        validate_fields.assert_not_called()
        self.assertIn("AIRTABLE_BASE_ID", stderr)
        self.assertIn("AIRTABLE_PROSPECTS_TABLE", stderr)
        self.assertIn("AIRTABLE_PAT", stderr)

    def test_airtable_dry_run_does_not_require_env_vars_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "scored.csv"
            self._write_airtable_csv(csv_path, [self._airtable_csv_row()])

            with (
                self._without_airtable_env(),
                patch("upload_airtable.requests.get") as get,
                patch("upload_airtable.requests.post") as post,
                patch("upload_airtable.requests.patch") as patch_request,
                patch("upload_airtable.validate_airtable_fields") as validate_fields,
            ):
                code, stdout, stderr = self._run_upload(["upload_airtable.py", "--input", str(csv_path)])

        self.assertEqual(code, 0, stderr)
        get.assert_not_called()
        post.assert_not_called()
        patch_request.assert_not_called()
        validate_fields.assert_not_called()
        planned = self._dry_run_plan(stdout)
        self.assertEqual(len(planned), 1)

    def test_normalizes_google_places_row_and_dedupe_keys_in_precedence_order(self) -> None:
        row = {
            "business_name": "  A+ Landscaping LLC  ",
            "category": "general_contractor",
            "city": "Kirtland",
            "address": "7955 Euclid Chardon Rd, Kirtland, OH 44094, USA",
            "phone": "(440) 637-5848",
            "website_url": "https://www.example.com/services",
            "google_maps_url": "https://maps.google.com/?cid=123",
            "rating": "4.9",
            "reviews": "130",
            "place_id": "ChIJ123",
            "source_query": "hardscaping contractor Cleveland OH",
        }

        lead = normalize_lead(row, fallback_vertical="hardscaping", source_batch="round1")

        self.assertEqual(lead.business_name, "A+ Landscaping LLC")
        self.assertEqual(lead.vertical, "hardscaping")
        self.assertEqual(lead.website, "https://www.example.com/services")
        self.assertEqual(lead.google_rating, 4.9)
        self.assertEqual(lead.google_review_count, 130)
        self.assertEqual(lead.source_batch, "round1")
        self.assertEqual(lead.place_id_or_unique_key, "ChIJ123")
        self.assertEqual(
            row_dedupe_keys(lead),
            [
                "place:ChIJ123",
                "phone:4406375848",
                "domain:example.com",
                "name_city:a-landscaping-llc:kirtland",
            ],
        )

    def test_hard_icp_gate_skips_franchise_but_keeps_strong_no_site_lead(self) -> None:
        franchise = normalize_lead(
            {
                "business_name": "Mr Roof Cleveland",
                "city": "Cleveland",
                "rating": "4.8",
                "reviews": "120",
                "website_url": "https://example.com",
            },
            fallback_vertical="roofing",
            source_batch="round1",
        )
        no_site = normalize_lead(
            {
                "business_name": "Ron & Sons Landscaping",
                "city": "East Cleveland",
                "rating": "4.9",
                "reviews": "19",
                "website_url": "",
            },
            fallback_vertical="hardscaping",
            source_batch="round1",
        )

        self.assertTrue(hard_icp_gate(franchise).should_skip)
        self.assertIn("franchise_or_chain", hard_icp_gate(franchise).reasons)
        self.assertFalse(hard_icp_gate(no_site).should_skip)

    def test_hard_icp_gate_uses_per_vertical_review_bounds(self) -> None:
        low_review_with_site = normalize_lead(
            {
                "business_name": "Five Star Detailing",
                "city": "Cleveland",
                "rating": "4.9",
                "reviews": "5",
                "website_url": "https://example.com",
            },
            fallback_vertical="detailing",
            source_batch="round1",
        )
        low_review_no_site = normalize_lead(
            {
                "business_name": "No Site Detailing",
                "city": "Cleveland",
                "rating": "4.9",
                "reviews": "5",
                "website_url": "",
            },
            fallback_vertical="detailing",
            source_batch="round1",
        )
        mature_garage_doors = normalize_lead(
            {
                "business_name": "Lake County Garage Door Repair",
                "city": "Cleveland",
                "rating": "4.9",
                "reviews": "200",
                "website_url": "https://example.com",
                "source_query": "garage door repair Cleveland OH",
            },
            fallback_vertical="garage_doors",
            source_batch="round1",
        )
        mature_detailing = normalize_lead(
            {
                "business_name": "Cleveland Ceramic Coating",
                "city": "Cleveland",
                "rating": "4.9",
                "reviews": "200",
                "website_url": "https://example.com",
            },
            fallback_vertical="detailing",
            source_batch="round1",
        )

        with_site_gate = hard_icp_gate(low_review_with_site)
        self.assertTrue(with_site_gate.should_skip)
        self.assertIn("too_few_reviews", with_site_gate.reasons)
        self.assertFalse(hard_icp_gate(low_review_no_site).should_skip)
        self.assertFalse(hard_icp_gate(mature_garage_doors).should_skip)

        detailing_gate = hard_icp_gate(mature_detailing)
        self.assertTrue(detailing_gate.should_skip)
        self.assertIn("high_review_count", detailing_gate.reasons)

    def test_evidence_profile_exposes_required_fields_and_evidence_items(self) -> None:
        enriched = {
            "google": {
                "business_name": "A+ Landscaping LLC",
                "phone": "(440) 637-5848",
                "rating": "4.9",
                "reviews": "130",
                "website_url": "https://clienthub.getjobber.com/public/request",
            },
            "website": {
                "site_status": "Loads",
                "site_issue_flags": ["non_standard_site_or_booking_url", "no_email_detected"],
                "final_url": "https://clienthub.getjobber.com/public/request",
                "homepage": {"url": "https://clienthub.getjobber.com/public/request", "text": "Client Hub"},
                "pages": {},
                "emails_found": [],
                "phones_found": [],
                "social_links": [],
                "forms_found": True,
                "cta_terms": [],
                "high_ticket_terms": [],
                "combined_text": "Client Hub",
            },
            "screenshots": {"desktop": "screenshots/a-desktop.png", "mobile": "screenshots/a-mobile.png"},
            "derived": {"phone_consistency": "google_only", "non_standard_site_or_booking": True},
        }

        profile = build_evidence_profile(enriched)

        self.assertEqual(profile["site_status"], "jobber_only")
        self.assertEqual(profile["mobile_status"], "unknown")
        self.assertTrue(profile["has_contact_form"])
        self.assertFalse(profile["email_visible"])
        self.assertIn("Jobber", profile["third_party_tools"])
        self.assertGreaterEqual(len(profile["evidence_items"]), 3)
        self.assertTrue(any(item["field"] == "third_party_tools" for item in profile["evidence_items"]))

    def test_static_iosue_like_estimate_form_is_classified_strong(self) -> None:
        html = """
        <html>
          <body>
            <h1>Let's Discuss Your Property</h1>
            <p>Contact us to schedule your estimate appointment.</p>
            <form action="/request-estimate" method="post">
              <label>First Name <input name="first_name"></label>
              <label>Last Name <input name="last_name"></label>
              <label>Company <input name="company"></label>
              <label>Email <input type="email" name="email"></label>
              <label>Phone <input type="tel" name="phone"></label>
              <label>Property Address <input name="address"></label>
              <label>Service Details <textarea name="service_details"></textarea></label>
              <label>Upload project photos <input type="file" name="images"></label>
              <div class="g-recaptcha" data-sitekey="abc123"></div>
              <button type="submit">Submit Request</button>
            </form>
            <a href="https://facebook.com/example">Facebook</a>
          </body>
        </html>
        """

        signals = detect_form_signals(html, page_url="https://example.com/contact")

        self.assertEqual(signals["contact_form_detected"], "Yes")
        self.assertEqual(signals["estimate_form_detected"], "Yes")
        self.assertEqual(signals["form_strength"], "Strong")
        self.assertEqual(signals["form_detection_method"], "static_html")
        self.assertIn("address", signals["form_fields"])
        self.assertIn("details", signals["form_fields"])
        self.assertTrue(signals["has_recaptcha"])

    def test_iframe_provider_form_is_detected(self) -> None:
        html = """
        <html><body>
          <h1>Request a Free Estimate</h1>
          <iframe src="https://form.jotform.com/123456789"></iframe>
          <p>This Jotform request form is protected by captcha.</p>
        </body></html>
        """

        signals = detect_form_signals(html, page_url="https://example.com/free-estimate")

        self.assertEqual(signals["contact_form_detected"], "Yes")
        self.assertEqual(signals["estimate_form_detected"], "Yes")
        self.assertEqual(signals["form_detection_method"], "embedded_provider")
        self.assertGreaterEqual(signals["form_strength_rank"], 1)
        self.assertIn("jotform", signals["provider_terms"])

    def test_scrape_site_checks_candidate_estimate_contact_pages(self) -> None:
        homepage = """
        <html><body>
          <a href="/services">Services</a>
          <p>Schedule an Estimate for patios and retaining walls.</p>
        </body></html>
        """
        estimate_page = """
        <html><body>
          <h1>Let's Discuss Your Property</h1>
          <p>Complete the request form below to schedule your estimate appointment.</p>
          <form>
            <input name="first_name">
            <input name="last_name">
            <input type="email" name="email">
            <input type="tel" name="phone">
            <input name="address">
            <textarea name="service_details"></textarea>
            <input type="file" name="image_upload">
            <div class="recaptcha"></div>
            <button type="submit">Submit</button>
          </form>
        </body></html>
        """

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            if url == "https://example.com":
                return {"ok": True, "status_code": 200, "final_url": url, "html": homepage, "error": ""}
            if url == "https://example.com/request-estimate":
                return {"ok": True, "status_code": 200, "final_url": url, "html": estimate_page, "error": ""}
            return {"ok": False, "status_code": 404, "final_url": url, "html": "", "error": ""}

        with patch("enrich_leads.fetch_url", side_effect=fake_fetch):
            site = scrape_site("https://example.com")

        self.assertEqual(site["contact_form_detected"], "Yes")
        self.assertEqual(site["estimate_form_detected"], "Yes")
        self.assertEqual(site["form_strength"], "Strong")
        self.assertEqual(site["form_detection_method"], "candidate_page")
        self.assertNotIn("no_form_detected", site["site_issue_flags"])
        self.assertIn("estimate", site["pages"])

    def test_scrape_site_stops_deeper_probing_after_per_lead_ceiling(self) -> None:
        homepage = """
        <html><body>
          <a href="/request-estimate">Request Estimate</a>
          <p>Paint correction ceramic coating request estimate gallery reviews.</p>
        </body></html>
        """
        clock_values = [0.0, 46.0]
        calls = []

        def fake_monotonic() -> float:
            if len(clock_values) > 1:
                return clock_values.pop(0)
            return clock_values[0]

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            calls.append((url, timeout))
            return {"ok": True, "status_code": 200, "final_url": url, "html": homepage, "error": ""}

        with patch("enrich_leads.fetch_url", side_effect=fake_fetch), patch(
            "enrich_leads.time.monotonic", side_effect=fake_monotonic
        ):
            site = scrape_site("https://example.com")

        self.assertEqual(calls, [("https://example.com", 12)])
        self.assertEqual(site["pages"], {})
        self.assertIn("per_lead_enrichment_timeout", site["site_issue_flags"])
        self.assertIn("rendered_check_needed", site["site_issue_flags"])
        self.assertEqual(site["visual_review_needed"], "Yes")
        self.assertGreaterEqual(site["enrichment_elapsed_seconds"], 45)

    def test_candidate_page_fetch_uses_short_timeout(self) -> None:
        homepage = """
        <html><body>
          <a href="/request-estimate">Request Estimate</a>
          <p>Paint correction ceramic coating request estimate gallery reviews.</p>
        </body></html>
        """

        def fake_fetch(url: str, timeout: int = 15) -> dict[str, object]:
            if url == "https://example.com":
                return {"ok": True, "status_code": 200, "final_url": url, "html": homepage, "error": ""}
            return {"ok": False, "status_code": 404, "final_url": url, "html": "", "error": ""}

        with patch("enrich_leads.fetch_url", side_effect=fake_fetch) as fetch:
            scrape_site("https://example.com")

        self.assertEqual(fetch.call_args_list[0].kwargs.get("timeout"), 12)
        self.assertEqual(fetch.call_args_list[1].kwargs.get("timeout"), 8)

    def test_strong_estimate_form_blocks_no_pain_estimate_funnel_sendability(self) -> None:
        enriched = {
            "google": self._google_fixture(),
            "evidence": {
                "site_status": "okay",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "form_detection_method": "candidate_page",
                "has_contact_form": True,
                "has_estimate_cta": True,
                "raw_flags": [],
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 130 reviews"},
                    {"id": "E2", "field": "estimate_form_detected", "value": "Yes"},
                    {"id": "E3", "field": "form_strength", "value": "Strong"},
                ],
            },
        }
        raw_score = self._manual_score(
            subscores={
                "icp_fit": 18,
                "reputation_demand_proof": 14,
                "web_conversion_pain": 22,
                "mockup_leverage": 12,
                "reachability": 8,
                "offer_angle_clarity": 9,
                "evidence_confidence": 5,
            },
            total_score=88,
            grade="A",
            evidence_refs=["E1", "E2", "E3"],
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong reviews.", "evidence_refs": ["E1"]},
                {"claim": "Your site already has a strong estimate form.", "evidence_refs": ["E2", "E3"]},
            ],
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Lead Priority"], "A")
        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Missing concrete conversion issue", row["Send Blockers"])
        self.assertEqual(row["Packet Eligible"], "")
        self.assertIn("Blocked by sendability", row["Packet Eligibility Reason"])
        self.assertNotIn("missing contact form", row["Safe Outreach Claims"].lower())
        self.assertNotIn("no contact form", row["Safe Outreach Claims"].lower())

    def test_strong_estimate_form_with_premium_perception_gap_can_be_sendable(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "reviews": "18",
            },
            "evidence": {
                "site_status": "okay",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "form_detection_method": "candidate_page",
                "cta_strength": "Strong",
                "visible_pain_score": 36,
                "visible_pain_reasons": ["Premium hardscape work is not reflected by the owned website"],
                "visible_pain_categories": ["premium_perception_gap"],
                "send_trigger": "Premium hardscape work is not reflected by the owned website",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 18 reviews"},
                    {"id": "E2", "field": "form_strength", "value": "Strong"},
                    {"id": "E3", "field": "visible_pain_categories", "value": "premium_perception_gap"},
                    {
                        "id": "E4",
                        "field": "send_trigger",
                        "value": "Premium hardscape work is not reflected by the owned website",
                    },
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Premium Perception Gap",
            evidence_refs=["E1", "E3", "E4"],
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong review proof.", "evidence_refs": ["E1"]},
                {
                    "claim": "The owned website does not reflect the premium hardscape work.",
                    "evidence_refs": ["E3", "E4"],
                },
            ],
            opener=(
                "Your reviews look strong, but the site does not make the premium hardscape work obvious. "
                "Want me to send a cleaner direction?"
            ),
            visible_pain_score=36,
            visible_pain_reasons=["Premium hardscape work is not reflected by the owned website"],
            send_trigger="Premium hardscape work is not reflected by the owned website",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Offer Fit"], "Premium Perception Gap")
        self.assertEqual(row["Sendable"], "Yes")
        self.assertNotIn("Strong estimate form exists", row["Send Blockers"])
        self.assertNotIn("Missing concrete conversion issue", row["Send Blockers"])

    def test_strong_estimate_form_with_weak_project_gallery_can_be_sendable(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "reviews": "22",
            },
            "evidence": {
                "site_status": "okay",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "form_detection_method": "candidate_page",
                "visible_pain_score": 34,
                "visible_pain_reasons": ["Project photos are buried or weak for high-ticket hardscaping"],
                "visible_pain_categories": ["portfolio_presentation_pain"],
                "send_trigger": "Project photos are buried or weak for high-ticket hardscaping",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 22 reviews"},
                    {"id": "E2", "field": "form_strength", "value": "Strong"},
                    {"id": "E3", "field": "visible_pain_categories", "value": "portfolio_presentation_pain"},
                    {
                        "id": "E4",
                        "field": "send_trigger",
                        "value": "Project photos are buried or weak for high-ticket hardscaping",
                    },
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Portfolio Rebuild",
            evidence_refs=["E1", "E3", "E4"],
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong review proof.", "evidence_refs": ["E1"]},
                {"claim": "The project gallery is weak for high-ticket work.", "evidence_refs": ["E3", "E4"]},
            ],
            opener="Your reviews look strong, but the project gallery undersells the hardscape work.",
            visible_pain_score=34,
            visible_pain_reasons=["Project photos are buried or weak for high-ticket hardscaping"],
            send_trigger="Project photos are buried or weak for high-ticket hardscaping",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Offer Fit"], "Portfolio Rebuild")
        self.assertEqual(row["Sendable"], "Yes")
        self.assertNotIn("Strong estimate form exists", row["Send Blockers"])

    def test_strong_estimate_form_cannot_use_missing_form_claim(self) -> None:
        enriched = {
            "google": self._google_fixture(),
            "evidence": {
                "site_status": "okay",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "visible_pain_score": 35,
                "visible_pain_reasons": ["No estimate form detected"],
                "visible_pain_categories": ["estimate_path_pain"],
                "send_trigger": "No estimate form detected",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 130 reviews"},
                    {"id": "E2", "field": "form_strength", "value": "Strong"},
                    {"id": "E3", "field": "send_trigger", "value": "No estimate form detected"},
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Website Cleanup",
            evidence_refs=["E1", "E2", "E3"],
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong reviews.", "evidence_refs": ["E1"]},
                {"claim": "There is no estimate form on the site.", "evidence_refs": ["E3"]},
            ],
            opener="Your reviews are strong, but there is no estimate form on the site.",
            visible_pain_score=35,
            visible_pain_reasons=["No estimate form detected"],
            send_trigger="No estimate form detected",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Strong estimate form conflicts with missing-form claim", row["Send Blockers"])

    def test_estimate_funnel_blocked_when_strong_form_has_only_non_estimate_pain(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "reviews": "18",
            },
            "evidence": {
                "site_status": "okay",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "visible_pain_score": 36,
                "visible_pain_reasons": ["Premium hardscape work is not reflected by the owned website"],
                "visible_pain_categories": ["premium_perception_gap"],
                "send_trigger": "Premium hardscape work is not reflected by the owned website",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 18 reviews"},
                    {"id": "E2", "field": "form_strength", "value": "Strong"},
                    {"id": "E3", "field": "visible_pain_categories", "value": "premium_perception_gap"},
                    {
                        "id": "E4",
                        "field": "send_trigger",
                        "value": "Premium hardscape work is not reflected by the owned website",
                    },
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Estimate Funnel",
            evidence_refs=["E1", "E3", "E4"],
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong review proof.", "evidence_refs": ["E1"]},
                {
                    "claim": "The owned website does not reflect the premium hardscape work.",
                    "evidence_refs": ["E3", "E4"],
                },
            ],
            opener="Your reviews look strong, but the site does not make the premium hardscape work obvious.",
            visible_pain_score=36,
            visible_pain_reasons=["Premium hardscape work is not reflected by the owned website"],
            send_trigger="Premium hardscape work is not reflected by the owned website",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Estimate Funnel blocked by strong estimate form without estimate-path weakness", row["Send Blockers"])

    def test_evidence_profile_derives_visible_pain_fields_and_send_trigger(self) -> None:
        profile = build_evidence_profile(
            {
                "google": self._google_fixture(),
                "website": {
                    "site_status": "Loads",
                    "final_url": "https://example.com",
                    "homepage": {"url": "https://example.com", "text": "Call us for patio estimates."},
                    "site_issue_flags": ["no_form_detected", "no_clear_cta_detected"],
                    "contact_form_detected": "No",
                    "estimate_form_detected": "No",
                    "form_strength": "None",
                    "cta_strength": "Weak",
                    "emails_found": ["office@example.com"],
                    "phones_found": ["(440) 637-5848"],
                },
            }
        )

        self.assertGreater(profile["visible_pain_score"], 0)
        self.assertIn("No estimate form detected", profile["visible_pain_reasons"])
        self.assertEqual(profile["send_trigger"], "No estimate form detected")
        self.assertTrue(any(item["field"] == "send_trigger" for item in profile["evidence_items"]))

    def test_evidence_profile_classifies_non_form_visible_pain_categories(self) -> None:
        profile = build_evidence_profile(
            {
                "google": self._google_fixture(),
                "website": {
                    "site_status": "Loads",
                    "final_url": "https://example.com",
                    "homepage": {
                        "url": "https://example.com",
                        "text": "Schedule an Estimate hardscape patio retaining wall landscape design",
                    },
                    "site_issue_flags": [
                        "premium_perception_gap",
                        "weak_project_gallery",
                        "low_visible_trust",
                    ],
                    "contact_form_detected": "Yes",
                    "estimate_form_detected": "Yes",
                    "form_strength": "Strong",
                    "cta_strength": "Strong",
                    "emails_found": ["office@example.com"],
                    "phones_found": ["(440) 637-5848"],
                    "high_ticket_terms": ["hardscape", "patio", "retaining wall"],
                },
            }
        )

        self.assertIn("premium_perception_gap", profile["visible_pain_categories"])
        self.assertIn("portfolio_presentation_pain", profile["visible_pain_categories"])
        self.assertIn("trust_proof_pain", profile["visible_pain_categories"])
        self.assertNotIn("No estimate form detected", profile["visible_pain_reasons"])
        self.assertEqual(profile["send_trigger"], "Premium work is undersold by the owned website")

    def test_deterministic_scoring_does_not_make_strong_form_site_a_sendable_estimate_funnel(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "business_name": "Iosue Style Landscape Concepts",
                "rating": "4.7",
                "reviews": "39",
            },
            "website": {
                "site_status": "Loads",
                "site_issue_flags": [],
                "final_url": "https://example.com",
                "homepage": {"url": "https://example.com", "text": "Schedule an Estimate hardscape patio retaining wall"},
                "pages": {},
                "emails_found": ["office@example.com"],
                "phones_found": ["(440) 637-5848"],
                "social_links": ["https://facebook.com/example"],
                "forms_found": True,
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "form_detection_method": "candidate_page",
                "cta_terms": ["schedule", "request estimate"],
                "cta_strength": "Strong",
                "high_ticket_terms": ["hardscape", "patio", "retaining wall", "landscape design"],
                "generic_copy_terms": [],
                "combined_text": "Schedule an Estimate hardscape patio retaining wall landscape design gallery portfolio",
            },
            "derived": {
                "phone_consistency": "matched",
                "has_email": True,
                "has_form": True,
                "has_cta": True,
                "has_social_links": True,
                "has_high_ticket_terms": True,
                "non_standard_site_or_booking": False,
            },
            "screenshots": {},
        }

        row = score_record(enriched, "hardscaping")

        self.assertNotEqual(row["Lead Priority"], "A")
        self.assertNotEqual(row["Offer Fit"], "Estimate Funnel")
        self.assertEqual(row["Sendable"], "No")
        self.assertEqual(row["Packet Eligible"], "")
        self.assertEqual(row["Visible Pain Score"], 0)
        self.assertEqual(row["Send Trigger"], "")
        self.assertNotIn("no form detected", row["Evidence Notes"].lower())

    def test_no_site_broken_and_social_only_can_be_sendable_with_demand_proof(self) -> None:
        for status, trigger in [
            ("No Website", "No owned website found"),
            ("Broken Website", "Website appears broken"),
            ("Social Only", "Social-only web presence"),
        ]:
            with self.subTest(status=status):
                row = score_record(
                    {
                        "google": {
                            **self._google_fixture(),
                            "business_name": f"{status} Hardscapes",
                            "rating": "4.8",
                            "reviews": "18",
                        },
                        "website": {
                            "site_status": status,
                            "site_issue_flags": [],
                            "pages": {},
                            "emails_found": [],
                            "phones_found": [],
                            "forms_found": False,
                            "cta_terms": [],
                            "combined_text": "hardscape patio retaining wall outdoor living",
                            "high_ticket_terms": ["hardscape", "patio", "retaining wall"],
                        },
                        "derived": {"has_high_ticket_terms": True},
                        "screenshots": {},
                        "buyer_intent": self._buyer_intent_fixture(),
                    },
                    "hardscaping",
                )

                self.assertEqual(row["Lead Priority"], "A")
                self.assertEqual(row["Sendable"], "Yes")
                self.assertEqual(row["Packet Eligible"], "Yes")
                self.assertGreaterEqual(int(row["Visible Pain Score"]), 30)
                self.assertEqual(row["Send Trigger"], trigger)

    def test_manual_score_validation_requires_evidence_references_and_merges_card_fields(self) -> None:
        enriched = {
            "google": {
                "business_name": "A+ Landscaping LLC",
                "city": "Kirtland",
                "phone": "(440) 637-5848",
                "website_url": "https://clienthub.getjobber.com/public/request",
                "google_maps_url": "https://maps.google.com/?cid=123",
                "rating": "4.9",
                "reviews": "130",
                "place_id": "ChIJ123",
            },
            "evidence": {
                "site_status": "jobber_only",
                "screenshots": {"desktop": "desktop.png", "mobile": "mobile.png"},
                "evidence_items": [
                    {"id": "ev-google-rating", "field": "google_rating", "value": "4.9 stars / 130 reviews"},
                    {"id": "ev-third-party-jobber", "field": "third_party_tools", "value": "Jobber"},
                ],
            },
        }
        raw_score = {
            "subscores": {
                "icp_fit": 18,
                "reputation_demand_proof": 14,
                "web_conversion_pain": 24,
                "mockup_leverage": 14,
                "reachability": 7,
                "offer_angle_clarity": 9,
                "evidence_confidence": 4,
            },
            "confidence": "medium",
            "hard_disqualifiers": [],
            "best_angle": "Social/Jobber Escape",
            "recommended_action": "Build mockup then SMS owner",
            "why_this_lead": ["Strong reviews", "Jobber-only presence"],
            "why_this_might_be_wrong": ["Owner may be referral-driven"],
            "opener": "Want me to send a cleaner estimate-page mockup?",
            "evidence_refs": ["ev-google-rating", "ev-third-party-jobber"],
            "safe_outreach_claims": [
                {"claim": "Your Google profile shows strong review proof.", "evidence_refs": ["ev-google-rating"]},
                {"claim": "Your visible web presence routes through Jobber.", "evidence_refs": ["ev-third-party-jobber"]},
            ],
            "unsafe_claims": ["Do not claim they are losing jobs."],
            "airtable_fields": {"Status": "New"},
        }

        merged = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")
        card = build_lead_card(merged)

        self.assertEqual(merged["Lead Priority"], "A")
        self.assertEqual(merged["AI Confidence"], "medium")
        self.assertEqual(merged["Recommended Action"], "Build mockup then SMS owner")
        self.assertEqual(merged["Send Trigger"], "Jobber-only web presence")
        self.assertGreater(int(merged["Visible Pain Score"]), 0)
        self.assertIn("Jobber-only web presence", merged["Visible Pain Reasons"])
        self.assertEqual(card["grade"], "A")
        self.assertEqual(card["best_angle"], "Social/Jobber Escape")
        self.assertEqual(card["airtable_fields"]["Lead Priority"], "A")
        self.assertEqual(card["airtable_fields"]["Send Trigger"], "Jobber-only web presence")
        self.assertTrue(card["sendable"])
        self.assertTrue(card["minimum_evidence_passed"])
        self.assertEqual(card["evidence_refs"], ["ev-google-rating", "ev-third-party-jobber"])

    def test_manual_score_file_loader_rejects_missing_evidence_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            score_path = Path(tmp) / "score.json"
            score_path.write_text(
                json.dumps(
                    {
                        "subscores": {
                            "icp_fit": 10,
                            "reputation_demand_proof": 10,
                            "web_conversion_pain": 10,
                            "mockup_leverage": 10,
                            "reachability": 10,
                            "offer_angle_clarity": 10,
                            "evidence_confidence": 5,
                        },
                        "confidence": "high",
                        "hard_disqualifiers": [],
                        "best_angle": "Website Rebuild",
                        "recommended_action": "human review",
                        "why_this_lead": ["Looks weak"],
                        "why_this_might_be_wrong": [],
                        "opener": "",
                        "evidence_refs": ["missing-id"],
                        "safe_outreach_claims": [{"claim": "Missing claim.", "evidence_refs": ["missing-id"]}],
                        "unsafe_claims": ["Do not use unsupported claims."],
                        "airtable_fields": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_manual_score(score_path, allowed_evidence_ids={"ev-known"})

    def test_llm_prompt_and_queue_use_manual_web_workflow(self) -> None:
        enriched = {
            "google": {
                "business_name": "A+ Landscaping LLC",
                "place_id": "ChIJ123",
                "vertical": "service",
                "source_query": "hardscaping contractor Cleveland OH",
            },
            "evidence": {
                "site_status": "jobber_only",
                "screenshots": {"desktop": "desktop.png", "mobile": "mobile.png"},
                "evidence_items": [{"id": "ev-google-rating", "field": "google_rating", "value": "4.9 / 130"}],
            },
        }

        prompt = build_llm_prompt(enriched, fallback_vertical="hardscaping")
        self.assertIn("Response must be one valid JSON object only.", prompt)
        self.assertIn("First character must be { and last character must be }.", prompt)
        self.assertIn("No markdown fences.", prompt)
        self.assertIn("No commentary.", prompt)
        self.assertIn("No reasoning outside JSON.", prompt)
        self.assertIn("No \"Thought for X seconds\" text.", prompt)
        self.assertIn("ICP Fit / 20", prompt)
        self.assertIn('"total_score": 0', prompt)
        self.assertIn('"grade": "A|B|C|Skip"', prompt)
        self.assertIn("total_score must equal the sum of all subscores.", prompt)
        self.assertIn("A = 80-100", prompt)
        self.assertIn("confidence must be one of low, medium, high.", prompt)
        self.assertIn(
            "Missing site plus strong reviews can be an A lead only when there is a concrete conversion gap and enough evidence.",
            prompt,
        )
        self.assertIn("No specific observation = no send.", prompt)
        self.assertIn("Do not score A/sendable just because the business is reputable.", prompt)
        self.assertIn(
            "mature plus an obvious broken/missing estimate path can still be high value when backed by money intent or very strong conversion-pain evidence.",
            prompt,
        )
        self.assertIn("A strong estimate form does not globally disqualify a lead.", prompt)
        self.assertIn("It only blocks missing-form, no-contact-path, and no-estimate-path claims.", prompt)
        self.assertIn("Do not use Estimate Funnel if the form is strong unless the actual estimate path is still visibly weak.", prompt)
        self.assertIn("Static fetch failure alone does not prove the website is broken.", prompt)
        self.assertIn("If rendered_site_status is Loaded, do not claim the website is broken.", prompt)
        self.assertIn("No screenshot/rendered evidence means no strong visual claims.", prompt)
        self.assertIn("Premium Perception Gap", prompt)
        self.assertIn("Portfolio Rebuild", prompt)
        self.assertIn("Mobile Quote Path Cleanup", prompt)
        self.assertIn('"send_trigger": ""', prompt)
        self.assertIn('"visible_pain_score": 0', prompt)
        self.assertIn('"visible_pain_reasons": []', prompt)
        self.assertIn('"safe_outreach_claims"', prompt)
        self.assertIn('"unsafe_claims"', prompt)
        self.assertIn("Build/send a no-site launch mockup", prompt)
        self.assertIn("Must mention at least one specific evidence-backed observation.", prompt)
        self.assertIn('"vertical": "hardscaping"', prompt)
        self.assertIn('"review_demand_band":', prompt)
        self.assertIn("ev-google-rating", prompt)

        with tempfile.TemporaryDirectory() as tmp:
            paths = write_review_queue([enriched], Path(tmp), fallback_vertical="hardscaping")
            self.assertEqual(len(paths), 1)
            self.assertTrue(paths[0].read_text(encoding="utf-8").startswith("# Manual LLM Review"))

    def test_llm_prompt_infers_vertical_when_cli_vertical_is_generic(self) -> None:
        enriched = {
            "google": {
                "business_name": "Barski Hardscaping",
                "place_id": "ChIJ456",
                "vertical": "service",
                "category": "landscaper",
                "source_query": "patio paver contractor Cleveland OH",
            },
            "evidence": {"evidence_items": [{"id": "E1", "field": "google_rating", "value": "4.9 / 24"}]},
        }

        prompt = build_llm_prompt(enriched, fallback_vertical="service")

        self.assertIn('"vertical": "hardscaping"', prompt)

    def test_llm_batch_prompt_contains_multiple_leads_and_score_files(self) -> None:
        first = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
            "website": {"combined_text": "Homepage text for A+ Landscaping."},
        }
        second = {
            "google": {
                **self._google_fixture(),
                "business_name": "Barski Hardscaping",
                "phone": "(440) 555-0101",
                "place_id": "ChIJ456",
            },
            "evidence": self._evidence_fixture(site_status="jobber_only"),
            "website": {"combined_text": "Homepage text for Barski Hardscaping."},
        }

        with tempfile.TemporaryDirectory() as tmp:
            batch_paths = write_llm_batches(
                [first, second],
                Path(tmp),
                fallback_vertical="hardscaping",
                batch_size=5,
            )
            prompt = batch_paths[0].prompt_path.read_text(encoding="utf-8")
            expected_files = batch_paths[0].expected_files_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(batch_paths), 1)
        self.assertIn("Response must be one valid JSON object only.", prompt)
        self.assertIn('"batch_id": "batch_001"', prompt)
        self.assertIn('"scores"', prompt)
        self.assertIn('"score_file": "chij123.json"', prompt)
        self.assertIn('"score_file": "chij456.json"', prompt)
        self.assertIn("A+ Landscaping LLC", prompt)
        self.assertIn("Barski Hardscaping", prompt)
        self.assertIn("ICP Fit / 20", prompt)
        self.assertIn(
            "Missing site plus strong reviews can be an A lead only when there is a concrete conversion gap and enough evidence.",
            prompt,
        )
        self.assertIn("No specific observation = no send.", prompt)
        self.assertIn("Do not score A/sendable just because the business is reputable.", prompt)
        self.assertIn(
            "mature plus an obvious broken/missing estimate path can still be high value when backed by money intent or very strong conversion-pain evidence.",
            prompt,
        )
        self.assertIn("A strong estimate form does not globally disqualify a lead.", prompt)
        self.assertIn("Static fetch failure alone does not prove the website is broken.", prompt)
        self.assertIn("If rendered_site_status is Loaded, do not claim the website is broken.", prompt)
        self.assertIn("No screenshot/rendered evidence means no strong visual claims.", prompt)
        self.assertIn("Premium Perception Gap", prompt)
        self.assertIn("Portfolio Rebuild", prompt)
        self.assertIn('"send_trigger": ""', prompt)
        self.assertEqual(expected_files, ["chij123.json", "chij456.json"])

    def test_llm_batch_only_candidates_ranks_visible_pain_before_high_review_decent_site(self) -> None:
        high_review_decent = {
            "google": {
                **self._google_fixture(),
                "business_name": "Polished High Review Hardscapes",
                "reviews": "85",
                "place_id": "ChIJHIGH",
            },
            "website": {
                "site_status": "Loads",
                "site_issue_flags": [],
                "combined_text": "hardscape patio retaining wall request an estimate gallery reviews",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "form_detection_method": "static_html",
                "cta_strength": "Strong",
                "forms_found": True,
                "emails_found": ["office@example.com"],
                "phones_found": ["(440) 637-5848"],
                "high_ticket_terms": ["hardscape", "patio"],
            },
            "derived": {"has_high_ticket_terms": True},
        }
        visible_pain = {
            "google": {
                **self._google_fixture(),
                "business_name": "Old School Patio Co",
                "reviews": "12",
                "place_id": "ChIJPAIN",
            },
            "website": {
                "site_status": "No Website",
                "site_issue_flags": [],
                "combined_text": "hardscape patio retaining wall",
                "high_ticket_terms": ["hardscape", "patio"],
            },
            "derived": {"has_high_ticket_terms": True},
        }

        with tempfile.TemporaryDirectory() as tmp:
            batch_paths = write_llm_batches(
                [high_review_decent, visible_pain],
                Path(tmp),
                fallback_vertical="hardscaping",
                batch_size=2,
                only_candidates=True,
            )
            expected_files = batch_paths[0].expected_files_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(expected_files, ["chijpain.json", "chijhigh.json"])

    def test_llm_batch_ranking_does_not_penalize_strong_form_when_non_form_pain_exists(self) -> None:
        weak_form_estimate_pain = {
            "google": {
                **self._google_fixture(),
                "business_name": "Weak Form Patio Co",
                "reviews": "12",
                "place_id": "ChIJWEAKFORM",
            },
            "website": {
                "site_status": "Loads",
                "site_issue_flags": ["no_form_detected", "no_clear_cta_detected"],
                "combined_text": "hardscape patio retaining wall",
                "contact_form_detected": "No",
                "estimate_form_detected": "No",
                "form_strength": "None",
                "cta_strength": "Weak",
                "emails_found": ["office@example.com"],
                "phones_found": ["(440) 637-5848"],
                "high_ticket_terms": ["hardscape", "patio"],
            },
            "derived": {"has_high_ticket_terms": True},
        }
        strong_form_premium_pain = {
            "google": {
                **self._google_fixture(),
                "business_name": "Premium Patio Co",
                "reviews": "12",
                "place_id": "ChIJPREMIUM",
            },
            "website": {
                "site_status": "Loads",
                "site_issue_flags": ["premium_perception_gap"],
                "combined_text": "request an estimate hardscape patio retaining wall outdoor living",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "form_detection_method": "candidate_page",
                "cta_strength": "Strong",
                "forms_found": True,
                "emails_found": ["office@example.com"],
                "phones_found": ["(440) 637-5848"],
                "high_ticket_terms": ["hardscape", "patio", "retaining wall"],
            },
            "derived": {"has_high_ticket_terms": True},
        }

        with tempfile.TemporaryDirectory() as tmp:
            batch_paths = write_llm_batches(
                [weak_form_estimate_pain, strong_form_premium_pain],
                Path(tmp),
                fallback_vertical="hardscaping",
                batch_size=2,
                only_candidates=True,
            )
            expected_files = batch_paths[0].expected_files_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(expected_files, ["chijpremium.json", "chijweakform.json"])

    def test_llm_score_bundle_importer_writes_individual_score_json_files(self) -> None:
        score_one = self._manual_score(total_score=75, grade="B")
        score_two = self._manual_score(
            total_score=86,
            grade="A",
            subscores={
                "icp_fit": 18,
                "reputation_demand_proof": 14,
                "web_conversion_pain": 22,
                "mockup_leverage": 12,
                "reachability": 8,
                "offer_angle_clarity": 8,
                "evidence_confidence": 4,
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_path = tmp_path / "bundle.json"
            scores_dir = tmp_path / "scores"
            bundle_path.write_text(
                json.dumps(
                    {
                        "batch_id": "batch_001",
                        "scores": [
                            {"score_file": "chij123.json", "score": score_one},
                            {"score_file": "chij456.json", "score": score_two},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = import_score_bundle(bundle_path, scores_dir)

            first = json.loads((scores_dir / "chij123.json").read_text(encoding="utf-8"))
            second = json.loads((scores_dir / "chij456.json").read_text(encoding="utf-8"))

        self.assertEqual(result.scores_imported, 2)
        self.assertEqual(result.files_written, 2)
        self.assertEqual(result.validation_failures, [])
        self.assertEqual(first["grade"], "B")
        self.assertEqual(first["total_score"], 75)
        self.assertEqual(second["grade"], "A")
        self.assertEqual(second["total_score"], 86)

    def test_llm_score_bundle_importer_rejects_invalid_json_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_path = tmp_path / "bundle.json"
            scores_dir = tmp_path / "scores"
            bundle_path.write_text("{bad json", encoding="utf-8")

            with self.assertRaises(ValueError) as caught:
                import_score_bundle(bundle_path, scores_dir)

        self.assertIn("Invalid JSON bundle", str(caught.exception))
        self.assertFalse(scores_dir.exists())

    def test_llm_score_bundle_importer_rejects_empty_scores_array(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_path = tmp_path / "bundle.json"
            scores_dir = tmp_path / "scores"
            bundle_path.write_text(json.dumps({"batch_id": "batch_001", "scores": []}), encoding="utf-8")

            with self.assertRaises(ValueError) as caught:
                import_score_bundle(bundle_path, scores_dir)

        self.assertIn("scores array must contain at least one score", str(caught.exception))
        self.assertFalse(scores_dir.exists())

    def test_llm_score_bundle_importer_rejects_score_without_safe_outreach_claims(self) -> None:
        score = self._manual_score(total_score=75, grade="B")
        score.pop("safe_outreach_claims")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_path = tmp_path / "bundle.json"
            scores_dir = tmp_path / "scores"
            bundle_path.write_text(
                json.dumps({"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": score}]}),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as caught:
                import_score_bundle(bundle_path, scores_dir)

        self.assertIn("safe_outreach_claims", str(caught.exception))
        self.assertFalse((scores_dir / "chij123.json").exists())

    def test_llm_score_bundle_importer_rejects_score_file_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_path = tmp_path / "bundle.json"
            scores_dir = tmp_path / "scores"
            bundle_path.write_text(
                json.dumps(
                    {
                        "batch_id": "batch_001",
                        "scores": [
                            {"score_file": "../../bad.json", "score": self._manual_score(total_score=75, grade="B")}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as caught:
                import_score_bundle(bundle_path, scores_dir)

        self.assertIn("unsafe score_file", str(caught.exception))
        self.assertFalse((scores_dir / "bad.json").exists())

    def test_llm_client_posts_anthropic_message_and_parses_score_bundle(self) -> None:
        from lead_machine.llm_client import score_batch

        score = self._manual_score(total_score=75, grade="B")
        response = {
            "content": [{"type": "text", "text": json.dumps({"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": score}]})}],
            "usage": {"input_tokens": 123, "output_tokens": 45},
        }

        class FakeResponse:
            status_code = 200
            text = json.dumps(response)

            def json(self) -> dict:
                return response

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
            with patch("lead_machine.llm_client.requests.post", return_value=FakeResponse()) as post:
                result = score_batch("score this batch", model="claude-sonnet-5", max_tokens=4096)

        self.assertEqual(result["bundle"]["batch_id"], "batch_001")
        self.assertEqual(result["bundle"]["scores"][0]["score_file"], "chij123.json")
        self.assertEqual(result["usage"]["input_tokens"], 123)
        self.assertEqual(result["usage"]["output_tokens"], 45)
        self.assertEqual(result["usage"]["total_tokens"], 168)
        _, kwargs = post.call_args
        self.assertEqual(kwargs["headers"]["x-api-key"], "test-key")
        self.assertEqual(kwargs["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(kwargs["json"]["model"], "claude-sonnet-5")
        self.assertEqual(kwargs["json"]["max_tokens"], 4096)
        self.assertEqual(kwargs["json"]["messages"][0]["content"], "score this batch")

    def test_llm_client_uses_configured_connect_and_read_timeout_tuple(self) -> None:
        from lead_machine.llm_client import score_batch

        score = self._manual_score(total_score=75, grade="B")
        response = {
            "content": [{"type": "text", "text": json.dumps({"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": score}]})}],
            "usage": {"input_tokens": 123, "output_tokens": 45},
        }

        class FakeResponse:
            status_code = 200
            text = json.dumps(response)

            def json(self) -> dict:
                return response

        env = {
            "ANTHROPIC_API_KEY": "test-key",
            "ANTHROPIC_CONNECT_TIMEOUT_SECONDS": "7",
            "ANTHROPIC_READ_TIMEOUT_SECONDS": "88",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch("lead_machine.llm_client.requests.post", return_value=FakeResponse()) as post:
                score_batch("score this batch", model="claude-sonnet-5", max_tokens=4096)

        _, kwargs = post.call_args
        self.assertEqual(kwargs["timeout"], (7, 88))

    def test_llm_client_retries_timeout_up_to_max_retries(self) -> None:
        import requests
        from lead_machine.llm_client import score_batch

        score = self._manual_score(total_score=75, grade="B")
        response = {
            "content": [{"type": "text", "text": json.dumps({"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": score}]})}],
            "usage": {"input_tokens": 123, "output_tokens": 45},
        }

        class FakeResponse:
            status_code = 200
            text = json.dumps(response)

            def json(self) -> dict:
                return response

        env = {
            "ANTHROPIC_API_KEY": "test-key",
            "ANTHROPIC_MAX_RETRIES": "2",
            "ANTHROPIC_RETRY_BACKOFF_SECONDS": "2",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "lead_machine.llm_client.requests.post",
                side_effect=[requests.exceptions.ReadTimeout("read timed out"), requests.exceptions.Timeout("timed out"), FakeResponse()],
            ) as post:
                with patch("time.sleep") as sleep:
                    result = score_batch("score this batch", model="claude-sonnet-5", max_tokens=4096)

        self.assertEqual(result["bundle"]["batch_id"], "batch_001")
        self.assertEqual(post.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    def test_llm_client_retries_500_and_503_responses(self) -> None:
        from lead_machine.llm_client import score_batch

        score = self._manual_score(total_score=75, grade="B")
        success = {
            "content": [{"type": "text", "text": json.dumps({"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": score}]})}],
            "usage": {"input_tokens": 123, "output_tokens": 45},
        }

        class FakeResponse:
            def __init__(self, status_code: int, payload: dict | None = None) -> None:
                self.status_code = status_code
                self._payload = payload or {}
                self.text = json.dumps(payload or {"error": "temporary"})

            def json(self) -> dict:
                return self._payload

        env = {
            "ANTHROPIC_API_KEY": "test-key",
            "ANTHROPIC_MAX_RETRIES": "2",
            "ANTHROPIC_RETRY_BACKOFF_SECONDS": "2",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "lead_machine.llm_client.requests.post",
                side_effect=[FakeResponse(500), FakeResponse(503), FakeResponse(200, success)],
            ) as post:
                with patch("time.sleep") as sleep:
                    result = score_batch("score this batch", model="claude-sonnet-5", max_tokens=4096)

        self.assertEqual(result["bundle"]["batch_id"], "batch_001")
        self.assertEqual(post.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    def test_llm_client_does_not_retry_deterministic_400_response(self) -> None:
        from lead_machine.llm_client import score_batch

        class FakeResponse:
            status_code = 400
            text = "bad request"

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "ANTHROPIC_MAX_RETRIES": "2"}, clear=False):
            with patch("lead_machine.llm_client.requests.post", return_value=FakeResponse()) as post:
                with patch("time.sleep") as sleep:
                    with self.assertRaises(RuntimeError) as caught:
                        score_batch("score this batch", model="claude-sonnet-5", max_tokens=4096)

        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()
        self.assertIn("Anthropic API error 400", str(caught.exception))

    def test_llm_client_final_retry_error_includes_timeout_settings_and_attempts(self) -> None:
        import requests
        from lead_machine.llm_client import score_batch

        env = {
            "ANTHROPIC_API_KEY": "test-key",
            "ANTHROPIC_CONNECT_TIMEOUT_SECONDS": "15",
            "ANTHROPIC_READ_TIMEOUT_SECONDS": "180",
            "ANTHROPIC_MAX_RETRIES": "2",
            "ANTHROPIC_RETRY_BACKOFF_SECONDS": "2",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "lead_machine.llm_client.requests.post",
                side_effect=requests.exceptions.ReadTimeout("read timed out"),
            ) as post:
                with patch("time.sleep"):
                    with self.assertRaises(RuntimeError) as caught:
                        score_batch("score this batch", model="claude-sonnet-5", max_tokens=4096)

        message = str(caught.exception)
        self.assertEqual(post.call_count, 3)
        self.assertIn("model=claude-sonnet-5", message)
        self.assertIn("connect_timeout=15", message)
        self.assertIn("read_timeout=180", message)
        self.assertIn("attempts=3", message)
        self.assertIn("read timed out", message)

    def test_api_llm_caps_stop_before_next_batch_call(self) -> None:
        from score_leads import score_records_api_run

        first_score = self._manual_score(total_score=75, grade="B")
        first_enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        second_enriched = {
            "google": {
                **self._google_fixture(),
                "business_name": "Second Landscaping",
                "place_id": "ChIJ456",
            },
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int) -> dict:
            return {
                "bundle": {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": first_score}]},
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch) as score_batch:
                run = score_records_api_run(
                    [first_enriched, second_enriched],
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="hardscaping",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=4096,
                    max_calls=1,
                    max_tokens_per_run=0,
                    max_usd_per_run=0.0,
                )

        self.assertEqual(score_batch.call_count, 1)
        self.assertEqual(run.calls, 1)
        self.assertEqual(run.tokens, 15)
        self.assertTrue(run.stopped_by_cap)
        self.assertEqual([row["Business Name"] for row in run.manual_run.rows], ["A+ Landscaping LLC"])
        self.assertEqual([missing.lead_name for missing in run.manual_run.missing_scores], ["Second Landscaping"])

    def test_api_llm_token_cap_stops_before_batch_that_could_exceed_remaining_budget(self) -> None:
        from score_leads import build_api_llm_scoring_prompt, build_llm_scoring_record, score_records_api_run

        first_score = self._manual_score(total_score=75, grade="B")
        first_enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        second_enriched = {
            "google": {
                **self._google_fixture(),
                "business_name": "Second Landscaping",
                "place_id": "ChIJ456",
            },
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int) -> dict:
            return {
                "bundle": {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": first_score}]},
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            }

        projected_prompt = build_api_llm_scoring_prompt(
            [build_llm_scoring_record(dict(first_enriched), fallback_vertical="hardscaping")],
            batch_id="batch_001",
        )
        projected_tokens = (len(projected_prompt) + 3) // 4 + 20

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch) as score_batch:
                run = score_records_api_run(
                    [first_enriched, second_enriched],
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="hardscaping",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=20,
                    max_calls=0,
                    max_tokens_per_run=projected_tokens + 4,
                    max_usd_per_run=0.0,
                )

        self.assertEqual(score_batch.call_count, 1)
        self.assertEqual(run.calls, 1)
        self.assertEqual(run.tokens, 5)
        self.assertTrue(run.stopped_by_cap)
        self.assertEqual(run.stop_reason, "MAX_LLM_TOKENS_PER_RUN")
        self.assertEqual([row["Business Name"] for row in run.manual_run.rows], ["A+ Landscaping LLC"])
        self.assertEqual([missing.lead_name for missing in run.manual_run.missing_scores], ["Second Landscaping"])

    def test_api_llm_usd_cap_stops_before_batch_that_could_exceed_remaining_budget(self) -> None:
        from score_leads import build_api_llm_scoring_prompt, build_llm_scoring_record, estimate_llm_usd, score_records_api_run

        first_score = self._manual_score(total_score=75, grade="B")
        first_enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        second_enriched = {
            "google": {
                **self._google_fixture(),
                "business_name": "Second Landscaping",
                "place_id": "ChIJ456",
            },
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int) -> dict:
            return {
                "bundle": {"batch_id": "batch_001", "scores": [{"score_file": "chij123.json", "score": first_score}]},
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }

        projected_prompt = build_api_llm_scoring_prompt(
            [build_llm_scoring_record(dict(first_enriched), fallback_vertical="hardscaping")],
            batch_id="batch_001",
        )
        projected_usd = estimate_llm_usd(
            {"input_tokens": (len(projected_prompt) + 3) // 4, "output_tokens": 20}
        )
        first_call_usd = estimate_llm_usd({"input_tokens": 1, "output_tokens": 1})

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch) as score_batch:
                run = score_records_api_run(
                    [first_enriched, second_enriched],
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="hardscaping",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=20,
                    max_calls=0,
                    max_tokens_per_run=0,
                    max_usd_per_run=projected_usd + (first_call_usd / 2),
                )

        self.assertEqual(score_batch.call_count, 1)
        self.assertEqual(run.calls, 1)
        self.assertTrue(run.stopped_by_cap)
        self.assertEqual(run.stop_reason, "MAX_LLM_USD_PER_RUN")
        self.assertEqual([row["Business Name"] for row in run.manual_run.rows], ["A+ Landscaping LLC"])
        self.assertEqual([missing.lead_name for missing in run.manual_run.missing_scores], ["Second Landscaping"])

    def _run_score_leads(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("sys.argv", argv), redirect_stdout(stdout), redirect_stderr(stderr):
            code = score_leads_main()
        return code, stdout.getvalue(), stderr.getvalue()

    def _write_enriched_jsonl(self, path: Path) -> None:
        record = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    def _fake_score_batch_factory(self):
        score = self._manual_score(total_score=75, grade="B")

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int) -> dict:
            return {
                "bundle": {
                    "batch_id": "batch_001",
                    "scores": [{"score_file": "chij123.json", "score": score}],
                },
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }

        return fake_score_batch

    def test_dry_run_llm_cost_does_not_call_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "enriched.jsonl"
            output_path = Path(tmp) / "scored.csv"
            self._write_enriched_jsonl(input_path)
            argv = [
                "score_leads.py",
                "--mode", "api-llm",
                "--input", str(input_path),
                "--output", str(output_path),
                "--scores-dir", str(Path(tmp) / "scores"),
                "--vertical", "hardscaping",
                "--dry-run-llm-cost",
            ]
            with patch("score_leads.llm_client.score_batch") as score_batch:
                code, stdout, stderr = self._run_score_leads(argv)

            self.assertEqual(code, 0, stderr)
            score_batch.assert_not_called()
            self.assertFalse(output_path.exists())
            self.assertIn("[LLM COST]", stdout)
            self.assertIn("batches=1", stdout)
            self.assertIn("projected_usd=", stdout)
            self.assertIn("[DRY-RUN]", stdout)

    def _write_large_evidence_scored_csv(self, path: Path) -> str:
        """Write a scored CSV whose Raw Evidence field exceeds the 128 KB
        default csv field-size limit, returning the giant value."""
        huge_evidence = json.dumps({"blob": "x" * 200_000})
        self.assertGreater(len(huge_evidence), 131072)
        fieldnames = [
            "Business Name",
            "Phone",
            "Sendable",
            "Packet Eligible",
            "Raw Evidence",
        ]
        row = {
            "Business Name": "Big Evidence Co",
            "Phone": "216-555-0100",
            "Sendable": "Yes",
            "Packet Eligible": "Yes",
            "Raw Evidence": huge_evidence,
        }
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow(row)
        return huge_evidence

    @contextmanager
    def _default_csv_field_limit(self):
        """Force the stock 128 KB csv field-size limit so the regression
        actually exercises the raise; restore the prior limit afterward."""
        previous = csv.field_size_limit()
        csv.field_size_limit(131072)
        try:
            yield
        finally:
            csv.field_size_limit(previous)

    def test_read_csv_rows_handles_large_raw_evidence_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scored_path = Path(tmp) / "scored.csv"
            huge_evidence = self._write_large_evidence_scored_csv(scored_path)
            with self._default_csv_field_limit():
                rows = read_csv_rows(scored_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Raw Evidence"], huge_evidence)

    def test_dry_run_llm_cost_resume_reads_large_existing_scored_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "enriched.jsonl"
            output_path = Path(tmp) / "scored.csv"
            self._write_enriched_jsonl(input_path)
            self._write_large_evidence_scored_csv(output_path)
            argv = [
                "score_leads.py",
                "--mode", "api-llm",
                "--input", str(input_path),
                "--output", str(output_path),
                "--scores-dir", str(Path(tmp) / "scores"),
                "--vertical", "hardscaping",
                "--dry-run-llm-cost",
            ]
            with self._default_csv_field_limit():
                with patch("score_leads.llm_client.score_batch") as score_batch:
                    code, stdout, stderr = self._run_score_leads(argv)
            self.assertEqual(code, 0, stderr)
            score_batch.assert_not_called()
            self.assertIn("[RESUME] Existing scored rows: 1", stdout)
            self.assertIn("[DRY-RUN]", stdout)

    def test_upload_airtable_reads_large_raw_evidence_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scored_path = Path(tmp) / "scored.csv"
            self._write_large_evidence_scored_csv(scored_path)
            with self._default_csv_field_limit():
                records = upload_airtable.read_csv_records(
                    scored_path,
                    only_sendable=False,
                    packet_dir=Path(tmp),
                )
            self.assertEqual(len(records), 1)

    def test_projected_cost_over_threshold_refuses_without_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "enriched.jsonl"
            output_path = Path(tmp) / "scored.csv"
            self._write_enriched_jsonl(input_path)
            argv = [
                "score_leads.py",
                "--mode", "api-llm",
                "--input", str(input_path),
                "--output", str(output_path),
                "--scores-dir", str(Path(tmp) / "scores"),
                "--vertical", "hardscaping",
            ]
            with patch.dict(os.environ, {"LLM_SPEND_CONFIRM_USD": "0.0001"}, clear=False):
                with patch("score_leads.llm_client.score_batch") as score_batch:
                    code, stdout, stderr = self._run_score_leads(argv)

                self.assertEqual(code, 1)
                score_batch.assert_not_called()
                self.assertFalse(output_path.exists())
                self.assertIn("[REFUSE]", stdout)
                self.assertIn("--confirm-llm-spend", stdout)

    def test_confirm_llm_spend_allows_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "enriched.jsonl"
            output_path = Path(tmp) / "scored.csv"
            self._write_enriched_jsonl(input_path)
            argv = [
                "score_leads.py",
                "--mode", "api-llm",
                "--input", str(input_path),
                "--output", str(output_path),
                "--scores-dir", str(Path(tmp) / "scores"),
                "--vertical", "hardscaping",
                "--confirm-llm-spend",
            ]
            with patch.dict(os.environ, {"LLM_SPEND_CONFIRM_USD": "0.0001"}, clear=False):
                with patch(
                    "score_leads.llm_client.score_batch",
                    side_effect=self._fake_score_batch_factory(),
                ) as score_batch:
                    code, stdout, stderr = self._run_score_leads(argv)

                self.assertEqual(code, 0, stderr)
                score_batch.assert_called_once()
                self.assertTrue(output_path.exists())

    def test_stale_scores_dir_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "enriched.jsonl"
            output_path = Path(tmp) / "scored.csv"
            self._write_enriched_jsonl(input_path)
            scores_dir = Path(tmp) / "scores"
            scores_dir.mkdir(parents=True)
            (scores_dir / "stale_old.json").write_text("{}", encoding="utf-8")
            argv = [
                "score_leads.py",
                "--mode", "api-llm",
                "--input", str(input_path),
                "--output", str(output_path),
                "--scores-dir", str(scores_dir),
                "--vertical", "hardscaping",
            ]
            with patch("score_leads.llm_client.score_batch") as score_batch:
                code, stdout, stderr = self._run_score_leads(argv)

            self.assertEqual(code, 1)
            score_batch.assert_not_called()
            self.assertTrue((scores_dir / "stale_old.json").exists())
            self.assertIn("[REFUSE]", stdout)
            self.assertIn("--fresh-scores-dir", stdout)

    def test_fresh_scores_dir_clears_stale_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "enriched.jsonl"
            output_path = Path(tmp) / "scored.csv"
            self._write_enriched_jsonl(input_path)
            scores_dir = Path(tmp) / "scores"
            bundle_dir = scores_dir / "_api_bundles"
            bundle_dir.mkdir(parents=True)
            (scores_dir / "stale_old.json").write_text("{}", encoding="utf-8")
            (bundle_dir / "old_bundle.json").write_text("{}", encoding="utf-8")
            argv = [
                "score_leads.py",
                "--mode", "api-llm",
                "--input", str(input_path),
                "--output", str(output_path),
                "--scores-dir", str(scores_dir),
                "--vertical", "hardscaping",
                "--fresh-scores-dir",
            ]
            with patch(
                "score_leads.llm_client.score_batch",
                side_effect=self._fake_score_batch_factory(),
            ) as score_batch:
                code, stdout, stderr = self._run_score_leads(argv)

            self.assertEqual(code, 0, stderr)
            score_batch.assert_called_once()
            self.assertFalse((scores_dir / "stale_old.json").exists())
            self.assertFalse((bundle_dir / "old_bundle.json").exists())
            self.assertIn("[FRESH]", stdout)

    def test_usage_metadata_written_after_successful_call_even_if_validation_fails(self) -> None:
        from score_leads import score_records_api_run

        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        with tempfile.TemporaryDirectory() as tmp:
            usage_path = Path(tmp) / "usage.json"
            with patch(
                "score_leads.llm_client.score_batch",
                side_effect=self._fake_score_batch_factory(),
            ):
                with patch(
                    "score_leads.score_records_manual_run",
                    side_effect=ValueError("merge validation failed"),
                ):
                    with self.assertRaises(ValueError):
                        score_records_api_run(
                            [enriched],
                            scores_dir=Path(tmp) / "scores",
                            fallback_vertical="hardscaping",
                            batch_size=1,
                            model="claude-sonnet-5",
                            max_tokens=4096,
                            max_calls=0,
                            max_tokens_per_run=0,
                            max_usd_per_run=0.0,
                            usage_output_path=usage_path,
                        )

            self.assertTrue(usage_path.exists())
            payload = json.loads(usage_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["calls"], 1)
            self.assertEqual(payload["total_tokens"], 15)
            self.assertTrue(payload["partial"])

    def test_api_llm_mode_writes_byte_identical_csv_to_manual_bundle_import(self) -> None:
        score_one = self._manual_score(total_score=75, grade="B")
        score_two = self._manual_score(
            total_score=86,
            grade="A",
            subscores={
                "icp_fit": 18,
                "reputation_demand_proof": 14,
                "web_conversion_pain": 22,
                "mockup_leverage": 12,
                "reachability": 8,
                "offer_angle_clarity": 8,
                "evidence_confidence": 4,
            },
            best_angle="No-Site Launch",
        )
        first_enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        second_enriched = {
            "google": {
                **self._google_fixture(),
                "business_name": "Second Landscaping",
                "place_id": "ChIJ456",
            },
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        bundle = {
            "batch_id": "batch_001",
            "scores": [
                {"score_file": "chij123.json", "score": score_one},
                {"score_file": "chij456.json", "score": score_two},
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            input_path = tmp_path / "enriched.jsonl"
            input_path.write_text(f"{json.dumps(first_enriched)}\n{json.dumps(second_enriched)}\n", encoding="utf-8")
            manual_scores_dir = tmp_path / "manual_scores"
            api_scores_dir = tmp_path / "api_scores"
            manual_output = tmp_path / "manual.csv"
            api_output = tmp_path / "api.csv"
            bundle_path = tmp_path / "bundle.json"
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            import_score_bundle(bundle_path, manual_scores_dir)

            manual_argv = [
                "score_leads.py",
                "--input",
                str(input_path),
                "--output",
                str(manual_output),
                "--vertical",
                "hardscaping",
                "--mode",
                "manual-llm",
                "--scores-dir",
                str(manual_scores_dir),
            ]
            with patch("sys.argv", manual_argv):
                manual_code = score_leads_main()

            api_argv = [
                "score_leads.py",
                "--input",
                str(input_path),
                "--output",
                str(api_output),
                "--vertical",
                "hardscaping",
                "--mode",
                "api-llm",
                "--scores-dir",
                str(api_scores_dir),
                "--llm-batch-size",
                "2",
            ]
            with patch("score_leads.llm_client.score_batch", return_value={"bundle": bundle, "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}):
                with patch("sys.argv", api_argv):
                    api_code = score_leads_main()

            manual_bytes = manual_output.read_bytes()
            api_bytes = api_output.read_bytes()

        self.assertEqual(manual_code, 0)
        self.assertEqual(api_code, 0)
        self.assertEqual(api_bytes, manual_bytes)

    def test_api_llm_resume_skips_existing_csv_rows_and_continues_from_same_input(self) -> None:
        records = [
            self._huge_detailing_record(name=f"Detailing {index}", place_id=f"ChIJ{index}")
            for index in range(1, 5)
        ]
        for index, record in enumerate(records, start=1):
            record["google"]["google_maps_url"] = f"https://maps.google.com/?cid={index}"

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int) -> dict:
            score_files = sorted(set(re.findall(r'"score_file": "([^"]+\.json)"', prompt)))
            return {
                "bundle": {
                    "batch_id": "batch_001",
                    "scores": [
                        {"score_file": score_file, "score": self._manual_score(total_score=75, grade="B")}
                        for score_file in score_files
                    ],
                },
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            input_path = tmp_path / "enriched.jsonl"
            output_path = tmp_path / "scored.csv"
            scores_dir = tmp_path / "scores"
            input_path.write_text(
                "".join(f"{json.dumps(record)}\n" for record in records),
                encoding="utf-8",
            )

            first_argv = [
                "score_leads.py",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--vertical",
                "detailing",
                "--mode",
                "api-llm",
                "--scores-dir",
                str(scores_dir),
                "--llm-batch-size",
                "1",
                "--limit",
                "2",
            ]
            with patch.dict(os.environ, {"MAX_LLM_CALLS_PER_RUN": "1", "MAX_LLM_TOKENS_PER_RUN": "0", "MAX_LLM_USD_PER_RUN": "0"}):
                with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch):
                    with patch("sys.argv", first_argv):
                        first_code = score_leads_main()

            with output_path.open(newline="", encoding="utf-8") as f:
                first_rows = list(csv.DictReader(f))
            self.assertEqual(first_code, 0)
            self.assertEqual([row["Place Key"] for row in first_rows], ["ChIJ1"])

            second_argv = list(first_argv)
            with patch.dict(os.environ, {"MAX_LLM_CALLS_PER_RUN": "0", "MAX_LLM_TOKENS_PER_RUN": "0", "MAX_LLM_USD_PER_RUN": "0"}):
                with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch) as score_batch:
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        with patch("sys.argv", second_argv):
                            second_code = score_leads_main()

            with output_path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            scored_files = [
                sorted(set(re.findall(r'"score_file": "([^"]+\.json)"', call.args[0])))
                for call in score_batch.call_args_list
            ]

        self.assertEqual(second_code, 0)
        self.assertEqual(scored_files, [["chij2.json"], ["chij3.json"]])
        self.assertEqual([row["Place Key"] for row in rows], ["ChIJ1", "ChIJ2", "ChIJ3"])
        self.assertIn("[RESUME] Existing scored rows: 1", stdout.getvalue())
        self.assertIn("[RESUME] Skipping already scored: 1", stdout.getvalue())
        self.assertIn("[RESUME] New records selected: 2", stdout.getvalue())

    def test_api_llm_resume_offset_selects_manual_chunk_after_skipping_scored_rows(self) -> None:
        records = [
            self._huge_detailing_record(name=f"Detailing {index}", place_id=f"ChIJ{index}")
            for index in range(1, 5)
        ]
        for index, record in enumerate(records, start=1):
            record["google"]["google_maps_url"] = f"https://maps.google.com/?cid={index}"

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int) -> dict:
            score_files = sorted(set(re.findall(r'"score_file": "([^"]+\.json)"', prompt)))
            return {
                "bundle": {
                    "batch_id": "batch_001",
                    "scores": [
                        {"score_file": score_file, "score": self._manual_score(total_score=75, grade="B")}
                        for score_file in score_files
                    ],
                },
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            input_path = tmp_path / "enriched.jsonl"
            output_path = tmp_path / "scored.csv"
            scores_dir = tmp_path / "scores"
            input_path.write_text(
                "".join(f"{json.dumps(record)}\n" for record in records),
                encoding="utf-8",
            )
            write_csv(output_path, [{"Place Key": "ChIJ2", "Business Name": "Existing Detail"}])

            argv = [
                "score_leads.py",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--vertical",
                "detailing",
                "--mode",
                "api-llm",
                "--scores-dir",
                str(scores_dir),
                "--llm-batch-size",
                "1",
                "--offset",
                "1",
                "--limit",
                "1",
            ]
            with patch.dict(os.environ, {"MAX_LLM_CALLS_PER_RUN": "0", "MAX_LLM_TOKENS_PER_RUN": "0", "MAX_LLM_USD_PER_RUN": "0"}):
                with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch) as score_batch:
                    with patch("sys.argv", argv):
                        code = score_leads_main()

            with output_path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            scored_files = [
                sorted(set(re.findall(r'"score_file": "([^"]+\.json)"', call.args[0])))
                for call in score_batch.call_args_list
            ]

        self.assertEqual(code, 0)
        self.assertEqual(scored_files, [["chij3.json"]])
        self.assertEqual([row["Place Key"] for row in rows], ["ChIJ2", "ChIJ3"])

    def test_api_llm_resume_does_not_hide_unknown_refs_for_new_records(self) -> None:
        first = self._huge_detailing_record(name="Already Scored Detail", place_id="ChIJ1")
        second = self._huge_detailing_record(name="Bad Ref Detail", place_id="ChIJ2")
        first["google"]["google_maps_url"] = "https://maps.google.com/?cid=1"
        second["google"]["google_maps_url"] = "https://maps.google.com/?cid=2"
        fake_ref_score = self._manual_score(
            total_score=75,
            grade="B",
            evidence_refs=["fake_unknown_ref"],
            safe_outreach_claims=[
                {"claim": "Your buyer-intent signals show active demand.", "evidence_refs": ["fake_unknown_ref"]}
            ],
        )
        fake_ref_bundle = {
            "batch_id": "batch_001",
            "scores": [{"score_file": "chij2.json", "score": fake_ref_score}],
        }

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            existing_keys = {"place:ChIJ1"}
            with patch(
                "score_leads.llm_client.score_batch",
                return_value={
                    "bundle": fake_ref_bundle,
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                },
            ):
                with self.assertRaises(ValueError) as caught:
                    from score_leads import score_records_api_run

                    score_records_api_run(
                        [first, second],
                        scores_dir=scores_dir,
                        fallback_vertical="detailing",
                        batch_size=1,
                        model="claude-sonnet-5",
                        max_tokens=4096,
                        max_calls=0,
                        max_tokens_per_run=100_000,
                        max_usd_per_run=0.0,
                        resume_scored_keys=existing_keys,
                    )

        self.assertIn("Unknown evidence_refs: fake_unknown_ref", str(caught.exception))

    def _huge_detailing_record(self, *, name: str = "A+ Detailing LLC", place_id: str = "ChIJ123") -> dict:
        giant_text = (
            "Ceramic coating packages include paint correction before installation. "
            "Call or text for a quote and appointment. "
            + ("generic navigation filler SHOULD_NOT_APPEAR_IN_COMPACT_PROMPT " * 900)
        )
        return {
            "google": {
                **self._google_fixture(),
                "business_name": name,
                "place_id": place_id,
                "category": "auto_detailing",
                "address": "123 Main St, Kirtland, OH",
            },
            "website": {
                "site_status": "weak",
                "final_url": "https://example.com",
                "homepage": {"url": "https://example.com", "text": giant_text},
                "pages": {
                    "services": {
                        "url": "https://example.com/services",
                        "text": "Paint protection film and PPF installs are available. Request an estimate online.",
                    },
                    "about": {
                        "url": "https://example.com/about",
                        "text": "Award winning team. " + ("about filler " * 600),
                    },
                },
                "combined_text": giant_text,
                "cta_strength": "Weak",
                "contact_form_detected": "No",
                "estimate_form_detected": "No",
                "form_strength": "None",
                "form_detection_method": "none",
                "high_ticket_terms": ["ceramic coating", "paint correction", "PPF"],
                "third_party_tools": ["ServiceTitan"],
                "social_links": ["https://facebook.com/example-detailing"],
                "desktop_screenshot_path": "screenshots/detailing-desktop.png",
                "mobile_screenshot_path": "screenshots/detailing-mobile.png",
                "visual_review_needed": "Yes",
                "visual_quality_score": "Weak",
            },
            "screenshots": {"desktop": "screenshots/detailing-desktop.png", "mobile": "screenshots/detailing-mobile.png"},
            "evidence": self._evidence_fixture(
                site_status="weak",
                evidence_items=[
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 130 reviews"},
                    {"id": "E2", "field": "cta_strength", "value": "Weak"},
                    {"id": "E3", "field": "estimate_form_detected", "value": "No"},
                    {"id": "E4", "field": "third_party_tools", "value": "ServiceTitan"},
                    {"id": "E5", "field": "screenshots", "value": {"desktop": "screenshots/detailing-desktop.png", "mobile": "screenshots/detailing-mobile.png"}},
                ],
            ),
            "buyer_intent": self._buyer_intent_fixture(),
            "safe_outreach_claims": [{"claim": "Your site mentions ceramic coating.", "evidence_refs": ["E2"]}],
        }

    def test_llm_scoring_record_preserves_full_enriched_text_but_compacts_prompt(self) -> None:
        from score_leads import build_api_llm_scoring_prompt, build_llm_scoring_record

        record = self._huge_detailing_record()
        original_combined = record["website"]["combined_text"]
        original_homepage = record["website"]["homepage"]["text"]
        original_pages_text = record["website"]["pages"]["about"]["text"]

        compact = build_llm_scoring_record(record, fallback_vertical="detailing")
        prompt = build_api_llm_scoring_prompt([compact], batch_id="batch_001")

        self.assertEqual(record["website"]["combined_text"], original_combined)
        self.assertEqual(record["website"]["homepage"]["text"], original_homepage)
        self.assertEqual(record["website"]["pages"]["about"]["text"], original_pages_text)
        self.assertNotIn(original_combined, prompt)
        self.assertNotIn("SHOULD_NOT_APPEAR_IN_COMPACT_PROMPT", prompt)
        self.assertIn("Ceramic coating packages include paint correction before installation.", prompt)
        self.assertIn("Paint protection film and PPF installs are available.", prompt)
        self.assertLess(len(json.dumps(compact, ensure_ascii=False)), 6000)

    def test_llm_scoring_record_preserves_screenshot_paths_and_visual_flags(self) -> None:
        from score_leads import build_llm_scoring_record

        compact = build_llm_scoring_record(self._huge_detailing_record(), fallback_vertical="detailing")

        self.assertEqual(compact["screenshots"]["desktop"], "screenshots/detailing-desktop.png")
        self.assertEqual(compact["screenshots"]["mobile"], "screenshots/detailing-mobile.png")
        self.assertEqual(compact["visual_review"]["visual_review_needed"], "Yes")
        self.assertEqual(compact["visual_review"]["visual_quality_score"], "Weak")

    def test_llm_scoring_record_always_includes_evidence_ref_options(self) -> None:
        from score_leads import build_llm_scoring_record

        record = {"google": {**self._google_fixture(), "website_url": ""}, "website": {}, "evidence": {}}

        compact = build_llm_scoring_record(record, fallback_vertical="detailing")

        options = compact["evidence_ref_options"]
        self.assertTrue(options)
        self.assertTrue(all(option.get("id") for option in options))
        self.assertIn("google_profile", {option["id"] for option in options})

    def test_evidence_ref_options_carry_only_id_and_label(self) -> None:
        # Slim the prompt: evidence_ref_options exist only so the LLM can copy an
        # id into evidence_refs. The proof text lives in selected_evidence_snippets
        # and the capsule fields, so snippet/source here are redundant tokens.
        from score_leads import build_llm_scoring_record, compact_evidence_ref_ids

        compact = build_llm_scoring_record(self._huge_detailing_record(), fallback_vertical="detailing")

        options = compact["evidence_ref_options"]
        self.assertTrue(options)
        for option in options:
            self.assertLessEqual(set(option.keys()), {"id", "label"})
            self.assertNotIn("snippet", option)
            self.assertNotIn("source", option)
        # ids are preserved so downstream evidence_ref validation still works
        self.assertTrue(compact_evidence_ref_ids(compact))

    def test_evidence_item_ref_options_only_include_selected_snippets(self) -> None:
        from score_leads import MAX_LLM_SCORING_SNIPPETS, build_llm_scoring_record

        record = {
            "google": {
                **self._google_fixture(),
                "business_name": "Evidence Heavy Detailer",
                "place_id": "ChIJEVIDENCE",
                "category": "auto_detailing",
            },
            "website": {"combined_text": ""},
            "evidence": self._evidence_fixture(
                site_status="weak",
                evidence_items=[
                    {
                        "id": f"E{i}",
                        "field": "cta_strength",
                        "value": f"Weak CTA proof {i}",
                        "source_url": f"https://example.com/{i}",
                    }
                    for i in range(MAX_LLM_SCORING_SNIPPETS + 3)
                ],
            ),
        }

        compact = build_llm_scoring_record(record, fallback_vertical="detailing")

        selected_ids = {
            item["id"]
            for item in compact["selected_evidence_snippets"]
            if item.get("kind") == "evidence_item"
        }
        offered_evidence_ids = {
            option["id"]
            for option in compact["evidence_ref_options"]
            if option["id"].startswith("E")
        }
        self.assertEqual(len(selected_ids), MAX_LLM_SCORING_SNIPPETS)
        self.assertEqual(offered_evidence_ids, selected_ids)
        self.assertNotIn(f"E{MAX_LLM_SCORING_SNIPPETS}", offered_evidence_ids)

    def test_buyer_intent_capsule_is_compacted_without_dropping_money_signal(self) -> None:
        # Raw buyer_intent duplicates buyer_intent_summary, the money_signal flag,
        # and provider_evidence.crm_booking_stack. Keep only the selected signals.
        from score_leads import build_llm_scoring_record

        compact = build_llm_scoring_record(self._huge_detailing_record(), fallback_vertical="detailing")

        buyer_intent = compact["buyer_intent"]
        self.assertNotIn("evidence", buyer_intent)
        self.assertNotIn("crm_booking_stack", buyer_intent)
        # money and demand signals still available to the scorer
        self.assertEqual(buyer_intent.get("money_signal"), True)
        self.assertEqual(compact["money_signal"], "Yes")
        self.assertTrue(compact["buyer_intent_summary"])
        self.assertEqual(
            compact["provider_evidence"]["crm_booking_stack"].get("detected_tools"),
            ["ServiceTitan"],
        )

    def test_api_llm_prompt_serializes_compact_records_minified(self) -> None:
        from score_leads import build_api_llm_scoring_prompt, build_llm_scoring_record

        compact = build_llm_scoring_record(self._huge_detailing_record(), fallback_vertical="detailing")
        prompt = build_api_llm_scoring_prompt([compact], batch_id="batch_001")

        records_block = prompt.split("compact_records:\n", 1)[1]
        # Minified JSON keeps the whole array on one line (no indent newlines).
        self.assertLessEqual(records_block.strip().count("\n"), 1)
        self.assertNotIn(': "', records_block)  # compact separators drop the ": " spacing

    def test_score_batch_with_debug_charges_failed_attempt_usage(self) -> None:
        # A validation-failed first attempt is a real paid call; its usage must be
        # added to the repaired result so token/USD caps see the true spend.
        import score_leads
        from lead_machine import llm_client

        compact_records = [
            {"score_file": "phone-123.json", "lead": {"business_name": "X"},
             "evidence_ref_options": [{"id": "google_profile", "label": "Google profile"}]}
        ]
        failed = llm_client.InvalidLlmResponse(
            "validation failed",
            raw_text="{}",
            parsed_json={"batch_id": "b", "scores": []},
            usage={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
        )
        repaired = {"bundle": {"batch_id": "b", "scores": []},
                    "usage": {"input_tokens": 800, "output_tokens": 150, "total_tokens": 950}}

        calls = {"n": 0}

        def fake_score_batch(prompt, *, model, max_tokens):
            calls["n"] += 1
            if calls["n"] == 1:
                raise failed
            return dict(repaired)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(score_leads.llm_client, "score_batch", side_effect=fake_score_batch), \
                 patch.object(score_leads, "build_validation_repair_prompt", return_value="repair"), \
                 patch.object(score_leads, "_write_validation_failed_llm_response", return_value=Path(tmp) / "dbg.json"):
                result = score_leads._score_batch_with_debug(
                    "prompt", batch_id="b", bundle_dir=Path(tmp),
                    compact_records=compact_records, model="m", max_tokens=4096,
                )

        self.assertEqual(
            result["usage"],
            {"input_tokens": 1800, "output_tokens": 350, "total_tokens": 2150},
        )
        self.assertEqual(result["call_count"], 2)

    def test_score_batch_with_debug_counts_usage_when_repair_also_fails(self) -> None:
        # Fallback path previously reported usage={} - hiding two real paid calls.
        import score_leads
        from lead_machine import llm_client

        compact_records = [{"score_file": "phone-123.json", "lead": {"business_name": "X"},
                            "evidence_ref_options": [{"id": "google_profile", "label": "Google profile"}]}]
        failed = llm_client.InvalidLlmResponse(
            "validation failed", raw_text="{}", parsed_json={"batch_id": "b", "scores": []},
            usage={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
        )
        repair_failed = llm_client.InvalidLlmResponse(
            "still bad", raw_text="{}", parsed_json={"batch_id": "b", "scores": []},
            usage={"input_tokens": 700, "output_tokens": 100, "total_tokens": 800},
        )

        calls = {"n": 0}

        def fake_score_batch(prompt, *, model, max_tokens):
            calls["n"] += 1
            raise failed if calls["n"] == 1 else repair_failed

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(score_leads.llm_client, "score_batch", side_effect=fake_score_batch), \
                 patch.object(score_leads, "build_validation_repair_prompt", return_value="repair"), \
                 patch.object(score_leads, "_write_validation_failed_llm_response", return_value=Path(tmp) / "dbg.json"), \
                 patch.object(score_leads, "_fallback_fill_empty_evidence_refs",
                              return_value=({"batch_id": "b", "scores": []}, "filled")):
                result = score_leads._score_batch_with_debug(
                    "prompt", batch_id="b", bundle_dir=Path(tmp),
                    compact_records=compact_records, model="m", max_tokens=4096,
                )

        self.assertEqual(
            result["usage"],
            {"input_tokens": 1700, "output_tokens": 300, "total_tokens": 2000},
        )
        self.assertEqual(result["call_count"], 2)

    def test_api_llm_prompt_requires_non_empty_evidence_refs_from_options(self) -> None:
        from score_leads import build_api_llm_scoring_prompt, build_llm_scoring_record

        compact = build_llm_scoring_record(self._huge_detailing_record(), fallback_vertical="detailing")
        prompt = build_api_llm_scoring_prompt([compact], batch_id="batch_001")

        self.assertIn("evidence_refs is REQUIRED for every score", prompt)
        self.assertIn("non-empty array of 1-3 strings", prompt)
        self.assertIn("copied exactly from that lead's evidence_ref_options[].id", prompt)
        self.assertIn("never output [] for evidence_refs", prompt)

    def test_api_llm_prompt_uses_shared_sendability_rules(self) -> None:
        from score_leads import build_api_llm_scoring_prompt, build_llm_scoring_record

        compact = build_llm_scoring_record(self._huge_detailing_record(), fallback_vertical="detailing")
        prompt = build_api_llm_scoring_prompt([compact], batch_id="batch_001")

        self.assertIn(
            "Missing site plus strong reviews can be an A lead only when there is a concrete conversion gap and enough evidence.",
            prompt,
        )
        self.assertIn("Do not score A/sendable just because the business is reputable.", prompt)
        self.assertIn(
            "mature plus an obvious broken/missing estimate path can still be high value when backed by money intent or very strong conversion-pain evidence.",
            prompt,
        )

    def test_api_llm_merge_accepts_lead_compact_evidence_refs_only(self) -> None:
        from score_leads import score_records_api_run

        first = self._huge_detailing_record(name="Buyer Intent Detail", place_id="ChIJ123")
        buyer_intent_score = self._manual_score(
            total_score=75,
            grade="B",
            evidence_refs=["buyer_intent"],
            safe_outreach_claims=[
                {"claim": "Your buyer-intent signals show active demand.", "evidence_refs": ["buyer_intent"]}
            ],
        )
        buyer_intent_bundle = {
            "batch_id": "batch_001",
            "scores": [{"score_file": "chij123.json", "score": buyer_intent_score}],
        }

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            with patch(
                "score_leads.llm_client.score_batch",
                return_value={
                    "bundle": buyer_intent_bundle,
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                },
            ):
                run = score_records_api_run(
                    [first],
                    scores_dir=scores_dir,
                    fallback_vertical="detailing",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=4096,
                    max_calls=0,
                    max_tokens_per_run=100_000,
                    max_usd_per_run=0.0,
                )

        self.assertEqual(run.manual_run.rows[0]["Evidence Refs"], json.dumps(["buyer_intent"]))
        self.assertNotIn("Invalid evidence reference", run.manual_run.rows[0]["Send Blockers"])

        second = self._huge_detailing_record(name="Fake Ref Detail", place_id="ChIJ456")
        fake_ref_score = self._manual_score(
            total_score=75,
            grade="B",
            evidence_refs=["fake_unknown_ref"],
            safe_outreach_claims=[
                {"claim": "Your buyer-intent signals show active demand.", "evidence_refs": ["fake_unknown_ref"]}
            ],
        )
        valid_first_bundle = {
            "batch_id": "batch_001",
            "scores": [{"score_file": "chij123.json", "score": buyer_intent_score}],
        }
        fake_ref_bundle = {
            "batch_id": "batch_002",
            "scores": [{"score_file": "chij456.json", "score": fake_ref_score}],
        }

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            with patch(
                "score_leads.llm_client.score_batch",
                side_effect=[
                    {
                        "bundle": valid_first_bundle,
                        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    },
                    {
                        "bundle": fake_ref_bundle,
                        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                    },
                ],
            ):
                with self.assertRaises(ValueError) as caught:
                    score_records_api_run(
                        [first, second],
                        scores_dir=scores_dir,
                        fallback_vertical="detailing",
                        batch_size=1,
                        model="claude-sonnet-5",
                        max_tokens=4096,
                        max_calls=0,
                        max_tokens_per_run=100_000,
                        max_usd_per_run=0.0,
                    )

            self.assertTrue((scores_dir / "chij123.json").exists())
            debug_path = scores_dir / "_api_bundles" / "merge_validation_failed_chij456.json"
            self.assertTrue(debug_path.exists())
            debug_payload = json.loads(debug_path.read_text(encoding="utf-8"))

        message = str(caught.exception)
        self.assertIn("Unknown evidence_refs: fake_unknown_ref", message)
        self.assertIn("API merge validation debug written", message)
        self.assertEqual(debug_payload["lead_id"], "ChIJ456")
        self.assertEqual(debug_payload["offending_evidence_refs"], ["fake_unknown_ref"])
        self.assertIn("buyer_intent", debug_payload["allowed_refs"])
        self.assertNotIn("fake_unknown_ref", debug_payload["allowed_refs"])

    def test_api_llm_merge_accepts_ev_refs_shown_in_compact_capsule_text_only(self) -> None:
        from score_leads import build_llm_scoring_record, score_records_api_run

        record = self._huge_detailing_record(name="Capsule Ref Detail", place_id="ChIJ789")
        record["website"]["combined_text"] = (
            "Ceramic coating evidence ev-services_detected-9 appears in the compact capsule text only."
        )
        record["website"]["homepage"]["text"] = record["website"]["combined_text"]
        compact = build_llm_scoring_record(record, fallback_vertical="detailing")

        self.assertIn("ev-services_detected-9", json.dumps(compact, ensure_ascii=False))
        self.assertIn(
            "ev-services_detected-9",
            {option["id"] for option in compact["evidence_ref_options"]},
        )

        shown_ref_score = self._manual_score(
            total_score=75,
            grade="B",
            evidence_refs=["ev-services_detected-9"],
            safe_outreach_claims=[
                {"claim": "Your site mentions ceramic coating.", "evidence_refs": ["ev-services_detected-9"]}
            ],
        )
        fake_ref_score = self._manual_score(
            total_score=75,
            grade="B",
            evidence_refs=["fake_unknown_ref"],
            safe_outreach_claims=[
                {"claim": "Your site mentions ceramic coating.", "evidence_refs": ["fake_unknown_ref"]}
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            with patch(
                "score_leads.llm_client.score_batch",
                return_value={
                    "bundle": {
                        "batch_id": "batch_001",
                        "scores": [{"score_file": "chij789.json", "score": shown_ref_score}],
                    },
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                },
            ):
                run = score_records_api_run(
                    [record],
                    scores_dir=scores_dir,
                    fallback_vertical="detailing",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=4096,
                    max_calls=0,
                    max_tokens_per_run=100_000,
                    max_usd_per_run=0.0,
                )

        self.assertEqual(run.manual_run.rows[0]["Evidence Refs"], json.dumps(["ev-services_detected-9"]))

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            with patch(
                "score_leads.llm_client.score_batch",
                return_value={
                    "bundle": {
                        "batch_id": "batch_001",
                        "scores": [{"score_file": "chij789.json", "score": fake_ref_score}],
                    },
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                },
            ):
                with self.assertRaises(ValueError) as caught:
                    score_records_api_run(
                        [record],
                        scores_dir=scores_dir,
                        fallback_vertical="detailing",
                        batch_size=1,
                        model="claude-sonnet-5",
                        max_tokens=4096,
                        max_calls=0,
                        max_tokens_per_run=100_000,
                        max_usd_per_run=0.0,
                    )

        self.assertIn("Unknown evidence_refs: fake_unknown_ref", str(caught.exception))

    def test_api_llm_compact_prompt_for_ten_huge_leads_stays_under_run_token_cap(self) -> None:
        from score_leads import build_api_llm_scoring_prompt, build_llm_scoring_record, estimate_prompt_tokens

        records = [
            build_llm_scoring_record(
                self._huge_detailing_record(name=f"Detailing {index}", place_id=f"ChIJ{index}"),
                fallback_vertical="detailing",
            )
            for index in range(10)
        ]
        prompt = build_api_llm_scoring_prompt(records, batch_id="batch_001")

        self.assertLess(estimate_prompt_tokens(prompt), 100_000)

    def test_api_llm_limit_ten_makes_at_least_one_call_under_normal_caps(self) -> None:
        from score_leads import score_records_api_run

        records = [
            self._huge_detailing_record(name=f"Detailing {index}", place_id=f"ChIJ{index}")
            for index in range(10)
        ]

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int) -> dict:
            score_files = sorted(set(re.findall(r'"score_file": "([^"]+\.json)"', prompt)))
            return {
                "bundle": {
                    "batch_id": "batch_001",
                    "scores": [
                        {"score_file": score_file, "score": self._manual_score(total_score=75, grade="B")}
                        for score_file in score_files
                    ],
                },
                "usage": {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
            }

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch) as score_batch:
                run = score_records_api_run(
                    records,
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="detailing",
                    batch_size=5,
                    model="claude-sonnet-5",
                    max_tokens=4096,
                    max_calls=0,
                    max_tokens_per_run=100_000,
                    max_usd_per_run=0.0,
                )

        self.assertGreaterEqual(score_batch.call_count, 1)
        self.assertGreaterEqual(run.calls, 1)

    def test_api_llm_shrinks_chunk_when_projected_prompt_exceeds_token_cap(self) -> None:
        from score_leads import build_api_llm_scoring_prompt, build_llm_scoring_record, estimate_prompt_tokens, score_records_api_run

        first = self._huge_detailing_record(name="First Detail", place_id="ChIJ1")
        second = self._huge_detailing_record(name="Second Detail", place_id="ChIJ2")
        single_prompt = build_api_llm_scoring_prompt(
            [build_llm_scoring_record(first, fallback_vertical="detailing")],
            batch_id="batch_001",
        )
        double_prompt = build_api_llm_scoring_prompt(
            [
                build_llm_scoring_record(first, fallback_vertical="detailing"),
                build_llm_scoring_record(second, fallback_vertical="detailing"),
            ],
            batch_id="batch_001",
        )
        run_cap = estimate_prompt_tokens(single_prompt) + 20 + 50
        self.assertLess(run_cap, estimate_prompt_tokens(double_prompt) + 20)

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int) -> dict:
            score_files = sorted(set(re.findall(r'"score_file": "([^"]+\.json)"', prompt)))
            return {
                "bundle": {
                    "batch_id": "batch_001",
                    "scores": [
                        {"score_file": score_file, "score": self._manual_score(total_score=75, grade="B")}
                        for score_file in score_files
                    ],
                },
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            }

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch", side_effect=fake_score_batch) as score_batch:
                run = score_records_api_run(
                    [first, second],
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="detailing",
                    batch_size=2,
                    model="claude-sonnet-5",
                    max_tokens=20,
                    max_calls=0,
                    max_tokens_per_run=run_cap,
                    max_usd_per_run=0.0,
                )

        self.assertEqual(score_batch.call_count, 2)
        self.assertEqual(run.calls, 2)
        self.assertFalse(run.stopped_by_cap)

    def test_api_llm_skips_single_record_that_exceeds_token_cap(self) -> None:
        from score_leads import score_records_api_run

        with tempfile.TemporaryDirectory() as tmp:
            with patch("score_leads.llm_client.score_batch") as score_batch:
                run = score_records_api_run(
                    [self._huge_detailing_record()],
                    scores_dir=Path(tmp) / "scores",
                    fallback_vertical="detailing",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=20,
                    max_calls=0,
                    max_tokens_per_run=10,
                    max_usd_per_run=0.0,
                )

        score_batch.assert_not_called()
        self.assertEqual(len(run.skipped_records or []), 1)
        self.assertEqual((run.skipped_records or [])[0].reason, "single_record_exceeds_token_cap")

    def test_api_llm_invalid_json_writes_raw_debug_output_before_raising(self) -> None:
        from lead_machine.llm_client import InvalidLlmResponse
        from score_leads import score_records_api_run

        raw_text = '{"batch_id":"batch_001","scores":[{"score_file":"chij123.json","score":{"opener":"unterminated'

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            with patch(
                "score_leads.llm_client.score_batch",
                side_effect=InvalidLlmResponse("Invalid LLM score bundle JSON: Unterminated string", raw_text=raw_text),
            ):
                with self.assertRaises(ValueError) as caught:
                    score_records_api_run(
                        [self._huge_detailing_record()],
                        scores_dir=scores_dir,
                        fallback_vertical="detailing",
                        batch_size=1,
                        model="claude-sonnet-5",
                        max_tokens=8192,
                        max_calls=0,
                        max_tokens_per_run=100_000,
                        max_usd_per_run=0.0,
                    )

            debug_path = scores_dir / "_api_bundles" / "batch_001_invalid_response_attempt_1.txt"
            self.assertTrue(debug_path.exists())
            self.assertEqual(debug_path.read_text(encoding="utf-8"), raw_text)
            self.assertIn(str(debug_path), str(caught.exception))

    def test_api_llm_terminal_failure_writes_partial_usage_output(self) -> None:
        from lead_machine.llm_client import InvalidLlmResponse
        from score_leads import score_records_api_run

        usage = {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200}

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            usage_path = Path(tmp) / "usage.json"
            with patch(
                "score_leads.llm_client.score_batch",
                side_effect=InvalidLlmResponse(
                    "terminal non-repairable error",
                    raw_text='{"bad": true}',
                    usage=usage,
                ),
            ):
                with self.assertRaises(ValueError):
                    score_records_api_run(
                        [self._huge_detailing_record()],
                        scores_dir=scores_dir,
                        fallback_vertical="detailing",
                        batch_size=1,
                        model="claude-sonnet-5",
                        max_tokens=4096,
                        max_calls=0,
                        max_tokens_per_run=100_000,
                        max_usd_per_run=0.0,
                        usage_output_path=usage_path,
                    )

            payload = json.loads(usage_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["calls"], 1)
            self.assertEqual(payload["input_tokens"], 1000)
            self.assertEqual(payload["output_tokens"], 200)
            self.assertEqual(payload["total_tokens"], 1200)
            self.assertTrue(payload["partial"])

    def test_api_llm_no_text_response_retries_with_larger_token_cap(self) -> None:
        from lead_machine.llm_client import InvalidLlmResponse
        from score_leads import score_records_api_run

        raw_text = json.dumps({"content": [{"type": "thinking", "thinking": "no final text yet"}]})

        calls = 0

        def fake_score_batch(prompt: str, *, model: str, max_tokens: int) -> dict:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise InvalidLlmResponse("Anthropic response did not include text content", raw_text=raw_text)
            score_files = sorted(set(re.findall(r'"score_file": "([^"]+\.json)"', prompt)))
            return {
                "bundle": {
                    "batch_id": "batch_001",
                    "scores": [
                        {"score_file": score_file, "score": self._manual_score(total_score=75, grade="B")}
                        for score_file in score_files
                    ],
                },
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            with patch(
                "score_leads.llm_client.score_batch",
                side_effect=fake_score_batch,
            ) as score_batch:
                run = score_records_api_run(
                    [self._huge_detailing_record()],
                    scores_dir=scores_dir,
                    fallback_vertical="detailing",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=4096,
                    max_calls=0,
                    max_tokens_per_run=100_000,
                    max_usd_per_run=0.0,
                )

            debug_path = scores_dir / "_api_bundles" / "batch_001_invalid_response_attempt_1.txt"
            self.assertTrue(debug_path.exists())
            self.assertEqual(debug_path.read_text(encoding="utf-8"), raw_text)
            self.assertEqual(score_batch.call_count, 2)
            self.assertEqual(score_batch.call_args_list[1].kwargs["max_tokens"], 8192)
            self.assertEqual(len(run.manual_run.rows), 1)

    def test_api_llm_validation_failure_repairs_with_evidence_ref_options(self) -> None:
        from lead_machine.llm_client import InvalidLlmResponse
        from score_leads import score_records_api_run

        invalid_score = self._manual_score(total_score=75, grade="B", evidence_refs=[])
        invalid_score["safe_outreach_claims"] = [
            {"claim": "Your Google profile shows strong reviews.", "evidence_refs": ["E1"]}
        ]
        repaired_score = self._manual_score(total_score=75, grade="B", evidence_refs=["E1"])
        invalid_bundle = {
            "batch_id": "batch_001",
            "scores": [{"score_file": "chij123.json", "score": invalid_score}],
        }
        repaired_bundle = {
            "batch_id": "batch_001",
            "scores": [{"score_file": "chij123.json", "score": repaired_score}],
        }
        raw_text = json.dumps(invalid_bundle)

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            with patch(
                "score_leads.llm_client.score_batch",
                side_effect=[
                    InvalidLlmResponse(
                        "Validation failures:\n- scores[0] (chij123.json): evidence_refs must be a non-empty list",
                        raw_text=raw_text,
                        parsed_json=invalid_bundle,
                    ),
                    {"bundle": repaired_bundle, "usage": {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12}},
                ],
            ) as score_batch:
                run = score_records_api_run(
                    [self._huge_detailing_record()],
                    scores_dir=scores_dir,
                    fallback_vertical="detailing",
                    batch_size=1,
                    model="claude-sonnet-5",
                    max_tokens=4096,
                    max_calls=0,
                    max_tokens_per_run=100_000,
                    max_usd_per_run=0.0,
                )

        self.assertEqual(score_batch.call_count, 2)
        repair_prompt = score_batch.call_args_list[1].kwargs.get("prompt") or score_batch.call_args_list[1].args[0]
        self.assertIn("Validation failures:", repair_prompt)
        self.assertIn(raw_text, repair_prompt)
        self.assertIn("evidence_ref_options", repair_prompt)
        self.assertEqual(run.manual_run.rows[0]["Evidence Refs"], json.dumps(["E1"]))

    def test_api_llm_validation_failure_writes_raw_debug_output(self) -> None:
        from lead_machine.llm_client import InvalidLlmResponse
        from score_leads import score_records_api_run

        invalid_score = self._manual_score(confidence="certain", evidence_refs=[])
        invalid_bundle = {
            "batch_id": "batch_001",
            "scores": [{"score_file": "chij123.json", "score": invalid_score}],
        }
        raw_text = json.dumps(invalid_bundle)

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            with patch(
                "score_leads.llm_client.score_batch",
                side_effect=[
                    InvalidLlmResponse(
                        "Validation failures:\n- scores[0] (chij123.json): confidence must be low, medium, or high",
                        raw_text=raw_text,
                        parsed_json=invalid_bundle,
                    ),
                    InvalidLlmResponse(
                        "Validation failures:\n- scores[0] (chij123.json): confidence must be low, medium, or high",
                        raw_text=raw_text,
                        parsed_json=invalid_bundle,
                    ),
                ],
            ):
                with self.assertRaises(RuntimeError) as caught:
                    score_records_api_run(
                        [self._huge_detailing_record()],
                        scores_dir=scores_dir,
                        fallback_vertical="detailing",
                        batch_size=1,
                        model="claude-sonnet-5",
                        max_tokens=4096,
                        max_calls=0,
                        max_tokens_per_run=100_000,
                        max_usd_per_run=0.0,
                    )

            debug_path = scores_dir / "_api_bundles" / "batch_001_validation_failed_attempt_1.json"
            self.assertTrue(debug_path.exists())
            self.assertEqual(debug_path.read_text(encoding="utf-8"), raw_text)
            message = str(caught.exception)
            self.assertIn("batch_001", message)
            self.assertIn("confidence must be low, medium, or high", message)
            self.assertIn(str(debug_path), message)

    def test_api_llm_preserves_successful_first_batch_when_second_batch_validation_fails(self) -> None:
        from lead_machine.llm_client import InvalidLlmResponse
        from score_leads import score_records_api_run

        first = self._huge_detailing_record(name="First Detail", place_id="ChIJ123")
        second = self._huge_detailing_record(name="Second Detail", place_id="ChIJ456")
        first_bundle = {
            "batch_id": "batch_001",
            "scores": [{"score_file": "chij123.json", "score": self._manual_score(total_score=75, grade="B")}],
        }
        invalid_bundle = {
            "batch_id": "batch_002",
            "scores": [{"score_file": "chij456.json", "score": self._manual_score(confidence="certain", evidence_refs=[])}],
        }

        with tempfile.TemporaryDirectory() as tmp:
            scores_dir = Path(tmp) / "scores"
            with patch(
                "score_leads.llm_client.score_batch",
                side_effect=[
                    {"bundle": first_bundle, "usage": {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}},
                    InvalidLlmResponse(
                        "Validation failures:\n- scores[0] (chij456.json): confidence must be low, medium, or high",
                        raw_text=json.dumps(invalid_bundle),
                        parsed_json=invalid_bundle,
                    ),
                    InvalidLlmResponse(
                        "Validation failures:\n- scores[0] (chij456.json): confidence must be low, medium, or high",
                        raw_text=json.dumps(invalid_bundle),
                        parsed_json=invalid_bundle,
                    ),
                ],
            ):
                with self.assertRaises(RuntimeError):
                    score_records_api_run(
                        [first, second],
                        scores_dir=scores_dir,
                        fallback_vertical="detailing",
                        batch_size=1,
                        model="claude-sonnet-5",
                        max_tokens=4096,
                        max_calls=0,
                        max_tokens_per_run=100_000,
                        max_usd_per_run=0.0,
                    )

            self.assertTrue((scores_dir / "_api_bundles" / "batch_001_response.json").exists())
            self.assertTrue((scores_dir / "chij123.json").exists())
            self.assertTrue((scores_dir / "_api_bundles" / "batch_002_validation_failed_attempt_1.json").exists())

    def test_manual_score_loader_accepts_new_schema_and_normalizes_mismatched_total(self) -> None:
        raw_score = self._manual_score(total_score=1, grade="Skip")

        with tempfile.TemporaryDirectory() as tmp:
            score_path = Path(tmp) / "lead.json"
            score_path.write_text(json.dumps(raw_score), encoding="utf-8")
            validated = load_manual_score(score_path, allowed_evidence_ids={"E1", "E2"})

        self.assertEqual(validated["total_score"], 75)
        self.assertEqual(validated["grade"], "B")
        self.assertEqual(validated["confidence"], "medium")

    def test_manual_score_loader_still_accepts_old_schema_without_total_or_grade(self) -> None:
        raw_score = self._manual_score()
        raw_score.pop("total_score", None)
        raw_score.pop("grade", None)

        with tempfile.TemporaryDirectory() as tmp:
            score_path = Path(tmp) / "lead.json"
            score_path.write_text(json.dumps(raw_score), encoding="utf-8")
            validated = load_manual_score(score_path, allowed_evidence_ids={"E1", "E2"})

        self.assertEqual(validated["total_score"], 75)
        self.assertEqual(validated["grade"], "B")

    def test_manual_llm_csv_outputs_user_facing_score_columns(self) -> None:
        raw_score = self._manual_score(
            subscores={
                "icp_fit": 18,
                "reputation_demand_proof": 14,
                "web_conversion_pain": 22,
                "mockup_leverage": 12,
                "reachability": 8,
                "offer_angle_clarity": 8,
                "evidence_confidence": 4,
            },
            total_score=86,
            grade="A",
            best_angle="No-Site Launch",
            confidence="medium",
        )
        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scores_dir = tmp_path / "scores"
            scores_dir.mkdir()
            (scores_dir / "chij123.json").write_text(json.dumps(raw_score), encoding="utf-8")
            output_path = tmp_path / "scored.csv"

            rows = score_records_manual([enriched], scores_dir=scores_dir, fallback_vertical="hardscaping")
            write_csv(output_path, rows)

            with output_path.open(newline="", encoding="utf-8") as f:
                [csv_row] = list(csv.DictReader(f))

        self.assertEqual(csv_row["Total Score"], "86")
        self.assertEqual(csv_row["Grade"], "A")
        self.assertEqual(csv_row["Best Angle"], "No-Site Launch")
        self.assertEqual(csv_row["Confidence"], "medium")
        self.assertEqual(csv_row["Sendable"], "Yes")
        self.assertEqual(csv_row["Minimum Evidence Passed"], "Yes")

    def test_manual_llm_airtable_fields_override_source_identity_fields(self) -> None:
        raw_score = self._manual_score(
            best_angle="No-Site Launch",
            recommended_action="Build no-site mockup",
            airtable_fields={
                "business_name": "Painesville Patio Co",
                "phone": "(440) 555-0199",
                "vertical": "hardscaping",
                "city": "Painesville",
                "category": "patio contractor",
                "recommended_action": "Send corrected hardscaping mockup",
            },
        )
        enriched = {
            "google": {
                **self._google_fixture(),
                "Business Name": "Painsville Services",
                "business_name": "",
                "Vertical": "service",
                "vertical": "",
                "City": "Painsville",
                "city": "",
                "Phone": "(440) 555-0100",
                "phone": "",
            },
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scores_dir = tmp_path / "scores"
            scores_dir.mkdir()
            (scores_dir / "chij123.json").write_text(json.dumps(raw_score), encoding="utf-8")
            output_path = tmp_path / "scored.csv"

            rows = score_records_manual([enriched], scores_dir=scores_dir, fallback_vertical="service")
            write_csv(output_path, rows)
            packet = packet_markdown(rows[0])

            with output_path.open(newline="", encoding="utf-8") as f:
                [csv_row] = list(csv.DictReader(f))

        self.assertEqual(csv_row["Business Name"], "Painesville Patio Co")
        self.assertEqual(csv_row["business_name"], "Painesville Patio Co")
        self.assertEqual(csv_row["Phone"], "(440) 555-0199")
        self.assertEqual(csv_row["phone"], "(440) 555-0199")
        self.assertEqual(csv_row["Vertical"], "hardscaping")
        self.assertEqual(csv_row["vertical"], "hardscaping")
        self.assertEqual(csv_row["category"], "patio contractor")
        self.assertEqual(csv_row["City"], "Painesville")
        self.assertEqual(csv_row["city"], "Painesville")
        self.assertEqual(csv_row["Best Service Angle"], "No-Site Launch")
        self.assertEqual(csv_row["best_angle"], "No-Site Launch")
        self.assertEqual(csv_row["Recommended Action"], "Send corrected hardscaping mockup")
        self.assertEqual(csv_row["recommended_action"], "Send corrected hardscaping mockup")
        self.assertIn("- **Business:** Painesville Patio Co", packet)
        self.assertIn("- **Vertical:** hardscaping", packet)
        self.assertIn("- **City:** Painesville", packet)

    def test_manual_llm_ignores_bad_airtable_field_keys_and_preserves_canonical_fields(self) -> None:
        raw_score = self._manual_score(
            airtable_fields={
                "Website URL": "https://bad.example",
                "Reviews": "999",
                "Rating": "1.0",
                "[Phone](tel:000)": "(000) 000-0000",
                "Business Name](https://bad.example)": "Bad Markdown Name",
            },
        )
        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertNotIn("[Phone](tel:000)", row)
        self.assertNotIn("Business Name](https://bad.example)", row)
        self.assertEqual(row["Website URL"], "https://example.com")
        self.assertEqual(row["Phone"], "(440) 637-5848")
        self.assertEqual(row["Reviews"], "130")
        self.assertEqual(row["Rating"], "4.9")

    def test_score_leads_writes_csv_after_importing_bad_airtable_fields_bundle(self) -> None:
        first_score = self._manual_score(total_score=75, grade="B")
        second_score = self._manual_score(
            total_score=75,
            grade="B",
            airtable_fields={
                "Website URL": "https://bad.example",
                "Reviews": "999",
                "Rating": "1.0",
                "[Phone](tel:000)": "(000) 000-0000",
            },
        )
        first_enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        second_enriched = {
            "google": {
                **self._google_fixture(),
                "business_name": "Bad Bundle Landscaping",
                "place_id": "ChIJ456",
            },
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bundle_path = tmp_path / "bundle.json"
            scores_dir = tmp_path / "scores"
            input_path = tmp_path / "enriched.jsonl"
            output_path = tmp_path / "scored.csv"
            bundle_path.write_text(
                json.dumps(
                    {
                        "scores": [
                            {"score_file": "chij123.json", "score": first_score},
                            {"score_file": "chij456.json", "score": second_score},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            input_path.write_text(
                f"{json.dumps(first_enriched)}\n{json.dumps(second_enriched)}\n",
                encoding="utf-8",
            )

            import_score_bundle(bundle_path, scores_dir)
            argv = [
                "score_leads.py",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--vertical",
                "hardscaping",
                "--mode",
                "manual-llm",
                "--scores-dir",
                str(scores_dir),
            ]
            with patch("sys.argv", argv):
                code = score_leads_main()

            with output_path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

        bad_bundle_row = next(row for row in rows if row["Business Name"] == "Bad Bundle Landscaping")
        self.assertEqual(code, 0)
        self.assertNotIn("[Phone](tel:000)", rows[0].keys())
        self.assertEqual(bad_bundle_row["Website URL"], "https://example.com")
        self.assertEqual(bad_bundle_row["Phone"], "(440) 637-5848")
        self.assertEqual(bad_bundle_row["Reviews"], "130")
        self.assertEqual(bad_bundle_row["Rating"], "4.9")

    def test_manual_scored_packet_omits_legacy_review_prompt(self) -> None:
        row = merge_manual_score(
            {"google": self._google_fixture(), "evidence": self._evidence_fixture(site_status="weak")},
            self._manual_score(),
            fallback_vertical="hardscaping",
        )

        packet = packet_markdown(row)

        self.assertIn("## Extracted Evidence", packet)
        self.assertIn("## Suggested Opener", packet)
        self.assertIn("## Core TSV Draft", packet)
        self.assertNotIn("ChatGPT/Claude Review Prompt", packet)

    def test_manual_llm_strict_mode_raises_when_any_score_missing(self) -> None:
        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        missing_score = {
            "google": {
                **self._google_fixture(),
                "business_name": "Missing Score Landscaping",
                "place_id": "ChIJ999",
            },
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scores_dir = tmp_path / "scores"
            scores_dir.mkdir()
            (scores_dir / "chij123.json").write_text(json.dumps(self._manual_score()), encoding="utf-8")

            with self.assertRaises(FileNotFoundError) as caught:
                score_records_manual(
                    [enriched, missing_score],
                    scores_dir=scores_dir,
                    fallback_vertical="hardscaping",
                )

        self.assertIn("Missing Score Landscaping", str(caught.exception))
        self.assertIn("chij999.json", str(caught.exception))

    def test_manual_llm_only_scored_writes_outputs_for_available_scores(self) -> None:
        raw_score = self._manual_score()
        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        missing_score = {
            "google": {
                **self._google_fixture(),
                "business_name": "Missing Score Landscaping",
                "place_id": "ChIJ999",
            },
            "evidence": self._evidence_fixture(site_status="weak"),
        }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scores_dir = tmp_path / "scores"
            scores_dir.mkdir()
            (scores_dir / "chij123.json").write_text(json.dumps(raw_score), encoding="utf-8")
            input_path = tmp_path / "enriched.jsonl"
            input_path.write_text(
                f"{json.dumps(enriched)}\n{json.dumps(missing_score)}\n",
                encoding="utf-8",
            )
            output_path = tmp_path / "scored.csv"
            cards_path = tmp_path / "cards.jsonl"
            stdout = io.StringIO()

            argv = [
                "score_leads.py",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--vertical",
                "hardscaping",
                "--mode",
                "manual-llm",
                "--scores-dir",
                str(scores_dir),
                "--cards-output",
                str(cards_path),
                "--only-scored",
            ]
            with patch("sys.argv", argv), redirect_stdout(stdout):
                score_leads_main()

            with output_path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            cards = [json.loads(line) for line in cards_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Business Name"], "A+ Landscaping LLC")
        self.assertIn("Sendable", rows[0])
        self.assertIn("Minimum Evidence Passed", rows[0])
        self.assertEqual(rows[0]["Sendable"], "Yes")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["business_name"], "A+ Landscaping LLC")
        self.assertNotIn("Missing Score Landscaping", [row["Business Name"] for row in rows])
        summary = stdout.getvalue()
        self.assertIn("[MANUAL] Scored records processed: 1", summary)
        self.assertIn("[MANUAL] Missing scores skipped: 1", summary)
        self.assertIn("Missing Score Landscaping", summary)
        self.assertIn(str(scores_dir / "chij999.json"), summary)
        self.assertIn(str(scores_dir / "missing-score-landscaping.json"), summary)

    def test_airtable_payload_and_feedback_metrics(self) -> None:
        card = {
            "place_id_or_unique_key": "ChIJ123",
            "grade": "A",
            "confidence": "medium",
            "best_angle": "Estimate Funnel",
            "recommended_action": "Build mockup then SMS owner",
            "airtable_fields": {
                "Business Name": "A+ Landscaping LLC",
                "Lead Priority": "A",
                "AI Confidence": "medium",
                "Contact Form Detected": "No",
                "Estimate Form Detected": "No",
                "Form Strength": "None",
                "Form Detection Method": "none",
                "CTA Strength": "Weak",
                "Visible Pain Score": "35",
                "Visible Pain Reasons": "No estimate form detected",
                "Send Trigger": "No estimate form detected",
            },
        }

        payload = build_airtable_payload(card)
        self.assertEqual(payload["fields"]["Place Key"], "ChIJ123")
        self.assertEqual(payload["fields"]["Lead Priority"], "A")
        self.assertEqual(payload["fields"]["Visible Pain Score"], 35)
        self.assertEqual(payload["fields"]["Send Trigger"], "No estimate form detected")

        sync_plan = plan_airtable_sync([card], existing_records_by_key={"ChIJ123": "rec123"})
        self.assertEqual(sync_plan[0]["method"], "PATCH")
        self.assertEqual(sync_plan[0]["record_id"], "rec123")

        metrics = compute_feedback_metrics(
            [
                {
                    "AI Grade": "A",
                    "Lou Grade": "A",
                    "Partner Grade": "B",
                    "Human Review Minutes": "2.5",
                },
                {
                    "AI Grade": "A",
                    "Lou Grade": "C",
                    "Partner Grade": "",
                    "Human Review Minutes": "4",
                },
                {
                    "AI Grade": "Skip",
                    "Sampled Skip Verdict": "False Skip",
                },
            ]
        )

        self.assertAlmostEqual(metrics["a_lead_human_approval_rate"], 0.5)
        self.assertAlmostEqual(metrics["average_a_review_minutes"], 3.25)
        self.assertAlmostEqual(metrics["sampled_false_skip_rate"], 1.0)

    def test_sendability_normalizes_duplicate_evidence_refs_and_flattens_safe_claims(self) -> None:
        self.assertEqual(normalize_evidence_refs(["E1", "E2", "E1", "E3"]), ["E1", "E2", "E3"])
        self.assertEqual(normalize_evidence_refs("E1, E2; E3 E2"), ["E1", "E2", "E3"])

        flattened = flatten_safe_outreach_claims(
            [
                {"claim": "Your quote path is hard to find on mobile.", "evidence_refs": ["E3", "E7"]},
                {"claim": "Your best photos are buried.", "evidence_refs": "E9"},
            ]
        )

        self.assertEqual(
            flattened,
            "Your quote path is hard to find on mobile. [E3,E7]; Your best photos are buried. [E9]",
        )

    def test_manual_score_with_invalid_safe_claim_ref_gets_blocked(self) -> None:
        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        raw_score = self._manual_score(
            safe_outreach_claims=[
                {"claim": "Your quote path is hard to find.", "evidence_refs": ["E2", "missing-ref"]}
            ]
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Invalid evidence reference", row["Send Blockers"])
        self.assertEqual(row["Minimum Evidence Passed"], "No")

    def test_manual_score_with_b_grade_but_no_concrete_issue_gets_blocked(self) -> None:
        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(
                site_status="okay",
                evidence_items=[
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 130 reviews"},
                    {"id": "E2", "field": "trust_signals", "value": "licensed, insured"},
                ],
            ),
        }
        raw_score = self._manual_score(
            safe_outreach_claims=[{"claim": "Your reviews are strong.", "evidence_refs": ["E1"]}]
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Lead Priority"], "B")
        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Missing concrete conversion issue", row["Send Blockers"])
        self.assertEqual(row["Packet Eligible"], "")

    def test_send_trigger_is_required_for_sendable_yes(self) -> None:
        enriched = {
            "google": self._google_fixture(),
            "evidence": {
                "site_status": "weak",
                "visible_pain_score": 35,
                "visible_pain_reasons": ["Generic/template website copy"],
                "send_trigger": "",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 18 reviews"},
                    {"id": "E2", "field": "site_issue_flags", "value": "generic_copy_signals"},
                ],
            },
        }
        raw_score = self._manual_score(
            evidence_refs=["E1", "E2"],
            safe_outreach_claims=[
                {"claim": "Your Google profile has enough review proof.", "evidence_refs": ["E1"]},
                {"claim": "The site has generic copy signals.", "evidence_refs": ["E2"]},
            ],
            airtable_fields={"Send Trigger": ""},
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Missing concrete send trigger", row["Send Blockers"])
        self.assertEqual(row["Packet Eligible"], "")

    def test_strong_reviews_with_sufficient_visible_pain_is_sendable(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "reviews": "130",
            },
            "buyer_intent": {
                "logic_version": BUYER_INTENT_LOGIC_VERSION,
                "crm_booking_stack": {
                    "detected_tools": ["ServiceTitan"],
                    "has_paid_crm": True,
                    "evidence": ["Detected paid CRM/booking tool: ServiceTitan"],
                },
                "review_velocity": {
                    "velocity_band": "steady",
                    "source": "count_only",
                    "evidence": ["130 Google reviews with 4.9 rating"],
                },
                "ads": {
                    "google_ad_transparency": "unknown",
                    "meta_ad_library": "unknown",
                    "lsa_badge": "unknown",
                    "errors": [],
                },
                "money_signal": True,
                "evidence": ["Paid CRM/booking stack detected"],
            },
            "evidence": {
                "site_status": "weak",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Weak",
                "visible_pain_score": 35,
                "visible_pain_reasons": ["Weak estimate form detected"],
                "visible_pain_categories": ["estimate_path_pain"],
                "send_trigger": "Weak estimate form detected",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 130 reviews"},
                    {"id": "E2", "field": "form_strength", "value": "Weak"},
                    {"id": "E3", "field": "send_trigger", "value": "Weak estimate form detected"},
                ],
            },
        }
        raw_score = self._manual_score(
            evidence_refs=["E1", "E2", "E3"],
            visible_pain_score=35,
            visible_pain_reasons=["Weak estimate form detected"],
            visible_pain_categories=["estimate_path_pain"],
            send_trigger="Weak estimate form detected",
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong review proof.", "evidence_refs": ["E1"]},
                {"claim": "The estimate form looks weak for the demand signal.", "evidence_refs": ["E2", "E3"]},
            ],
            opener="Your reviews look strong, but the estimate form feels weak for that demand.",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "Yes")
        self.assertNotIn("Visible pain too weak for review count", row["Send Blockers"])

    def test_bad_site_without_money_signal_is_sendable_with_real_pain(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "reviews": "18",
            },
            "buyer_intent": {
                "crm_booking_stack": {"detected_tools": [], "has_paid_crm": False, "evidence": []},
                "review_velocity": {
                    "velocity_band": "steady",
                    "source": "count_only",
                    "evidence": ["18 Google reviews with 4.9 rating"],
                },
                "ads": {
                    "google_ad_transparency": "unknown",
                    "meta_ad_library": "unknown",
                    "lsa_badge": "unknown",
                    "errors": [],
                },
                "money_signal": False,
                "evidence": [],
            },
            "evidence": {
                "site_status": "missing",
                "visible_pain_score": 40,
                "visible_pain_reasons": ["No owned website found"],
                "visible_pain_categories": ["no_site_or_social_only_pain"],
                "send_trigger": "No owned website found",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 18 reviews"},
                    {"id": "E2", "field": "site_status", "value": "missing"},
                    {"id": "E3", "field": "send_trigger", "value": "No owned website found"},
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Website Rebuild",
            evidence_refs=["E1", "E2", "E3"],
            visible_pain_score=40,
            visible_pain_reasons=["No owned website found"],
            visible_pain_categories=["no_site_or_social_only_pain"],
            send_trigger="No owned website found",
            safe_outreach_claims=[
                {"claim": "No owned website is visible.", "evidence_refs": ["E2", "E3"]},
                {"claim": "The Google profile has review proof.", "evidence_refs": ["E1"]},
            ],
            opener="I could not find an owned website, only the Google profile.",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "Yes")
        self.assertEqual(row["Money Signal"], "No")

    def test_advanced_optimization_is_not_sendable_without_concrete_visible_pain(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "reviews": "92",
            },
            "evidence": {
                "site_status": "okay",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "cta_strength": "Strong",
                "visible_pain_score": 0,
                "visible_pain_reasons": [],
                "send_trigger": "",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 92 reviews"},
                    {"id": "E2", "field": "form_strength", "value": "Strong"},
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Advanced Optimization",
            evidence_refs=["E1", "E2"],
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong reviews.", "evidence_refs": ["E1"]},
                {"claim": "The site has a strong estimate form.", "evidence_refs": ["E2"]},
            ],
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Advanced Optimization without concrete visible pain", row["Send Blockers"])
        self.assertIn("Missing concrete send trigger", row["Send Blockers"])

    def test_manual_score_with_high_score_and_low_confidence_gets_blocked_but_evidence_can_pass(self) -> None:
        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(site_status="weak"),
        }
        raw_score = self._manual_score(confidence="low")

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Lead Priority"], "B")
        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Low confidence", row["Send Blockers"])
        self.assertEqual(row["Minimum Evidence Passed"], "Yes")

    def test_no_site_lead_with_strong_reviews_passes_concrete_issue_gate(self) -> None:
        row = score_record(
            {
                "google": {
                    "business_name": "Ron & Sons Landscaping",
                    "city": "East Cleveland",
                    "phone": "(216) 555-1212",
                    "rating": "4.9",
                    "reviews": "31",
                    "website_url": "",
                    "google_maps_url": "https://maps.google.com/?cid=456",
                    "place_id": "ChIJ456",
                },
                "website": {
                    "site_status": "No Website",
                    "site_issue_flags": [],
                    "pages": {},
                    "emails_found": [],
                    "phones_found": [],
                    "forms_found": False,
                    "cta_terms": [],
                    "combined_text": "",
                },
                "derived": {},
                "screenshots": {},
                "buyer_intent": self._buyer_intent_fixture(),
            },
            "hardscaping",
        )

        self.assertEqual(row["Lead Priority"], "A")
        self.assertEqual(row["Sendable"], "Yes")
        self.assertEqual(row["Minimum Evidence Passed"], "Yes")
        self.assertEqual(row["Packet Eligible"], "Yes")
        self.assertIn("no owned website or conversion asset", row["Safe Outreach Claims"])

    def test_static_fetch_failure_with_rendered_load_is_not_broken_claim(self) -> None:
        record = {
            "google": {
                **self._google_fixture(),
                "business_name": "BG Contracting",
                "reviews": "18",
                "rating": "4.8",
            },
            "website": {
                "site_status": "Loaded",
                "rendered_site_status": "Loaded",
                "rendered_status_reason": "Browser rendered homepage successfully after static fetch failed.",
                "visual_review_needed": "Yes",
                "site_issue_flags": [
                    "static_fetch_failed",
                    "rendered_check_needed",
                    "manual_visual_review_needed",
                    "outdated_site",
                    "premium_perception_gap",
                ],
                "final_url": "https://bgcontracting.example",
                "domain": "bgcontracting.example",
                "homepage": {
                    "url": "https://bgcontracting.example",
                    "status_code": "",
                    "title": "",
                    "meta": "",
                    "text": "",
                    "error": "static request timed out",
                },
                "pages": {},
                "emails_found": ["office@bgcontracting.example"],
                "phones_found": ["(440) 555-0199"],
                "forms_found": False,
                "contact_form_detected": "Unknown",
                "estimate_form_detected": "Unknown",
                "form_strength": "None",
                "form_detection_method": "none",
                "cta_strength": "Unknown",
                "cta_terms": [],
                "high_ticket_terms": ["hardscaping", "retaining wall", "patio"],
                "generic_copy_terms": [],
                "combined_text": "",
            },
            "derived": {
                "phone_consistency": "matched",
                "has_form": False,
                "has_high_ticket_terms": True,
                "non_standard_site_or_booking": False,
            },
            "screenshots": {
                "desktop": "screenshots/bg-contracting-desktop.png",
                "mobile": "screenshots/bg-contracting-mobile.png",
                "error": "",
            },
            "buyer_intent": self._buyer_intent_fixture(),
        }
        record["evidence"] = build_evidence_profile(record)

        row = score_record(record, "hardscaping")

        self.assertEqual(row["Site Status"], "Loaded")
        self.assertEqual(row["Rendered Site Status"], "Loaded")
        self.assertEqual(row["Visual Review Needed"], "Yes")
        self.assertNotEqual(row["Send Trigger"], "Website appears broken")
        self.assertNotIn("website appears broken", row["Safe Outreach Claims"].lower())
        self.assertNotIn("appears broken", row["Opener"].lower())
        self.assertIn(row["Offer Fit"], {"Website Rebuild", "Premium Perception Gap", "Website Cleanup"})
        self.assertEqual(row["Sendable"], "Yes")
        self.assertEqual(row["Packet Eligible"], "Yes")

    def test_high_scoring_hallucinated_lead_gets_blocked_when_claims_are_ungrounded(self) -> None:
        enriched = {
            "google": self._google_fixture(),
            "evidence": self._evidence_fixture(
                site_status="okay",
                evidence_items=[
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 130 reviews"},
                    {"id": "E2", "field": "trust_signals", "value": "licensed, insured"},
                ],
            ),
        }
        raw_score = self._manual_score(
            safe_outreach_claims=[{"claim": "You are losing jobs because your form is broken.", "evidence_refs": []}]
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Missing concrete conversion issue", row["Send Blockers"])
        self.assertIn("Missing evidence-backed opener", row["Send Blockers"])
        self.assertIn("Safe outreach claim missing evidence reference", row["Send Blockers"])
        self.assertEqual(row["Minimum Evidence Passed"], "No")

    def test_packet_eligible_cannot_be_yes_when_sendable_is_no(self) -> None:
        row = {"Lead Priority": "A", "Packet Eligible": "Yes", "Sendable": "No"}

        self.assertFalse(is_eligible(row, include_all=False, priority_filter=None))
        self.assertTrue(is_eligible(row, include_all=True, priority_filter=None))

    # --- Packet tier: split packet creation from sendability ----------------

    def _facebook_only_evidence(self, *, reviews: str, reasons: list) -> dict:
        return {
            "google": {
                **self._google_fixture(),
                "business_name": "Facebook Only Detailing LLC",
                "rating": "5.0",
                "reviews": reviews,
            },
            "evidence": {
                "site_status": "weak",
                "visible_pain_score": 50,
                "visible_pain_reasons": reasons,
                "visible_pain_categories": ["no_site_or_social_only_pain", "estimate_path_pain"],
                "send_trigger": "Social-only web presence",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": f"5.0 stars / {reviews} reviews"},
                    {"id": "E2", "field": "site_status", "value": "weak"},
                    {"id": "E3", "field": "send_trigger", "value": "Social-only web presence"},
                ],
            },
        }

    def _facebook_only_score(self, **overrides: object) -> dict:
        base = dict(
            best_angle="No-Site Launch",
            evidence_refs=["E1", "E2", "E3"],
            visible_pain_score=50,
            visible_pain_reasons=[
                "No standalone website — business presence is Facebook-only",
                "No clear estimate CTA detected",
                "Phone number hard to find on the page",
            ],
            visible_pain_categories=["no_site_or_social_only_pain", "estimate_path_pain"],
            send_trigger="Social-only web presence",
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong review proof.", "evidence_refs": ["E1"]},
                {"claim": "The web presence relies on social pages, not an owned site.", "evidence_refs": ["E2", "E3"]},
            ],
            opener="Your reviews look strong, but I could not find a standalone website. Want me to send a direction?",
        )
        base.update(overrides)
        return self._manual_score(**base)

    def test_b_no_site_facebook_only_low_confidence_is_manual_review_packet(self) -> None:
        enriched = self._facebook_only_evidence(
            reviews="45",
            reasons=[
                "No standalone website — business presence is Facebook-only",
                "No clear estimate CTA detected",
                "Phone number hard to find on the page",
            ],
        )
        raw_score = self._facebook_only_score(confidence="low")

        row = merge_manual_score(enriched, raw_score, fallback_vertical="detailing")

        self.assertEqual(row["Lead Priority"], "B")
        self.assertEqual(row["Sendable"], "No")
        self.assertEqual(row["Minimum Evidence Passed"], "Yes")
        self.assertEqual(row["Packet Tier"], "Manual Review Packet")
        self.assertEqual(row["Packet Eligible"], "Yes")
        self.assertTrue(row["Manual Review Reason"])
        self.assertTrue(is_eligible(row, include_all=False, priority_filter=None))

    def test_refresh_packet_tiers_backfills_old_rehab_style_csv_row(self) -> None:
        from refresh_packet_tiers import refresh_csv

        enriched = self._facebook_only_evidence(
            reviews="45",
            reasons=[
                "No standalone website — business presence is Facebook-only",
                "No clear estimate CTA detected",
                "Phone number hard to find on the page",
            ],
        )
        enriched["google"]["business_name"] = "Rehab Auto Spa"
        raw_score = self._facebook_only_score(confidence="low")
        scored = merge_manual_score(enriched, raw_score, fallback_vertical="detailing")

        old_row = dict(scored)
        old_row.pop("Packet Tier", None)
        old_row.pop("Manual Review Reason", None)
        old_row["Packet Eligible"] = ""
        old_row["Packet Eligibility Reason"] = ""

        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "old_scored.csv"
            output_path = Path(tmp) / "refreshed.csv"
            fieldnames = [key for key in old_row if key not in {"Packet Tier", "Manual Review Reason"}]
            with input_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(old_row)

            refresh_csv(input_path, output_path)

            with output_path.open(newline="", encoding="utf-8") as f:
                [row] = list(csv.DictReader(f))

        self.assertEqual(row["Business Name"], "Rehab Auto Spa")
        self.assertEqual(row["Lead Priority"], scored["Lead Priority"])
        self.assertEqual(row["Packet Tier"], "Manual Review Packet")
        self.assertEqual(row["Packet Eligible"], "Yes")
        self.assertIn("Manual Review Packet", row["Packet Eligibility Reason"])
        self.assertTrue(row["Manual Review Reason"])

    def test_c_no_site_facebook_only_below_b_is_manual_review_packet(self) -> None:
        enriched = self._facebook_only_evidence(
            reviews="24",
            reasons=[
                "Business uses a Facebook profile as its primary web presence — no standalone website",
                "No estimate CTA or form detectable",
            ],
        )
        raw_score = self._facebook_only_score(
            subscores={
                "icp_fit": 10,
                "reputation_demand_proof": 10,
                "web_conversion_pain": 12,
                "mockup_leverage": 8,
                "reachability": 6,
                "offer_angle_clarity": 5,
                "evidence_confidence": 4,
            },
            visible_pain_reasons=[
                "Business uses a Facebook profile as its primary web presence — no standalone website",
                "No estimate CTA or form detectable",
            ],
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="detailing")

        self.assertEqual(row["Lead Priority"], "C")
        self.assertEqual(row["Sendable"], "No")
        self.assertIn("Score below B threshold", row["Send Blockers"])
        self.assertEqual(row["Minimum Evidence Passed"], "Yes")
        self.assertEqual(row["Packet Tier"], "Manual Review Packet")
        self.assertEqual(row["Packet Eligible"], "Yes")

    def test_c_estimate_funnel_strong_cta_low_visible_pain_is_not_manual_review(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "business_name": "Luminix Detailing and Ceramic Coating",
                "rating": "5.0",
                "reviews": "15",
            },
            "evidence": {
                "site_status": "okay",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Basic",
                "cta_strength": "Strong",
                "visible_pain_score": 12,
                "visible_pain_reasons": ["Basic estimate form only — no paid CRM or booking tool detected"],
                "visible_pain_categories": ["estimate_path_pain"],
                "send_trigger": "Basic estimate form only",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "5.0 stars / 15 reviews"},
                    {"id": "E2", "field": "form_strength", "value": "Basic"},
                    {"id": "E3", "field": "send_trigger", "value": "Basic estimate form only"},
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Estimate Funnel",
            subscores={
                "icp_fit": 11,
                "reputation_demand_proof": 10,
                "web_conversion_pain": 12,
                "mockup_leverage": 8,
                "reachability": 6,
                "offer_angle_clarity": 5,
                "evidence_confidence": 3,
            },
            evidence_refs=["E1", "E2", "E3"],
            visible_pain_score=12,
            visible_pain_reasons=["Basic estimate form only — no paid CRM or booking tool detected"],
            visible_pain_categories=["estimate_path_pain"],
            send_trigger="Basic estimate form only",
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong review proof.", "evidence_refs": ["E1"]},
                {"claim": "The estimate form is basic for the demand signal.", "evidence_refs": ["E2", "E3"]},
            ],
            opener="Your reviews look strong; the estimate form looks basic for that demand.",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="detailing")

        self.assertEqual(row["Lead Priority"], "C")
        self.assertEqual(row["Sendable"], "No")
        self.assertNotEqual(row["Packet Tier"], "Manual Review Packet")
        self.assertEqual(row["Packet Eligible"], "")
        self.assertFalse(is_eligible(row, include_all=False, priority_filter=None))

    def test_skip_social_only_high_pain_becomes_watchlist_not_manual_review_packet(self) -> None:
        # Skip grade (total < 50) + social-only pain: should land in Watchlist, never Manual Review Packet.
        enriched = self._facebook_only_evidence(
            reviews="45",
            reasons=[
                "No standalone website — business presence is Facebook-only",
                "No clear estimate CTA detected",
            ],
        )
        raw_score = self._facebook_only_score(
            subscores={
                "icp_fit": 8,
                "reputation_demand_proof": 7,
                "web_conversion_pain": 10,
                "mockup_leverage": 6,
                "reachability": 5,
                "offer_angle_clarity": 5,
                "evidence_confidence": 4,
            },
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="detailing")

        self.assertEqual(row["Lead Priority"], "Skip")
        self.assertEqual(row["Sendable"], "No")
        self.assertNotEqual(row["Packet Tier"], "Manual Review Packet")
        self.assertEqual(row["Packet Tier"], "Watchlist")
        self.assertEqual(row["Packet Eligible"], "")
        self.assertFalse(is_eligible(row, include_all=False, priority_filter=None))

    def test_advanced_optimization_strong_form_no_issue_is_no_packet(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "reviews": "40",
            },
            "evidence": {
                "site_status": "okay",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "cta_strength": "Strong",
                "visible_pain_score": 0,
                "visible_pain_reasons": [],
                "send_trigger": "",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 40 reviews"},
                    {"id": "E2", "field": "form_strength", "value": "Strong"},
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Advanced Optimization",
            evidence_refs=["E1", "E2"],
            safe_outreach_claims=[
                {"claim": "Your Google profile shows strong reviews.", "evidence_refs": ["E1"]},
                {"claim": "The site has a strong estimate form.", "evidence_refs": ["E2"]},
            ],
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="detailing")

        self.assertEqual(row["Sendable"], "No")
        self.assertEqual(row["Packet Tier"], "No Packet")
        self.assertEqual(row["Packet Eligible"], "")
        self.assertFalse(is_eligible(row, include_all=False, priority_filter=None))

    def test_sendable_lead_is_sendable_packet(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "reviews": "18",
            },
            "buyer_intent": self._buyer_intent_fixture(),
            "evidence": {
                "site_status": "missing",
                "visible_pain_score": 40,
                "visible_pain_reasons": ["No owned website found"],
                "visible_pain_categories": ["no_site_or_social_only_pain"],
                "send_trigger": "No owned website found",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 18 reviews"},
                    {"id": "E2", "field": "site_status", "value": "missing"},
                    {"id": "E3", "field": "send_trigger", "value": "No owned website found"},
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Website Rebuild",
            evidence_refs=["E1", "E2", "E3"],
            visible_pain_score=40,
            visible_pain_reasons=["No owned website found"],
            visible_pain_categories=["no_site_or_social_only_pain"],
            send_trigger="No owned website found",
            safe_outreach_claims=[
                {"claim": "No owned website is visible.", "evidence_refs": ["E2", "E3"]},
                {"claim": "The Google profile has review proof.", "evidence_refs": ["E1"]},
            ],
            opener="I could not find an owned website, only the Google profile.",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "Yes")
        self.assertEqual(row["Packet Tier"], "Sendable Packet")
        self.assertEqual(row["Packet Eligible"], "Yes")
        self.assertEqual(row["Manual Review Reason"], "")
        self.assertTrue(is_eligible(row, include_all=False, priority_filter=None))

    def test_score_record_uses_record_vertical_profile_for_detailing_denylist(self) -> None:
        row = score_record(
            {
                "google": {
                    **self._google_fixture(),
                    "business_name": "Mister Car Wash Cleveland",
                    "category": "Car detailing service",
                    "source_query": "ceramic coating Cleveland OH",
                    "vertical": "detailing",
                    "rating": "4.8",
                    "reviews": "28",
                },
                "website": {
                    "site_status": "No Website",
                    "site_issue_flags": [],
                    "combined_text": "ceramic coating paint correction ppf",
                    "high_ticket_terms": ["ceramic coating", "paint correction", "ppf"],
                },
                "derived": {
                    "has_high_ticket_terms": True,
                    "has_social_links": False,
                },
                "evidence": {
                    "site_status": "No Website",
                    "visible_pain_score": 40,
                    "visible_pain_reasons": ["No owned website found"],
                    "visible_pain_categories": ["no_site_or_social_only_pain"],
                    "send_trigger": "No owned website found",
                    "evidence_items": [
                        {"id": "ev-rating", "field": "google_rating", "value": "4.8 / 28"},
                        {"id": "ev-trigger", "field": "send_trigger", "value": "No owned website found"},
                    ],
                },
            },
            "hardscaping",
        )

        self.assertEqual(row["Lead Priority"], "Skip")
        self.assertIn("franchise/brand denylist", row["Fit Flags"])

    def test_no_money_bad_site_cannot_be_deterministic_a(self) -> None:
        row = score_record(
            {
                "google": {
                    **self._google_fixture(),
                    "rating": "4.9",
                    "reviews": "18",
                },
                "website": {
                    "site_status": "No Website",
                    "site_issue_flags": [],
                    "combined_text": "hardscape patio retaining wall",
                    "high_ticket_terms": ["hardscape", "patio", "retaining wall"],
                },
                "buyer_intent": {
                    "crm_booking_stack": {"detected_tools": [], "has_paid_crm": False, "evidence": []},
                    "review_velocity": {"velocity_band": "steady", "source": "count_only", "evidence": []},
                    "ads": {
                        "google_ad_transparency": "unknown",
                        "meta_ad_library": "unknown",
                        "lsa_badge": "unknown",
                        "errors": [],
                    },
                    "money_signal": False,
                    "evidence": [],
                },
                "derived": {"has_high_ticket_terms": True},
            },
            "hardscaping",
        )

        self.assertNotEqual(row["Lead Priority"], "A")
        self.assertIn(row["Lead Priority"], {"C", "Skip"})

    def test_money_signal_defends_high_review_bad_site_deterministic_tier(self) -> None:
        base_record = {
            "google": {
                **self._google_fixture(),
                "rating": "4.9",
                "reviews": "92",
            },
            "website": {
                "site_status": "No Website",
                "site_issue_flags": [],
                "combined_text": "hardscape patio retaining wall ServiceTitan",
                "high_ticket_terms": ["hardscape", "patio", "retaining wall"],
            },
            "buyer_intent": {
                "logic_version": BUYER_INTENT_LOGIC_VERSION,
                "crm_booking_stack": {
                    "detected_tools": ["ServiceTitan"],
                    "has_paid_crm": True,
                    "evidence": ["Detected paid CRM/booking tool: ServiceTitan"],
                },
                "review_velocity": {"velocity_band": "live", "source": "count_only", "evidence": []},
                "ads": {
                    "google_ad_transparency": "unknown",
                    "meta_ad_library": "unknown",
                    "lsa_badge": "unknown",
                    "errors": [],
                },
                "money_signal": True,
                "evidence": ["Paid CRM/booking stack detected"],
            },
            "derived": {"has_high_ticket_terms": True},
        }

        row = score_record(base_record, "hardscaping")

        self.assertEqual(row["Lead Priority"], "A")
        self.assertEqual(row["Money Signal"], "Yes")

    def test_score_record_surfaces_ideal_growth_review_demand_band(self) -> None:
        row = score_record(
            {
                "google": {
                    **self._google_fixture(),
                    "rating": "4.7",
                    "reviews": "20",
                },
                "website": {
                    "site_status": "Loaded",
                    "site_issue_flags": ["no_clear_cta_detected", "weak_basic_form"],
                    "combined_text": "hardscape patio retaining wall estimate",
                    "high_ticket_terms": ["hardscape", "patio", "retaining wall"],
                    "forms_found": True,
                    "contact_form_detected": "Yes",
                    "estimate_form_detected": "Yes",
                    "form_strength": "Weak",
                    "cta_strength": "Weak",
                },
                "evidence": {
                    "site_status": "weak",
                    "visible_pain_score": 35,
                    "visible_pain_reasons": ["Weak estimate path"],
                    "visible_pain_categories": ["estimate_path_pain"],
                    "send_trigger": "Weak estimate path",
                    "evidence_items": [
                        {"id": "E1", "field": "google_rating", "value": "4.7 stars / 20 reviews"},
                        {"id": "E2", "field": "form_strength", "value": "Weak"},
                        {"id": "E3", "field": "send_trigger", "value": "Weak estimate path"},
                    ],
                },
                "derived": {"has_high_ticket_terms": True, "has_form": True},
            },
            "hardscaping",
        )

        self.assertEqual(row["Review Demand Band"], "ideal_growth")
        self.assertEqual(row["Review Fit Band"], "ideal_growth")

    def test_batch_prompt_separates_money_intent_from_count_only_review_demand(self) -> None:
        prompt = build_batch_prompt([], batch_id="batch_test")

        self.assertIn("Paid CRM/booking tools and known ad signals can support money intent.", prompt)
        self.assertIn("Count-only review demand bands support reputation fit, not money intent by themselves.", prompt)
        self.assertNotIn("live review velocity", prompt)

    def test_mature_high_review_strong_funnel_is_not_sendable(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "rating": "5.0",
                "reviews": "260",
            },
            "evidence": {
                "site_status": "okay",
                "contact_form_detected": "Yes",
                "estimate_form_detected": "Yes",
                "form_strength": "Strong",
                "cta_strength": "Strong",
                "visible_pain_score": 0,
                "visible_pain_reasons": [],
                "visible_pain_categories": [],
                "send_trigger": "",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "5.0 stars / 260 reviews"},
                    {"id": "E2", "field": "form_strength", "value": "Strong estimate form"},
                ],
            },
        }
        raw_score = self._manual_score(
            best_angle="Review/Trust Proof Upgrade",
            evidence_refs=["E1", "E2"],
            safe_outreach_claims=[
                {"claim": "Your Google profile shows mature review proof.", "evidence_refs": ["E1"]},
                {"claim": "The site has a strong estimate form.", "evidence_refs": ["E2"]},
            ],
            opener="Your reviews are strong and the estimate form is visible.",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Sendable"], "No")
        self.assertEqual(row["Packet Eligible"], "")
        self.assertIn("Missing concrete conversion issue", row["Send Blockers"])
        self.assertIn("Visible conversion pain too weak for business maturity", row["Send Blockers"])

    def test_mature_high_review_missing_estimate_path_can_be_sendable_with_money_signal(self) -> None:
        enriched = {
            "google": {
                **self._google_fixture(),
                "rating": "4.9",
                "reviews": "260",
            },
            "buyer_intent": self._buyer_intent_fixture(),
            "evidence": {
                "site_status": "weak",
                "contact_form_detected": "No",
                "estimate_form_detected": "No",
                "form_strength": "None",
                "cta_strength": "Weak",
                "visible_pain_score": 45,
                "visible_pain_reasons": ["Missing estimate path"],
                "visible_pain_categories": ["estimate_path_pain"],
                "send_trigger": "Missing estimate path",
                "evidence_items": [
                    {"id": "E1", "field": "google_rating", "value": "4.9 stars / 260 reviews"},
                    {"id": "E2", "field": "site_issue_flags", "value": "missing_estimate_path"},
                    {"id": "E3", "field": "send_trigger", "value": "Missing estimate path"},
                    {"id": "buyer_intent", "field": "buyer_intent", "value": "paid CRM detected"},
                ],
            },
        }
        raw_score = self._manual_score(
            evidence_refs=["E1", "E2", "E3", "buyer_intent"],
            visible_pain_score=45,
            visible_pain_reasons=["Missing estimate path"],
            visible_pain_categories=["estimate_path_pain"],
            send_trigger="Missing estimate path",
            safe_outreach_claims=[
                {"claim": "Your Google profile shows mature review proof.", "evidence_refs": ["E1"]},
                {"claim": "The estimate path appears missing.", "evidence_refs": ["E2", "E3"]},
            ],
            opener="Your reviews look strong, but I could not find a clear estimate path.",
        )

        row = merge_manual_score(enriched, raw_score, fallback_vertical="hardscaping")

        self.assertEqual(row["Money Signal"], "Yes")
        self.assertEqual(row["Sendable"], "Yes")
        self.assertNotIn("Visible conversion pain too weak for business maturity", row["Send Blockers"])

    def _google_fixture(self) -> dict:
        return {
            "business_name": "A+ Landscaping LLC",
            "city": "Kirtland",
            "phone": "(440) 637-5848",
            "website_url": "https://example.com",
            "google_maps_url": "https://maps.google.com/?cid=123",
            "rating": "4.9",
            "reviews": "130",
            "place_id": "ChIJ123",
        }

    def _buyer_intent_fixture(self) -> dict:
        return {
            "logic_version": BUYER_INTENT_LOGIC_VERSION,
            "crm_booking_stack": {
                "detected_tools": ["ServiceTitan"],
                "has_paid_crm": True,
                "evidence": ["Detected paid CRM/booking tool: ServiceTitan"],
            },
            "review_velocity": {
                "velocity_band": "steady",
                "source": "count_only",
                "evidence": ["18 Google reviews with 4.8 rating"],
            },
            "ads": {
                "google_ad_transparency": "unknown",
                "meta_ad_library": "unknown",
                "lsa_badge": "unknown",
                "errors": [],
            },
            "money_signal": True,
            "evidence": ["Paid CRM/booking stack detected"],
        }

    def _evidence_fixture(self, *, site_status: str, evidence_items: list | None = None) -> dict:
        return {
            "site_status": site_status,
            "screenshots": {"desktop": "desktop.png", "mobile": "mobile.png"},
            "visible_pain_score": 35 if site_status == "weak" else 0,
            "visible_pain_reasons": ["No clear estimate CTA"] if site_status == "weak" else [],
            "send_trigger": "No clear estimate CTA" if site_status == "weak" else "",
            "evidence_items": evidence_items
            or [
                {"id": "E1", "field": "google_rating", "value": "4.9 stars / 130 reviews"},
                {"id": "E2", "field": "site_issue_flags", "value": "no_clear_cta_detected"},
                {"id": "E3", "field": "send_trigger", "value": "No clear estimate CTA"},
            ],
        }

    def _manual_score(self, **overrides: object) -> dict:
        score = {
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
        score.update(overrides)
        return score


if __name__ == "__main__":
    unittest.main()
