#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import AbstractSet, Any, Dict, Iterable, List, Sequence
from urllib.parse import quote

import requests

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False

from lead_machine.email_hygiene import clean_airtable_email
from lead_machine.airtable_sync import (
    AI_CONFIDENCE_CHOICES,
    AIRTABLE_ALLOWED_FIELDS,
    ICP_LANE_CHOICES,
    OFFER_FIT_CHOICES,
    SITE_STATUS_CHOICES,
    TYPED_AIRTABLE_FIELDS,
    VERTICAL_CHOICES,
    build_airtable_payload,
    card_phone_key,
    card_place_key,
    normalize_business_name,
    normalize_city,
    normalize_airtable_value,
    plan_airtable_sync,
)
from lead_machine.icp_lanes import classify_icp_lane
from lead_machine.queue_state import PROSPECT_STATUS_CHOICE_SET
from lead_machine.score_schema import (
    ASSET_STATES,
    COMMERCIAL_FIT_STATES,
    CONTACT_ELIGIBILITY_STATES,
    PROMISE_STATES,
)
from lead_machine.schema import GENERIC_DOMAINS, normalize_domain, normalize_phone
from lead_machine.setter_assignment import (
    assign_setter,
    assignment_identity,
    configured_setter_roster,
)
from lead_machine.sendability import (
    PACKET_TIER_MANUAL,
    PACKET_TIER_NONE,
    PACKET_TIER_SENDABLE,
    PACKET_TIER_CHOICES,
    SCORER_WITHHELD_BLOCKER,
    admit,
    evaluate_sendability,
    hard_icp_skip_blockers,
    has_hard_disqualifier,
    is_contact_ready,
)
from lead_machine.verdict import (
    VERDICT_CHOICES,
    VERDICT_FIELD,
    VERDICT_REASONS_FIELD,
    verdict_for_tier,
)
from mark_sent import OUTREACH_CHANNEL_CHOICES
from score_leads import LEAD_PRIORITY_CHOICES


# Alias the canonical tiers rather than restating the literals: sendability
# owns these strings, and a silent divergence here is exactly the class of bug
# this schema cut exists to remove.
_TIER_SENDABLE = PACKET_TIER_SENDABLE
_TIER_MANUAL = PACKET_TIER_MANUAL
_TIER_NONE = PACKET_TIER_NONE

# Every single select the write path actually sends. `airtable_required_choices`
# reads this to refuse when the base lacks a choice the code can write, so a
# column the write path no longer sends must not be listed: after step 4 of the
# verdict cutover the field is gone, and demanding it would fail the gate over a
# column nobody writes. The five readiness selects left here alongside `Packet
# Tier` — `Verdict` is the one that replaced all six.
WRITE_PATH_SELECT_CHOICES: dict[str, frozenset[str]] = {
    "Vertical": VERTICAL_CHOICES,
    VERDICT_FIELD: VERDICT_CHOICES,
    "Lead Priority": LEAD_PRIORITY_CHOICES,
    "Offer Fit": OFFER_FIT_CHOICES,
    "Site Status": SITE_STATUS_CHOICES,
    "AI Confidence": AI_CONFIDENCE_CHOICES,
    "ICP Lane": ICP_LANE_CHOICES,
    "Status": PROSPECT_STATUS_CHOICE_SET,
    "Outreach Channel": OUTREACH_CHANNEL_CHOICES,
    # The actual choices are runtime configuration, not a checked-in roster.
    # `configured_write_path_select_choices()` fills this from the same source
    # the assignment writer uses before the live-schema gate runs.
    "Assigned Setter": frozenset(),
}


def configured_write_path_select_choices() -> dict[str, frozenset[str]]:
    choices = dict(WRITE_PATH_SELECT_CHOICES)
    choices["Assigned Setter"] = frozenset(configured_setter_roster())
    return choices


def _select_options_note(field: str) -> str:
    if field == "Assigned Setter":
        return "Options: configured by LPM_SETTER_ROSTER"
    return "Options: " + ", ".join(sorted(WRITE_PATH_SELECT_CHOICES[field]))


# (field, airtable_type, notes)
_FIELD_SCHEMA: List[tuple[str, str, str]] = [
    ("Business Name",          "Single line text",   ""),
    ("Owner Name",             "Single line text",   ""),
    ("Vertical",               "Single select",      _select_options_note("Vertical")),
    ("City",                   "Single line text",   ""),
    ("Metro Context",          "Single line text",   "Authoritative Operator metro-context slug"),
    ("Phone",                  "Phone number",       ""),
    ("Email",                  "Email",              ""),
    ("Website URL",            "URL",                ""),
    ("Google Maps URL",        "URL",                ""),
    ("Reviews",                "Number",             "Integer; set field to 'Integer' precision"),
    ("Rating",                 "Number",             "Decimal; set field to 2 decimal places"),
    ("Specific Observation",   "Long text",          ""),
    (VERDICT_FIELD,            "Single select",      _select_options_note(VERDICT_FIELD)),
    (VERDICT_REASONS_FIELD,    "Long text",          "Why the verdict is what it is; machine-owned"),
    # Skip is a real grade the scorer writes on every disqualified lead, not a
    # placeholder. It was missing from this line while the live base also carried
    # junk choices next to it, which is exactly the reading that gets it deleted.
    ("Lead Priority",          "Single select",      _select_options_note("Lead Priority")),
    ("Offer Fit",              "Single select",      _select_options_note("Offer Fit")),
    ("Site Status",            "Single select",      _select_options_note("Site Status")),
    ("Visible Pain Score",     "Number",             "Integer 0–100"),
    ("Visible Pain Reasons",   "Long text",          ""),
    ("Send Trigger",           "Single line text",   ""),
    ("Buyer Intent Signals",   "Long text",          "Comma-separated signals detected by LLM"),
    ("LPM Offer Component",    "Single line text",   "One authoritative offer component"),
    ("Evidence Refs",          "Long text",          "Structured evidence identifiers"),
    ("Safe Outreach Claims",   "Long text",          "Claims bound to evidence identifiers"),
    ("AI Confidence",          "Single select",      _select_options_note("AI Confidence")),
    ("Score Total",            "Number",             "Integer"),
    ("ICP Lane",               "Single select",      _select_options_note("ICP Lane")),
    ("Opportunity Score",      "Number",             "Integer 0–100; the icp_lanes score behind ICP Lane"),
    ("Opener",                 "Long text",          ""),
    ("Place Key",              "Single line text",   "Used as upsert key; must be unique per business"),
    ("Assigned Setter",        "Single select",      _select_options_note("Assigned Setter")),
    ("Status",                 "Single select",      _select_options_note("Status")),
    ("Outreach Channel",       "Single select",      _select_options_note("Outreach Channel")),
    ("Consent Posture",        "Single line text",   "Required and documented before any SMS draft can pass"),
    ("Last Contacted",         "Date",               ""),
    ("Next Follow-Up",         "Date",               ""),
    ("Mockup URL",             "URL",                ""),
    ("Video URL",              "URL",                ""),
    ("Packet Path",            "Single line text",   "Local file path to the packet markdown file"),
]


