from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import AbstractSet, Any, Iterable, Mapping


ACCEPTABLE_SMS_CONSENT_POSTURES = frozenset(
    {
        "documented opt in",
        "express written consent",
        "prior express written consent",
    }
)
SUPPRESSION_EVENTS = frozenset(
    {"opt_out", "opted_out", "unsubscribe", "unsubscribed", "suppressed", "stop"}
)
MARK_SENT_ALLOWED_SOURCE_STATUSES = frozenset({"new"})


class ComplianceError(ValueError):
    pass


def normalize_consent_posture(value: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())


def has_acceptable_sms_consent(value: Any) -> bool:
    return normalize_consent_posture(value) in ACCEPTABLE_SMS_CONSENT_POSTURES


def normalize_airtable_status(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def is_mark_sent_source_status(value: Any) -> bool:
    return normalize_airtable_status(value) in MARK_SENT_ALLOWED_SOURCE_STATUSES


def suppression_identifier(kind: str, value: Any) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return ""
    if kind == "phone":
        cleaned = "".join(character for character in cleaned if character.isdigit())
        if len(cleaned) == 11 and cleaned.startswith("1"):
            cleaned = cleaned[1:]
    else:
        cleaned = cleaned.casefold()
    return f"{kind}:{cleaned}" if cleaned else ""


def enriched_contact_identifiers(enriched: Mapping[str, Any] | None) -> tuple[list[str], list[str]]:
    """Return (emails, phones) visible on an enriched record."""
    if not isinstance(enriched, Mapping):
        return [], []
    google = enriched.get("google") if isinstance(enriched.get("google"), Mapping) else {}
    website = enriched.get("website") if isinstance(enriched.get("website"), Mapping) else {}

    emails = [value for value in (website.get("emails_found") or []) if value]
    for key in ("email", "Email"):
        if google.get(key):
            emails.append(google[key])

    phones = [value for value in (website.get("phones_found") or []) if value]
    for key in ("phone", "Phone"):
        if google.get(key):
            phones.append(google[key])

    return emails, phones


def record_suppression_identifiers(record: Mapping[str, Any]) -> set[str]:
    """Every suppression key a record can be matched on, whatever its shape.

    One identifier set for the three shapes suppression has to survive — Airtable
    envelopes ({"id", "fields"}), flat queue/pooled rows, and enriched pipeline
    records with nested google/website blocks — so send time and score time
    recognise a lead by exactly the same keys. Scoring used to build its own
    emails-and-phones set, which left the two stages on different definitions of
    "the same lead".

    Read the record-id keys for what they are: they only populate for a record
    that already exists in Airtable — a mark_sent envelope, a hermes queue row, a
    re-scored export. A newly discovered lead has never been uploaded, so it has
    no Airtable id yet and cannot be matched by one even in principle; the
    ledger's lead_id always refers to a record already in the base. On the
    discovery path it is the nested google/website contacts that carry the whole
    gate — pinned by
    tests/test_dnc_enrichment.py::SuppressionIdentityTests, which asserts that
    shape has no record id to match on.
    """
    if not isinstance(record, Mapping):
        # Fail closed. An empty identifier set reads downstream as "nothing
        # matched", which is indistinguishable from "checked and clear": the
        # hermes morning queue keeps `[r for r in records if not
        # _is_suppressed(r, suppression)]`, so a malformed element would be
        # queued for send rather than stopping the run. A record we cannot
        # identify is a record we cannot clear.
        raise ComplianceError(
            f"suppression cannot identify a {type(record).__name__} record"
        )
    envelope = record.get("fields") if isinstance(record.get("fields"), Mapping) else None
    fields = record if envelope is None else envelope
    # An enriched record's google block is the row enrichment was handed, so an
    # Airtable export re-run through enrich_leads carries its record id there.
    google = record.get("google") if isinstance(record.get("google"), Mapping) else {}

    record_ids = [
        record.get("record_id"),
        record.get("lead_id"),
        fields.get("record_id"),
        fields.get("lead_id"),
        google.get("record_id"),
        google.get("lead_id"),
    ]
    if envelope is not None:
        # A bare `id` counts as a record id only inside the envelope Airtable
        # actually returns. Google Places keys the *Place ID* as `id` ("ChIJ…")
        # and Places payloads are what this pipeline is built out of, so
        # honouring `id` on any flat dict would put a Place ID into the
        # record_id key space — a false positive against a ledger row that
        # happens to hold that string, and a false negative for the record it
        # shadows. Nothing live needs it: query_ready_to_send names the key
        # `record_id`, and mark_sent passes the Airtable envelope.
        record_ids.append(record.get("id"))

    emails, phones = enriched_contact_identifiers(record)
    emails += [fields.get("email"), fields.get("Email")]
    phones += [fields.get("phone"), fields.get("Phone")]

    return {
        identifier
        for kind, values in (("record_id", record_ids), ("email", emails), ("phone", phones))
        for value in values
        if (identifier := suppression_identifier(kind, value))
    }


def suppression_source_path(value: Path | str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ComplianceError("suppression source is not configured")
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise ComplianceError(f"suppression source {path} is missing or unreadable")
    return path


def load_suppression_index(value: Path | str) -> set[str]:
    path = suppression_source_path(value)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ComplianceError(f"suppression source {path} is unreadable: {exc}") from exc

    suppressed: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ComplianceError(
                f"suppression source {path}:{line_number} is malformed JSON: {exc.msg}"
            ) from exc
        if not isinstance(row, dict):
            raise ComplianceError(f"suppression source {path}:{line_number} must contain a JSON object")
        event = str(row.get("event") or "").strip().casefold().replace("-", "_").replace(" ", "_")
        if event not in SUPPRESSION_EVENTS:
            continue
        # `lead_id` is the key operator-agent's STATE/outcomes.jsonl writes for
        # the Airtable record id (STATE/README.md); this file is read as LPM's
        # suppression source, so both spellings have to resolve or every
        # ledger-written opt_out either fails the run below or silently
        # suppresses nobody.
        identifiers = {
            identifier
            for key, kind in (
                ("record_id", "record_id"),
                ("lead_id", "record_id"),
                ("phone", "phone"),
                ("email", "email"),
            )
            if (identifier := suppression_identifier(kind, row.get(key)))
        }
        if not identifiers:
            raise ComplianceError(
                f"suppression source {path}:{line_number} has a suppression event without an identifier"
            )
        suppressed.update(identifiers)
    return suppressed


# --- Writing the suppression ledger --------------------------------------
# This module was read-only until 2026-08-07: it could load an index and match
# against it, but nothing in the repo could *create* a suppression row. The only
# ledger appender was hermes_queue's, whose sole call site writes
# `{"event": "sent"}`. Every `opt_out` literal lived in a test fixture, so a rep
# who heard "take me off your list" had nowhere to put it — the one gap in the
# pipeline that created liability rather than delay.
#
# The writer lives here, beside the reader, because the two have to agree on the
# row shape exactly: a row `load_suppression_index` skips is a suppression that
# silently applies to nobody.

DEFAULT_SUPPRESSION_EVENT = "opt_out"

# Free-text that means a business withdrew consent, for screening any note a rep
# types about a lead they are retiring for a *non*-compliance reason.
#
# Needed because `mark_declined.py` writes `Status="Not Fit"` — the same
# terminal value `mark_outcome.py` writes for an opt-out — and the only status
# it accepts is `New`, which is exactly when "take me off your list" is heard:
# on the first dial. Nothing in the repo reads `Status` as consent state, so a
# misfiled refusal breaks no invariant; it just leaves no suppression row, and
# the business is re-discovered under a new record id and called again. That is
# the case the ledger exists to prevent, arriving through the one door that
# does not write to it.
#
# Substrings, matched case-folded, and deliberately over-broad. A false positive
# costs a rep one retyped reason and a pointer to the right command; a false
# negative costs a call to someone who said no. Tune only in that direction.
OPT_OUT_LANGUAGE = (
    "take me off",
    "take us off",
    "off your list",
    "off the list",
    "stop calling",
    "stop contacting",
    "don't call",
    "dont call",
    "do not call",
    "don't contact",
    "dont contact",
    "do not contact",
    # Bare "be contacted" rather than "not to be contacted": the negation moves
    # around ("does not want to be contacted", "prefers not to be contacted",
    # "asked not to be contacted") and a list that chases each phrasing will
    # miss one. This also trips "asked to be contacted in the fall", which is a
    # legitimate refusal — that rep retypes one reason, which is the trade this
    # list is explicitly tuned to make.
    "be contacted",
    "no contact",
    "unsubscribe",
    "opt out",
    "opted out",
    "opt-out",
    "remove me",
    "remove us",
    "lose my number",
    "harass",
    "dnc",
)


def opt_out_language_in(text: Any) -> str:
    """The first opt-out phrase found in *text*, or "" if there is none.

    Returns the phrase rather than a bool so the caller can quote it back: a
    refusal that says which words tripped it is one the rep can act on.
    """
    haystack = str(text or "").casefold()
    for phrase in OPT_OUT_LANGUAGE:
        if phrase in haystack:
            return phrase
    return ""


def normalize_suppression_event(value: Any) -> str:
    """Fold an event name the way `load_suppression_index` folds it on read."""
    return str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")


def configured_suppression_source() -> str:
    """The ledger path the environment names, unresolved and unvalidated.

    Shared by the reader below and by every write path, so a run cannot end up
    reading one file and appending to another.
    """
    return (
        os.getenv("SUPPRESSION_SOURCE", "").strip()
        or os.getenv("LPM_OUTCOMES_JSONL", "").strip()
    )


def append_suppression_event(
    ledger: Path | str,
    *,
    event: str = DEFAULT_SUPPRESSION_EVENT,
    record_id: Any = "",
    phone: Any = "",
    email: Any = "",
    occurred_at: str = "",
    source: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Append one suppression row: append-only, fsynced, idempotent on repeat.

    Fails closed in four directions, each of which would otherwise produce a
    suppression that suppresses nobody:

    * an `event` outside `SUPPRESSION_EVENTS` — the reader skips those rows;
    * a row with no identifier — the reader *raises* on those, so writing one
      would brick every later run that loads this file;
    * a ledger path that does not exist — creating it would record the opt-out
      into a file nothing reads, while the rep believes they are covered;
    * a ledger that does not parse — a good row added to a broken file is still
      a file every reader rejects wholesale.

    Idempotency is defined on identifiers, not on the row: repeating the same
    opt-out appends nothing, but adding a channel the ledger has never seen
    (a second phone, a late-discovered email) still appends. A rule keyed on
    exact-row equality would silently drop the new channel.

    Contact values are written as given, not normalised: `suppression_identifier`
    normalises on read, and the raw string is what a human can recognise in the
    ledger later.

    Returns `{"result": "appended"|"already_present", "path", "identifiers"}`.
    """
    normalized_event = normalize_suppression_event(event)
    if normalized_event not in SUPPRESSION_EVENTS:
        raise ComplianceError(
            f"{event!r} is not a suppression event, so load_suppression_index would "
            f"skip the row it produces. Expected one of {sorted(SUPPRESSION_EVENTS)}"
        )

    path = suppression_source_path(ledger)

    identifiers = {
        identifier
        for kind, value in (("record_id", record_id), ("phone", phone), ("email", email))
        if (identifier := suppression_identifier(kind, value))
    }
    if not identifiers:
        raise ComplianceError(
            "a suppression event needs at least one identifier (record_id, phone or email); "
            "load_suppression_index rejects a row without one"
        )

    row: dict[str, Any] = {"event": normalized_event}
    for key, value in (
        ("record_id", record_id),
        ("phone", phone),
        ("email", email),
        ("occurred_at", occurred_at),
        ("source", source),
        ("notes", notes),
    ):
        cleaned = str(value or "").strip()
        if cleaned:
            row[key] = cleaned

    existing = load_suppression_index(path)
    if identifiers <= existing:
        return {
            "result": "already_present",
            "path": str(path),
            "identifiers": sorted(identifiers),
        }

    line = json.dumps(row, sort_keys=True)
    try:
        # A ledger whose last line has no terminator would otherwise be
        # concatenated with this row into one unparseable line, taking down
        # every reader of the file.
        existing_text = path.read_text(encoding="utf-8")
        prefix = "\n" if existing_text and not existing_text.endswith("\n") else ""
        with path.open("a", encoding="utf-8") as handle:
            handle.write(prefix + line + "\n")
            handle.flush()
            # An opt-out that only reached the page cache is not recorded.
            os.fsync(handle.fileno())
    except OSError as exc:
        raise ComplianceError(f"suppression ledger {path} is not writable: {exc}") from exc

    return {"result": "appended", "path": str(path), "identifiers": sorted(identifiers)}


def matching_suppression_identifiers(
    record: Mapping[str, Any], suppression: Iterable[str]
) -> set[str]:
    return record_suppression_identifiers(record) & set(suppression)


def is_suppressed(record: Mapping[str, Any], suppression: Iterable[str]) -> bool:
    return bool(matching_suppression_identifiers(record, suppression))


# --- Enrichment-time suppression -----------------------------------------
# Suppression has to bite before a lead is scored, not only at send time.
# mark_sent is the last line of defence; these are what keep a suppressed
# business out of the scored CSV, the packets, and the Airtable base in the
# first place.

DNC_SUPPRESSED_REASON = "dnc_suppressed"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def load_configured_suppression_index() -> set[str]:
    """Load the suppression index named by the environment, failing closed.

    A missing suppression source is an unsafe state, not an empty one: with no
    index every opt-out silently stops applying. Callers must opt out loudly via
    ALLOW_MISSING_SUPPRESSION_INDEX rather than getting an empty set by default.
    """
    suppression_source = configured_suppression_source()

    if not suppression_source:
        if _env_flag("ALLOW_MISSING_SUPPRESSION_INDEX", False):
            return set()
        raise ComplianceError(
            "No suppression source configured "
            "(SUPPRESSION_SOURCE or LPM_OUTCOMES_JSONL). "
            "Set ALLOW_MISSING_SUPPRESSION_INDEX=1 to bypass."
        )

    try:
        return load_suppression_index(suppression_source)
    except ComplianceError:
        raise
    except Exception as exc:
        raise ComplianceError(
            f"Suppression source was configured but could not be loaded: {suppression_source}"
        ) from exc


def has_suppression_match(
    *,
    emails: Iterable[Any] = (),
    phones: Iterable[Any] = (),
    suppression_index: AbstractSet[str],
) -> bool:
    for kind, values in (("email", emails), ("phone", phones)):
        for value in values:
            if not value:
                continue
            identifier = suppression_identifier(kind, value)
            if identifier and identifier in suppression_index:
                return True
    return False


def suppression_disqualifiers(enriched: Mapping[str, Any] | None) -> list[str]:
    """Read the enrichment-time suppression verdict off an enriched record.

    The bridge between the enricher, which is the only stage that sees scraped
    emails and phones, and the scorers, which decide what ships. Tolerates a
    malformed `derived` block rather than raising: a scorer that crashes on a
    weird record fails open for the whole batch.
    """
    derived = enriched.get("derived") if isinstance(enriched, Mapping) else None
    if not isinstance(derived, Mapping):
        return []
    reasons = derived.get("hard_icp_reasons")
    if not isinstance(reasons, (list, tuple, set)):
        return []
    return [DNC_SUPPRESSED_REASON] if DNC_SUPPRESSED_REASON in reasons else []


def is_suppressed_record(enriched: Mapping[str, Any] | None) -> bool:
    return bool(suppression_disqualifiers(enriched))


def record_matches_suppression(
    enriched: Mapping[str, Any], suppression_index: AbstractSet[str]
) -> bool:
    """Match an enriched record against a live suppression index.

    Distinct from `is_suppressed_record`, which only reads the verdict the
    enricher already stored. That verdict is a snapshot: opt-outs arrive *after*
    enrichment almost by definition, so a record enriched before a business
    opted out carries a clean flag forever. Every stage that can still stop a
    lead has to re-check against the current index rather than trust it.

    Runs through the shared identifier set rather than contacts alone so scoring
    and mark_sent recognise a record by exactly the same keys: an opt-out logged
    against a record_id or lead_id used to bite only at send time, which left the
    business to be enriched, scored, packeted and pushed to Airtable first.

    Inherits that set's fail-closed handling of a malformed record, unlike
    `suppression_disqualifiers`, which only reads a stored verdict and can safely
    shrug. Here a shrug would be a clearance.
    """
    if not suppression_index:
        return False
    return bool(matching_suppression_identifiers(enriched, suppression_index))


def merge_suppression_disqualifiers(row: dict[str, Any], enriched: Mapping[str, Any] | None) -> dict[str, Any]:
    """Carry the enricher's suppression verdict onto a scored row, in place.

    Both scoring paths call this before `apply_sendability`, which is what makes
    the verdict binding: `sendability.has_hard_disqualifier` reads these keys, so
    a suppressed lead picks up HARD_DISQUALIFIER_BLOCKER, which is in
    HARD_ELIGIBILITY_BLOCKERS, which forces minimum_evidence_passed False and
    therefore Packet Tier = No Packet. No separate gate to keep in sync.

    HARD_ELIGIBILITY_BLOCKERS, not EVIDENCE_BLOCKERS — membership in the latter
    stopped implying anything when the gate was thinned and the two sets became
    equal. The distinction is load-bearing here: the hard set is what routes a
    lead to No Packet, while the claim-integrity set routes to Manual Review,
    which would draft a packet and an opener for a business that opted out.
    `tests/test_sendability_admission.py` pins both the membership and the tier.

    `Skip Reason` is set as well as the canonical list because it is already a
    synced Airtable column, so the operator sees why the lead was dropped
    instead of finding an unexplained No Packet.
    """
    reasons = suppression_disqualifiers(enriched)
    if not reasons:
        return row

    existing = row.get("hard_disqualifiers")
    merged = list(existing) if isinstance(existing, (list, tuple)) else []
    for reason in reasons:
        if reason not in merged:
            merged.append(reason)
    row["hard_disqualifiers"] = merged
    row["Hard Disqualifiers"] = merged

    skip_reason = str(row.get("Skip Reason") or "").strip()
    parts = [part.strip() for part in skip_reason.split(";") if part.strip()]
    for reason in reasons:
        if reason not in parts:
            parts.append(reason)
    row["Skip Reason"] = "; ".join(parts)
    return row