def build_schema_report(
    existing_field_names: set[str] | None = None,
) -> List[Dict[str, Any]]:
    """Return a list of dicts describing every field the pipeline may write.

    If *existing_field_names* is provided, the ``exists`` key reflects whether
    the field is present in that set.  When ``None`` the field is unchecked and
    ``exists`` is ``None``.
    """
    rows: List[Dict[str, Any]] = []
    for field, atype, notes in _FIELD_SCHEMA:
        if existing_field_names is None:
            exists: bool | None = None
        else:
            exists = field in existing_field_names
        rows.append({"field": field, "type": atype, "exists": exists, "notes": notes})
    return rows


def _exists_marker(exists: bool | None) -> str:
    if exists is True:
        return "YES"
    if exists is False:
        return "MISSING"
    return "?"


def print_schema_report(report: List[Dict[str, Any]], file: Any = None) -> None:
    if file is None:
        file = sys.stdout
    col_field = max(len(r["field"]) for r in report) + 2
    col_type = max(len(r["type"]) for r in report) + 2
    header = f"{'Field':<{col_field}}{'Airtable Type':<{col_type}}{'Exists':<10}Notes"
    print(header, file=file)
    print("-" * len(header), file=file)
    for row in report:
        print(
            f"{row['field']:<{col_field}}{row['type']:<{col_type}}"
            f"{_exists_marker(row['exists']):<10}{row['notes']}",
            file=file,
        )


def _write_schema_markdown(report: List[Dict[str, Any]], path: Path) -> None:
    lines = [
        "# Airtable Schema — Required Fields\n",
        "| Field | Airtable Type | Exists | Notes |",
        "|-------|--------------|--------|-------|",
    ]
    for row in report:
        lines.append(
            f"| {row['field']} | {row['type']} | {_exists_marker(row['exists'])} | {row['notes']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_schema_csv(report: List[Dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["field", "type", "exists", "notes"])
        writer.writeheader()
        for row in report:
            writer.writerow({
                "field": row["field"],
                "type": row["type"],
                "exists": _exists_marker(row["exists"]),
                "notes": row["notes"],
            })


def write_schema_report(report: List[Dict[str, Any]], path: Path) -> None:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        _write_schema_csv(report, path)
    else:
        _write_schema_markdown(report, path)


CSV_AIRTABLE_FIELDS = [
    "Business Name",
    "Owner Name",
    "Vertical",
    "City",
    "Metro Context",
    "Phone",
    "Email",
    "Website URL",
    "Google Maps URL",
    "Reviews",
    "Rating",
    "Specific Observation",
    "Packet Tier",
    VERDICT_FIELD,
    VERDICT_REASONS_FIELD,
    "Lead Priority",
    "Offer Fit",
    "Site Status",
    "Visible Pain Score",
    "Visible Pain Reasons",
    "Send Trigger",
    "Buyer Intent Signals",
    "Commercial Fit State",
    "LPM Offer Component",
    "Contact Eligibility",
    "Asset State",
    "Promise State",
    "Evidence Refs",
    "Safe Outreach Claims",
    "AI Confidence",
    "Skip Reason",
    "Score Total",
    "ICP Lane",
    "Opportunity Score",
    "Opener",
    "Send Blockers",
    "Place Key",
    "Mockup URL",
    "Video URL",
    "Last Contacted",
    "Next Follow-Up",
    "Consent Posture",
]

OPERATIONAL_FIELDS = [
    "Status",
    "Last Contacted",
    "Next Follow-Up",
    "Outreach Channel",
]
GENERATED_OPERATIONAL_FIELDS = ["Assigned Setter"]
LEGACY_OUTREACH_CHANNEL_PLACEHOLDERS = frozenset({"Google Maps"})
OPTIONAL_FIELDS = ["Packet Path"]
DEFAULT_PACKET_DIR = Path("packets_manual_existing_manual")

ICP_LANE_FIELD = "ICP Lane"
OPPORTUNITY_SCORE_FIELD = "Opportunity Score"


def raw_evidence_record(row: Dict[str, Any]) -> Dict[str, Any]:
    """The enriched record a scored row carries, or an empty dict.

    Both scorers dump the whole enriched record into `Raw Evidence` before
    writing the CSV, which is the only place the upload path can still reach the
    structured evidence a flat row has already collapsed.
    """
    raw = row.get("Raw Evidence") or row.get("raw_evidence")
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def icp_lane_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    """Lane and opportunity score for a scored row, derived not copied.

    No scorer writes these as CSV columns; `classify_icp_lane` is the one
    authority for both, so this re-derives them from the record the same way
    `is_sendable_record` re-derives the ICP verdict instead of trusting a stored
    value. A row whose record cannot be recovered gets neither field: an absent
    cell reads as "never classified", whereas classifying an empty dict would
    mint a confident `skip` for a lead nothing ever looked at.
    """
    record = raw_evidence_record(row)
    if not record:
        return {}
    result = classify_icp_lane(record)
    # int, not str: no write path here sets typecast, so a stringified number
    # into an Airtable Number field is a 422 rather than a coercion.
    return {ICP_LANE_FIELD: result.lane, OPPORTUNITY_SCORE_FIELD: int(result.score)}


def read_cards(path: Path) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cards.append(json.loads(line))
    return cards


def truthy_yes(value: Any) -> bool:
    return str(value or "").strip().lower() in {"yes", "true", "1"}


def is_sendable_record(record: Dict[str, Any]) -> bool:
    """Readiness for upload, re-derived through `admit` rather than trusted.

    A packet card's `sendable` is a snapshot of what the scorer decided, and this
    is the last read before the record reaches Airtable. The hard ICP verdict is
    re-checked here for the same reason suppression is re-checked at every stage
    that can still stop a lead: a scored row carries it in Raw Evidence, and a
    stage that only reads a stored boolean cannot notice when the two disagree.

    The commercial tribunal used to supply a guard here by accident: a legacy CSV
    carrying nothing but `Sendable=Yes,Packet Eligible=Yes` failed the readiness
    contract because it did not also carry `Commercial Fit State`. Those columns
    were standing in for "was this row decided by the current pipeline". With the
    tribunal gone that proxy goes too, and a row with no `Verdict` is exactly a
    row nothing in this codebase has judged.

    So such a row is judged here, by the authority itself. An earlier version of
    this guard asked a narrower question — does the row name a concrete leak —
    and that was wrong twice over. It let a `dnc_suppressed` business upload as
    `Verdict=send, Status=New`, which is the rep's dial queue, and it let 19
    named businesses through carrying claims `evaluate_sendability` refuses:
    openers asserting a missing estimate form on a page that was never parsed,
    and claims citing evidence ids absent from their own snapshot. Those are the
    two blockers that exist to stop a misrepresentation to a business owner.

    A predicate weaker than the authority is a second sendable path, and
    AGENTS.md allows exactly one: "`sendability.admit()` is the only place a lead
    may be declared sendable. Any new sendable path goes through `admit`." This
    goes through it.

    Rows that do carry a `Verdict` are left to `readiness_flags`, which already
    re-runs the admission over the stored value in the tightening direction.
    Re-deciding them here would second-guess the gate with a copy of itself.
    """
    fields = record.get("airtable_fields") or record
    readiness = dict(fields)
    if record.get("sendable") is not None:
        blockers = () if truthy_yes(record.get("sendable")) else (SCORER_WITHHELD_BLOCKER,)
        readiness.setdefault("Sendable", admit(record, blockers).sendable_flag)
    if record.get("packet_eligible") is not None:
        readiness.setdefault("Packet Eligible", "Yes" if truthy_yes(record.get("packet_eligible")) else "No")
    # Merged, not `record` alone: `csv_row_to_record` nests every column under
    # `airtable_fields`, so judging the bare record would hand the authority an
    # almost-empty dict and refuse everything. JSONL cards carry their evidence
    # at the top level instead, and the merge lets one call read both shapes.
    if not clean_cell(fields.get("Verdict")) and not evaluate_sendability({**record, **fields})["sendable"]:
        return False
    # Unconditional, unlike the setdefault above: a row that already carries
    # Sendable=Yes from an older CSV would otherwise keep it.
    return not hard_icp_skip_blockers(record) and is_contact_ready(readiness)


def clean_cell(value: Any) -> str:
    return str(value or "").strip()


def coerce_airtable_value(field: str, value: Any) -> Any:
    cleaned = clean_cell(value)
    if cleaned == "":
        return ""
    if field in TYPED_AIRTABLE_FIELDS:
        normalized = normalize_airtable_value(field, cleaned)
        if normalized is None:
            return ""
        return normalized
    return cleaned


def safe_slug(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


ARCHIVED_RUNS_DIR = Path("runs")


def infer_packet_dir(input_path: Path) -> Path:
    """Locate the packet directory for a scored CSV.

    Completed runs are archived under `runs/` to keep the repo root navigable,
    so a directory that is not at the root may still exist one level down. New
    runs keep writing to the root, which is why the root is checked first.
    """
    stem = input_path.stem
    prefix = "scored_"
    suffix = "_manual"
    if stem.startswith(prefix) and stem.endswith(suffix):
        batch = stem[len(prefix) : -len(suffix)]
        if batch:
            name = f"packets_{batch}_manual"
            candidate = Path(name)
            if candidate.is_dir() or not (ARCHIVED_RUNS_DIR / name).is_dir():
                return candidate
            return ARCHIVED_RUNS_DIR / name
    if not DEFAULT_PACKET_DIR.is_dir() and (ARCHIVED_RUNS_DIR / DEFAULT_PACKET_DIR.name).is_dir():
        return ARCHIVED_RUNS_DIR / DEFAULT_PACKET_DIR.name
    return DEFAULT_PACKET_DIR


def path_from_cwd(path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return Path.cwd() / path


def packet_path_exists(path_value: str) -> bool:
    return path_from_cwd(path_value).exists()


def packet_path_is_inside_dir(path_value: str, packet_dir: Path) -> bool:
    try:
        path = path_from_cwd(path_value).resolve()
        root = path_from_cwd(str(packet_dir)).resolve()
        path.relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def existing_packet_path(row: Dict[str, str], packet_dir: Path) -> str:
    if not packet_dir.exists():
        return ""

    business = clean_cell(row.get("Business Name"))
    business_key = normalize_business_name(business)
    city = clean_cell(row.get("City"))
    city_key = normalize_city(city)
    place_key = clean_cell(row.get("Place Key"))

    index_path = packet_dir / "packet_index.csv"
    if index_path.exists():
        with index_path.open(newline="", encoding="utf-8") as f:
            indexed_rows = list(csv.DictReader(f))

        def matched_packet_path(index_row: dict[str, str], identity: str) -> str:
            indexed_path = clean_cell(index_row.get("Packet Path"))
            if not indexed_path:
                raise ValueError(f"Packet index match for {identity} has no Packet Path")
            if not packet_path_exists(indexed_path):
                raise ValueError(f"Packet index match for {identity} points to a missing packet: {indexed_path}")
            return indexed_path

        if place_key:
            matches = [item for item in indexed_rows if clean_cell(item.get("Place Key")) == place_key]
            if len(matches) > 1:
                raise ValueError(f"Packet index has duplicate Place Key {place_key}")
            return matched_packet_path(matches[0], f"Place Key {place_key}") if matches else ""

        if not business_key or not city_key:
            return ""
        matches = [
            item
            for item in indexed_rows
            if normalize_business_name(clean_cell(item.get("Business Name"))) == business_key
            and normalize_city(clean_cell(item.get("City"))) == city_key
        ]
        if len(matches) > 1:
            raise ValueError(f"Packet index has ambiguous legacy identity for {business}, {city}")
        return matched_packet_path(matches[0], f"{business}, {city}") if matches else ""

    return ""


def current_packet_path(row: Dict[str, str], packet_dir: Path) -> str:
    packet_path = existing_packet_path(row, packet_dir)
    if packet_path:
        return packet_path

    explicit_path = clean_cell(row.get("Packet Path"))
    if not explicit_path or not packet_path_exists(explicit_path):
        return ""
    if packet_dir.exists() and not packet_path_is_inside_dir(explicit_path, packet_dir):
        return ""
    return explicit_path


def csv_row_to_record(row: Dict[str, str], packet_dir: Path) -> Dict[str, Any]:
    business_name = clean_cell(row.get("Business Name"))
    if not business_name:
        raise ValueError("CSV row missing required Business Name")

    fields: Dict[str, Any] = {}
    defaulted_fields: List[str] = []
    for field in CSV_AIRTABLE_FIELDS:
        if field == "Vertical":
            value = clean_cell(row.get(field, ""))
            if value:
                fields[field] = value
            continue
        value = coerce_airtable_value(field, row.get(field, ""))
        if value != "":
            fields[field] = value

    # After the column loop, so a derived lane wins over a stale one a CSV may
    # already carry rather than the other way round.
    fields.update(icp_lane_fields(row))

    def mark_defaulted(field: str) -> None:
        if field not in defaulted_fields:
            defaulted_fields.append(field)

    # Operational columns are human-owned the moment a rep touches the lead, and
    # a scored CSV always carries a machine-written `Status: "New"`
    # (score_leads.py) — so copying them through to a PATCH overwrites whatever
    # the human set. That is how a dialled lead went back to `New`, re-entered
    # `queue_state.QUEUE_FILTER_FORMULA`, and got contacted a second time, and
    # how a `Won` reverted and stopped counting toward the accept-rate KPI.
    #
    # Marking them defaulted keeps them on the POST that creates the record and
    # strips them from every PATCH (airtable_sync.plan_airtable_sync), which is
    # the ownership contract `lead_machine/verdict.py` already documents:
    # `Status` is never machine-written except by mark_sent / mark_outcome.
    for field in OPERATIONAL_FIELDS:
        value = clean_cell(row.get(field, ""))
        if value:
            fields[field] = value
            mark_defaulted(field)

    # Judge readiness on the source row, not the filtered payload: Sendable and
    # Packet Eligible are no longer Airtable columns, so they survive only in the
    # CSV. This also keeps scored CSVs written before Packet Tier existed working.
    source = {**row, **fields}
    row_is_sendable = is_sendable_record(source)
    # A hard disqualifier is not a matter of degree, and `dnc_suppressed` is one
    # of them. Both tier derivations below fall back to Manual Review whenever
    # `Packet Eligible` is Yes, which for a suppressed business means drafting a
    # packet — with an opener — for someone who asked not to be contacted. The
    # canonical gate already routes these to No Packet via
    # `minimum_evidence_passed`; this is the CSV path saying the same thing.
    row_is_disqualified = has_hard_disqualifier(source)
    if clean_cell(fields.get("Packet Tier")) == _TIER_SENDABLE and not row_is_sendable:
        # An explicit tier is still only a snapshot. Total mirroring means a
        # stale Sendable Packet would otherwise survive after the canonical
        # admission/contact gate withdrew the lead and re-enter the rep queue.
        # Tighten to the canonical non-send tier, and do not mark this as a
        # default: an existing Airtable row must be PATCHed away from the stale
        # sendable tier.
        fields["Packet Tier"] = (
            _TIER_MANUAL
            if truthy_yes(source.get("Packet Eligible")) and not row_is_disqualified
            else _TIER_NONE
        )

    # Packet Tier is the only readiness column Airtable keeps, so a CSV without
    # one would upload a record the send-queue filter can never match. Derive it
    # from the booleans — the mirror of sendability.readiness_flags — and mark it
    # defaulted so a PATCH does not clobber a tier already in the base.
    #
    # A row that is neither sendable nor packet-eligible falls through to
    # _TIER_NONE rather than to a blank. Blank has to keep meaning "the pipeline
    # never computed a tier for this record", which is the state that silently
    # emptied the send queue; it must not also mean "computed, and the answer was
    # no packet".
    if not clean_cell(fields.get("Packet Tier")):
        if row_is_sendable:
            derived_tier = _TIER_SENDABLE
        elif truthy_yes(source.get("Packet Eligible")) and not row_is_disqualified:
            derived_tier = _TIER_MANUAL
        else:
            derived_tier = _TIER_NONE
        fields["Packet Tier"] = derived_tier
        defaulted_fields.append("Packet Tier")
        tier_was_defaulted = True
    else:
        tier_was_defaulted = False

    # Derived from the final tier rather than copied from the CSV column, and
    # placed after every rule that can still move the tier — the stale-sendable
    # tighten above included. The two cannot disagree in a payload if one is
    # computed from the other, which is the whole point of the collapse: the
    # nine surfaces drifted because each was written independently.
    fields[VERDICT_FIELD] = verdict_for_tier(clean_cell(fields["Packet Tier"]))
    fields[VERDICT_REASONS_FIELD] = clean_cell(
        row.get(VERDICT_REASONS_FIELD) or row.get("Send Blockers")
    )
    if tier_was_defaulted:
        # Computed from a guessed tier, so it is a guess too. Holding the tier
        # back from the PATCH while letting its verdict through would hand the
        # base a disagreeing pair — and the Call Queue reads the verdict alone,
        # so the guess is what decides whether the lead stays dialable.
        defaulted_fields.extend([VERDICT_FIELD, VERDICT_REASONS_FIELD])

    if row_is_sendable:
        if not clean_cell(fields.get("Status")):
            fields["Status"] = "New"
            mark_defaulted("Status")
    elif clean_cell(fields.get("Packet Tier")) == _TIER_MANUAL:
        fields["Status"] = "Manual Review"
        mark_defaulted("Status")

    # Filter platform/system emails before Airtable payload creation.
    if "Email" in fields:
        cleaned_email = clean_airtable_email(fields["Email"])
        if cleaned_email:
            fields["Email"] = cleaned_email
        else:
            del fields["Email"]

    if clean_cell(fields.get("Outreach Channel")) in LEGACY_OUTREACH_CHANNEL_PLACEHOLDERS:
        fields.pop("Outreach Channel", None)

    if not clean_cell(fields.get("Outreach Channel")):
        channel = "Phone" if clean_cell(fields.get("Phone")) else "Email" if clean_cell(fields.get("Email")) else ""
        if channel:
            fields["Outreach Channel"] = channel
            mark_defaulted("Outreach Channel")

    packet_path = current_packet_path(row, packet_dir)
    if packet_path:
        fields["Packet Path"] = packet_path

    place_key = clean_cell(row.get("Place Key"))
    phone_key = normalize_phone(row.get("Phone", ""))
    if place_key:
        upsert_key = f"place:{place_key}"
    elif phone_key:
        upsert_key = f"phone:{phone_key}"
    else:
        name = clean_cell(row.get("Business Name")) or "<unnamed>"
        raise ValueError(f"CSV row cannot be upserted because Place Key and Phone are blank: {name}")

    # After the upsert key is settled, so a row carrying neither identifier
    # fails with the message that names the business rather than with a less
    # specific assignment error, and so assignment hashes the same identity the
    # upsert does.
    roster = configured_setter_roster()
    if roster:
        fields["Assigned Setter"] = assign_setter(
            assignment_identity(place_key, row.get("Phone", "")), roster
        )
        mark_defaulted("Assigned Setter")

    return {
        "place_id_or_unique_key": place_key,
        "upsert_key": upsert_key,
        "business_name": business_name,
        "phone": clean_cell(row.get("Phone")),
        "airtable_fields": fields,
        "defaulted_airtable_fields": defaulted_fields,
    }


def validate_write_path_select_values(records: Iterable[Dict[str, Any]]) -> None:
    """Refuse any select value the live schema gate cannot authorize."""
    choices = configured_write_path_select_choices()
    for record in records:
        fields = build_airtable_payload(record)["fields"]
        for field, allowed in choices.items():
            value = fields.get(field)
            if value in ("", None):
                continue
            if value not in allowed:
                key = str(record.get("upsert_key") or record.get("place_id_or_unique_key") or "")
                raise ValueError(f"unsupported {field} value {value!r} for {key or '<unknown>'}")


def raise_csv_field_limit() -> None:
    """Allow csv to read very large fields (e.g. the Raw Evidence JSON blob).

    The default field-size limit is 128 KB; real scored/tiered CSVs routinely
    exceed it, which would otherwise raise
    `_csv.Error: field larger than field limit`.
    """
    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:
        csv.field_size_limit(2_147_483_647)


def read_csv_records(
    path: Path, *, packet_dir: Path
) -> List[Dict[str, Any]]:
    raise_csv_field_limit()
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        missing = [field for field in ["Business Name"] if field not in fieldnames]
        if missing:
            raise ValueError(f"Input CSV missing required columns: {', '.join(missing)}")

        records: List[Dict[str, Any]] = []
        for row in reader:
            records.append(csv_row_to_record(row, packet_dir))
        return records


def read_input_records(
    path: Path, *, packet_dir: Path
) -> List[Dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        records = read_csv_records(path, packet_dir=packet_dir)
    else:
        records = read_cards(path)
    return enforce_setter_assignment_ownership(
        enforce_operational_field_ownership(records)
    )


def enforce_operational_field_ownership(
    records: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Mark human-owned operational columns defaulted on every front door.

    `csv_row_to_record` already does this, which is what `063ed4e` fixed after
    a dialled lead was PATCHed back to `New` and contacted twice. Packet cards
    are the other front door and never got the same treatment: `lead_card`
    copies the scorer's `Status: "New"` into `airtable_fields` and returns no
    `defaulted_airtable_fields` at all, so a JSONL re-upload of a lead a human
    had moved to `Not Fit` PATCHed the terminal state away and put the row back
    in `QUEUE_FILTER_FORMULA`. Idempotent, so the CSV path is unaffected.
    """
    normalized: List[Dict[str, Any]] = []
    for record in records:
        fields = record.get("airtable_fields")
        if not isinstance(fields, dict):
            normalized.append(record)
            continue
        defaulted = [str(field) for field in (record.get("defaulted_airtable_fields") or [])]
        for field in OPERATIONAL_FIELDS:
            if clean_cell(fields.get(field)) and field not in defaulted:
                defaulted.append(field)
        record["defaulted_airtable_fields"] = defaulted
        normalized.append(record)
    return normalized


def enforce_setter_assignment_ownership(
    records: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Normalize every uploader input to the create-only setter contract.

    CSV conversion already derives this field, but JSONL cards are another
    supported front door. Keeping the final ownership check here means neither
    format can inject an arbitrary assignment or PATCH a rep's manual choice.
    """
    roster = configured_setter_roster()
    normalized: List[Dict[str, Any]] = []
    for record in records:
        fields = record.get("airtable_fields")
        if not isinstance(fields, dict):
            fields = {}
            record["airtable_fields"] = fields
        supplied = clean_cell(fields.get("Assigned Setter"))
        defaulted = [
            str(field)
            for field in (record.get("defaulted_airtable_fields") or [])
            if str(field) != "Assigned Setter"
        ]
        if not roster:
            fields.pop("Assigned Setter", None)
            record["defaulted_airtable_fields"] = defaulted
            normalized.append(record)
            continue

        identity = card_place_key(record) or card_phone_key(record)
        if not identity:
            # Nothing to upsert on either. `plan_airtable_sync` refuses this
            # record by name a moment later; pre-empting it here would replace
            # that message with a vaguer one.
            fields.pop("Assigned Setter", None)
            record["defaulted_airtable_fields"] = defaulted
            normalized.append(record)
            continue

        expected = assign_setter(identity, roster)
        if supplied and supplied != expected:
            raise ValueError(
                f"Assigned Setter {supplied!r} does not match deterministic assignment "
                f"{expected!r} for {identity}"
            )
        fields["Assigned Setter"] = expected
        defaulted.append("Assigned Setter")
        record["defaulted_airtable_fields"] = defaulted
        normalized.append(record)
    return normalized


def airtable_base_url(base_id: str, table: str) -> str:
    return f"https://api.airtable.com/v0/{base_id}/{quote(table, safe='')}"


def request_params_for_fields(fields: Sequence[str], *, page_size: int = 100) -> List[tuple[str, Any]]:
    params: List[tuple[str, Any]] = [("pageSize", page_size)]
    for field in fields:
        params.append(("fields[]", field))
    return params


def validate_airtable_fields(base_id: str, table: str, token: str, fields: Iterable[str]) -> None:
    field_list = sorted({field for field in fields if field})
    if not field_list:
        return

    url = airtable_base_url(base_id, table)
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, params=request_params_for_fields(field_list, page_size=1), timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(
            "Airtable field validation failed. Create the required fields before running live sync: "
            f"{', '.join(field_list)}. Airtable response {resp.status_code}: {resp.text[:1000]}"
        )


def assert_required_choices_ready(
    base_id: str,
    token: str,
    prospects_table: str,
) -> None:
    """Refuse a live upload before any write if declared selects are absent."""
    # Lazy import avoids a module cycle: the checker derives uploader choices
    # from this module, while the uploader reuses its read-only audit here.
    from airtable_required_choices import (
        audit_choices,
        fetch_base_tables,
        missing_choices,
        required_choices,
        resolve_table_names,
    )

    tables = fetch_base_tables(base_id, token)
    table_names = resolve_table_names({"Prospects": prospects_table})
    missing = missing_choices(audit_choices(tables, required_choices(), table_names))
    if missing:
        raise ValueError(
            "Airtable select-choice gate is incomplete; run "
            "`.venv/bin/python airtable_required_choices.py check` and add: "
            + "; ".join(missing)
        )


def airtable_field_exists(base_id: str, table: str, token: str, field: str) -> bool:
    url = airtable_base_url(base_id, table)
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, params=request_params_for_fields([field], page_size=1), timeout=30)
    if resp.status_code < 400:
        return True
    if resp.status_code == 422:
        return False
    raise RuntimeError(f"Airtable field check failed for {field} {resp.status_code}: {resp.text[:1000]}")


def remove_airtable_field(records: Iterable[Dict[str, Any]], field: str) -> None:
    for record in records:
        fields = record.get("airtable_fields") or {}
        fields.pop(field, None)


def payload_field_names(records: Iterable[Dict[str, Any]]) -> List[str]:
    field_names = set()
    for record in records:
        field_names.update(build_airtable_payload(record).get("fields", {}).keys())
    return sorted(field_names)


def _add_existing_key(existing: Dict[str, Any], key: str, record_id: str) -> None:
    if not key or not record_id:
        return
    current = existing.get(key)
    if current is None:
        existing[key] = record_id
        return
    if isinstance(current, list):
        if record_id not in current:
            current.append(record_id)
        return
    if current != record_id:
        existing[key] = [current, record_id]


def fetch_existing_records(base_id: str, table: str, token: str) -> Dict[str, Any]:
    url = airtable_base_url(base_id, table)
    headers = {"Authorization": f"Bearer {token}"}
    params: List[tuple[str, str]] = []
    existing: Dict[str, Any] = {}

    while True:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(f"Airtable list failed {resp.status_code}: {resp.text[:1000]}")
        data = resp.json()
        for record in data.get("records", []):
            fields = record.get("fields") or {}
            record_id = record.get("id", "")
            place_key = clean_cell(fields.get("Place Key"))
            if place_key:
                _add_existing_key(existing, f"place:{place_key}", record_id)
                _add_existing_key(existing, place_key, record_id)
            google_place_id = clean_cell(fields.get("Google Place ID"))
            if google_place_id:
                _add_existing_key(existing, f"place:{google_place_id}", record_id)
                _add_existing_key(existing, google_place_id, record_id)
            phone = normalize_phone(fields.get("Phone", ""))
            if phone:
                _add_existing_key(existing, f"phone:{phone}", record_id)
            domain = normalize_domain(fields.get("Website URL", ""))
            if domain and domain not in GENERIC_DOMAINS:
                _add_existing_key(existing, f"domain:{domain}", record_id)
            business_name = normalize_business_name(fields.get("Business Name"))
            city = normalize_city(fields.get("City"))
            if business_name and city:
                _add_existing_key(existing, f"business_city:{business_name}|{city}", record_id)
        offset = data.get("offset")
        if not offset:
            break
        params = []
        params.append(("offset", offset))

    return existing


def print_plan_diagnostics(planned: Iterable[Dict[str, Any]]) -> None:
    for item in planned:
        dropped_fields = item.get("dropped_fields") or []
        if dropped_fields:
            dropped = ", ".join(
                f"{field.get('field')}={field.get('value')!r}"
                for field in dropped_fields
            )
            print(
                f"[WARN] Dropped unsupported Airtable field values for {item.get('upsert_key')}: {dropped}",
                file=sys.stderr,
            )
        warning = item.get("warning")
        if warning:
            print(f"[WARN] {warning}", file=sys.stderr)
            continue
        reason = item.get("match_reason")
        if reason and item.get("method") == "PATCH":
            print(
                f"[MATCH] {item.get('upsert_key')} -> PATCH {item.get('record_id')} via {reason}",
                file=sys.stderr,
            )


AIRTABLE_TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
AIRTABLE_MAX_RETRIES = 4
AIRTABLE_RETRY_BACKOFF_SECONDS = 1.0
# Airtable's documented limit is 5 requests per second per base.
AIRTABLE_MIN_SECONDS_BETWEEN_WRITES = 0.21
DEFAULT_AIRTABLE_RECEIPTS_PATH = "run_state/airtable_writes.jsonl"


def _airtable_write_with_retry(
    session: requests.Session,
    *,
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    label: str,
) -> requests.Response:
    """Send one Airtable write, retrying only what is worth retrying.

    A 422 for an unknown field will fail identically every time; retrying it
    just delays the error. 429 and 5xx are the ones that clear on their own, and
    Airtable rate-limits a base at 5 requests per second, so a burst of writes
    hits 429 as a matter of course rather than as an exception.
    """
    last_error = ""
    for attempt in range(1, AIRTABLE_MAX_RETRIES + 1):
        try:
            response = session.request(method, url, headers=headers, json=payload, timeout=30)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt >= AIRTABLE_MAX_RETRIES:
                raise RuntimeError(
                    f"Airtable {method} failed for {label} after {attempt} attempts: {last_error}"
                ) from exc
        else:
            if response.status_code < 400:
                return response
            last_error = f"{response.status_code}: {response.text[:1000]}"
            if response.status_code not in AIRTABLE_TRANSIENT_HTTP_STATUSES:
                raise RuntimeError(f"Airtable {method} failed for {label} {last_error}")
            if attempt >= AIRTABLE_MAX_RETRIES:
                raise RuntimeError(
                    f"Airtable {method} failed for {label} after {attempt} attempts, {last_error}"
                )
        sleeping = AIRTABLE_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
        print(
            f"[AIRTABLE RETRY] attempt={attempt} {method} {label} "
            f"reason={last_error[:120]} sleeping={sleeping:.1f}s",
            file=sys.stderr,
        )
        time.sleep(sleeping)
    raise RuntimeError(f"Airtable {method} failed for {label}: {last_error}")


def send_airtable_plan(
    base_id: str,
    table: str,
    token: str,
    planned: List[Dict[str, Any]],
    *,
    receipts_path: Path | str | None = None,
    applied_keys: AbstractSet[str] | None = None,
) -> int:
    """Apply *planned* writes, recording each one that lands.

    This used to write sequentially and raise on the first status >= 400, which
    left the base half-synced with no record of which writes had applied — the
    operator could neither trust the base nor safely re-run. Every applied write
    is now appended to a receipts file before the next one is attempted, so a
    re-run can skip what already landed.
    """
    base_url = airtable_base_url(base_id, table)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    already_applied = set(applied_keys or ())
    receipts = _open_receipts(receipts_path)
    applied = 0
    last_write_at = 0.0

    try:
        with requests.Session() as session:
            for item in planned:
                if item["method"] == "SKIP":
                    print(f"[SKIP] {item.get('upsert_key')}: {item.get('warning', 'manual review required')}")
                    continue

                label = str(item.get("upsert_key") or item.get("place_key") or "")
                if label and label in already_applied:
                    print(f"[RESUME] {item['method']} {label} already applied; skipping")
                    continue

                # Airtable caps a base at 5 writes/second. Pacing here turns a
                # predictable 429 into no request at all.
                wait = AIRTABLE_MIN_SECONDS_BETWEEN_WRITES - (time.monotonic() - last_write_at)
                if wait > 0:
                    time.sleep(wait)

                if item["method"] == "PATCH":
                    url = f"{base_url}/{item['record_id']}"
                    method = "PATCH"
                else:
                    url = base_url
                    method = "POST"

                response = _airtable_write_with_retry(
                    session,
                    method=method,
                    url=url,
                    headers=headers,
                    payload=item["payload"],
                    label=label or "unknown",
                )
                last_write_at = time.monotonic()
                applied += 1
                _append_receipt(
                    receipts,
                    {
                        "method": method,
                        "upsert_key": label,
                        "record_id": item.get("record_id") or _response_record_id(response),
                        "base_id": base_id,
                        "table": table,
                    },
                )
                print(f"[SYNC] {method} {label}")
    finally:
        if receipts is not None:
            receipts.close()

    return applied


def _response_record_id(response: requests.Response) -> str:
    try:
        return str((response.json() or {}).get("id") or "")
    except Exception:
        return ""


def _open_receipts(receipts_path: Path | str | None):
    if receipts_path is None:
        return None
    path = Path(receipts_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a", encoding="utf-8")


def _append_receipt(handle, receipt: Dict[str, Any]) -> None:
    if handle is None:
        return
    handle.write(json.dumps({**receipt, "written_at": _utc_now_iso()}, ensure_ascii=False) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_applied_write_keys(receipts_path: Path | str, *, base_id: str, table: str) -> set[str]:
    """Keys already written to *base_id*/*table*, per the receipts file.

    Scoped to the base and table so a receipts file from a different target
    cannot make a run silently skip writes it never performed.
    """
    path = Path(receipts_path)
    if not path.is_file():
        return set()
    applied: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            receipt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(receipt, dict):
            continue
        if receipt.get("base_id") != base_id or receipt.get("table") != table:
            continue
        key = str(receipt.get("upsert_key") or "").strip()
        if key:
            applied.add(key)
    return applied


def main() -> int:
    load_dotenv()
    raise_csv_field_limit()

    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="", help="Scored CSV or lead cards JSONL")
    parser.add_argument("--dry-run", action="store_true", help="Print planned Airtable writes without sending")
    parser.add_argument("--live", action="store_true", help="Send planned writes to Airtable")
    parser.add_argument(
        "--receipts",
        default=os.getenv("AIRTABLE_RECEIPTS_PATH", DEFAULT_AIRTABLE_RECEIPTS_PATH),
        help="JSONL path recording each applied Airtable write, so a failed run can be resumed",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip writes already recorded in the receipts file for this base and table",
    )
    parser.add_argument(
        "--packet-dir",
        default="",
        help="Packet directory for optional Packet Path mapping; defaults to packets_<BATCH>_manual for scored_<BATCH>_manual.csv",
    )
    parser.add_argument("--base-id", default=os.getenv("AIRTABLE_BASE_ID", ""))
    parser.add_argument("--table", default=os.getenv("AIRTABLE_PROSPECTS_TABLE", ""))
    parser.add_argument("--token", default=os.getenv("AIRTABLE_PAT", ""))
    parser.add_argument(
        "--schema-report", "--print-schema",
        action="store_true",
        dest="schema_report",
        help="Print required Airtable fields and their recommended types; no sync is performed",
    )
    parser.add_argument(
        "--schema-report-output",
        default="",
        metavar="PATH",
        help="Write schema report to PATH (.md or .csv); implies --schema-report",
    )
    args = parser.parse_args()

    if args.dry_run and args.live:
        parser.error("--dry-run cannot be combined with --live")

    schema_mode = args.schema_report or bool(args.schema_report_output)

    if schema_mode:
        report = build_schema_report()
        print_schema_report(report)
        if args.schema_report_output:
            out_path = Path(args.schema_report_output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            write_schema_report(report, out_path)
            print(f"[SCHEMA] Written to {out_path}")
        return 0

    if not args.input:
        parser.error("--input is required (or use --schema-report to print the field schema)")

    input_path = Path(args.input)
    packet_dir = Path(args.packet_dir) if args.packet_dir else infer_packet_dir(input_path)

    try:
        if not input_path.exists():
            raise FileNotFoundError(input_path)

        records = read_input_records(
            input_path,
            packet_dir=packet_dir,
        )
        validate_write_path_select_values(records)

        if not args.live:
            existing = {}
            if args.dry_run and args.base_id and args.table and args.token:
                existing = fetch_existing_records(args.base_id, args.table, args.token)
            planned = plan_airtable_sync(records, existing_records_by_key=existing)
            print_plan_diagnostics(planned)
            print(f"[DRY-RUN] Records: {len(records)}")
            print(json.dumps(planned, ensure_ascii=False, indent=2))
            return 0

        if not args.base_id or not args.table or not args.token:
            print(
                "[ERROR] AIRTABLE_BASE_ID, AIRTABLE_PROSPECTS_TABLE, and AIRTABLE_PAT are required",
                file=sys.stderr,
            )
            return 1

        assert_required_choices_ready(args.base_id, args.token, args.table)

        for field in OPTIONAL_FIELDS:
            has_value = any((record.get("airtable_fields") or {}).get(field) for record in records)
            if has_value and not airtable_field_exists(args.base_id, args.table, args.token, field):
                remove_airtable_field(records, field)

        if input_path.suffix.lower() == ".csv":
            # Only the columns a payload can actually carry. `CSV_AIRTABLE_FIELDS`
            # is the CSV-side contract and still names the seven the verdict
            # cutover deletes — `Packet Tier` among them, because the verdict is
            # derived from it in-process — so validating the raw list would
            # demand columns the base no longer has and refuse every live run.
            validate_airtable_fields(
                args.base_id,
                args.table,
                args.token,
                [
                    field
                    for field in CSV_AIRTABLE_FIELDS + OPERATIONAL_FIELDS + GENERATED_OPERATIONAL_FIELDS
                    if field in set(AIRTABLE_ALLOWED_FIELDS)
                ],
            )
        else:
            validate_airtable_fields(args.base_id, args.table, args.token, payload_field_names(records))

        existing = fetch_existing_records(args.base_id, args.table, args.token)
        planned = plan_airtable_sync(records, existing_records_by_key=existing)
        print_plan_diagnostics(planned)
        receipts_path = Path(args.receipts)
        applied_keys: set[str] = set()
        if args.resume:
            applied_keys = load_applied_write_keys(
                receipts_path, base_id=args.base_id, table=args.table
            )
            if applied_keys:
                print(f"[RESUME] {len(applied_keys)} writes already applied per {receipts_path}")
        applied = send_airtable_plan(
            args.base_id,
            args.table,
            args.token,
            planned,
            receipts_path=receipts_path,
            applied_keys=applied_keys,
        )
        print(f"[DONE] Synced records: {applied} of {len(planned)} planned")
        print(f"[DONE] Write receipts: {receipts_path}")
        return 0
    except (
        FileNotFoundError,
        RuntimeError,
        ValueError,
        # The schema gate calls raise_for_status, and HTTPError is not a
        # subclass of any of the above: a 403 from a PAT without
        # schema.bases:read, or a 429 from the meta endpoint, would traceback
        # instead of printing [ERROR] and exiting 1.
        requests.RequestException,
    ) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
