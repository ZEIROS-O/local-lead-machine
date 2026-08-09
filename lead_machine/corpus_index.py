"""The cross-batch place registry — the corpus-level dedupe ledger.

Every dedupe mechanism in the search → enrich → score path used to be scoped to
a single batch: Text Search pagination dedupes within one API call,
``seen_place_ids`` within one batch, ``dedupe_search_rows`` within one batch.
Nothing compared a batch against the corpus, so re-running an adjacent metro
re-discovered, re-billed, re-crawled and re-scored businesses already sitting in
``output/``. Measured on 2026-08-09 over 17 configs: 1,621 enriched rows for 626
unique businesses (61% redundant), with one place_id found in 14 separate
batches. The only mechanism that ever collapsed them was the Airtable upsert,
which fires *after* all Places, crawl and LLM spend.

This module is the missing cross-batch memory. It is deliberately not a second
source of truth: :func:`rebuild_from_corpus` reconstructs it from the artifacts
on disk, so a lost, stale or hand-mangled ledger is a rebuild rather than an
investigation.

Key construction is the same four-class precedence the in-batch dedupe already
uses — ``place_id → phone → domain → name_city``. ``make_search_batch.make_place_key``
delegates to :func:`primary_key` here so there is exactly one implementation:
two key builders that drift are two different opinions about what "the same
business" means, and the more conservative one silently stops saving money.
"""
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from lead_machine.schema import GENERIC_DOMAINS, normalize_domain, normalize_phone, pick, slug


# Sits beside run_state/review_velocity.sqlite3. run_state/ is gitignored in
# full, so the ledger is machine-local by construction and never carries lead
# contents into the repo.
REGISTRY_RELATIVE_PATH = Path("run_state") / "place_registry.jsonl"

# The dedupe classes, strongest identity first. Same order and same meaning as
# make_search_batch.dedupe_search_rows.
KEY_CLASSES: Tuple[str, ...] = ("place_id", "phone", "domain", "name_city")

# Namespace prefix per class, so one flat set can hold all four without a
# phone number ever colliding with a name_city slug.
_NAMESPACE = {"place_id": "place", "phone": "phone", "domain": "domain", "name_city": "name_city"}


def registry_path_for(root: Path | str = Path(".")) -> Path:
    return Path(root) / REGISTRY_RELATIVE_PATH


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: Optional[datetime] = None) -> str:
    moment = moment or utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_stamp(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def row_keys(row: Mapping[str, Any]) -> Dict[str, str]:
    """The four dedupe-class keys a row supports, omitting the ones it cannot.

    Field aliases match the search rows the pipeline writes, which carry both
    the snake_case and the Airtable-cased spelling of every column.
    """
    source = dict(row)
    keys: Dict[str, str] = {}

    place_id = pick(source, "place_id", "Place ID")
    if place_id:
        keys["place_id"] = place_id

    phone = normalize_phone(pick(source, "phone", "Phone"))
    if phone:
        keys["phone"] = phone

    domain = normalize_domain(pick(source, "website_url", "Website URL", "website"))
    if domain and domain not in GENERIC_DOMAINS:
        keys["domain"] = domain

    name = slug(pick(source, "business_name", "Business Name", "name"))
    city = slug(pick(source, "city", "City"))
    if name and city:
        keys["name_city"] = f"{name}:{city}"

    return keys


def primary_key(row: Mapping[str, Any]) -> str:
    """The single strongest key for a row — what ``make_place_key`` returns.

    Note the place_id case returns the bare id, not ``place:<id>``: that is the
    existing on-disk contract for the ``Place Key`` column and Airtable's
    ``place:{place_key}`` upsert, and changing it would orphan every record
    already in the base.
    """
    keys = row_keys(row)
    if "place_id" in keys:
        return keys["place_id"]
    for cls in ("phone", "domain", "name_city"):
        if cls in keys:
            return f"{cls}:{keys[cls]}"
    return slug(pick(dict(row), "business_name", "Business Name", "name")) or "unknown"


def namespaced_keys(row: Mapping[str, Any]) -> Set[str]:
    """All four class keys, namespaced, for set-membership exclusion."""
    return {f"{_NAMESPACE[cls]}:{value}" for cls, value in row_keys(row).items()}


@dataclass(frozen=True)
class RegistryEntry:
    """One business the corpus has already paid for."""

    key: str
    place_id: str = ""
    phone: str = ""
    domain: str = ""
    name_city: str = ""
    first_seen: str = ""
    first_seen_batch: str = ""
    last_seen: str = ""
    last_seen_batch: str = ""

    def to_json(self) -> Dict[str, str]:
        return {
            "key": self.key,
            "place_id": self.place_id,
            "phone": self.phone,
            "domain": self.domain,
            "name_city": self.name_city,
            "first_seen": self.first_seen,
            "first_seen_batch": self.first_seen_batch,
            "last_seen": self.last_seen,
            "last_seen_batch": self.last_seen_batch,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Optional["RegistryEntry"]:
        key = str(data.get("key") or "").strip()
        if not key:
            return None
        return cls(
            key=key,
            place_id=str(data.get("place_id") or ""),
            phone=str(data.get("phone") or ""),
            domain=str(data.get("domain") or ""),
            name_city=str(data.get("name_city") or ""),
            first_seen=str(data.get("first_seen") or ""),
            first_seen_batch=str(data.get("first_seen_batch") or ""),
            last_seen=str(data.get("last_seen") or ""),
            last_seen_batch=str(data.get("last_seen_batch") or ""),
        )

    def namespaced_keys(self) -> Set[str]:
        out: Set[str] = set()
        for cls in KEY_CLASSES:
            value = getattr(self, cls, "")
            if value:
                out.add(f"{_NAMESPACE[cls]}:{value}")
        return out


def _merge(older: RegistryEntry, newer: RegistryEntry) -> RegistryEntry:
    """Fold a later append onto an earlier one.

    ``first_seen`` is the earliest observation and never moves; ``last_seen``
    always takes the newest, because it is what ``--refresh-older-than`` reads.
    The class keys take the newest non-empty value: a business that gains a
    website between batches should become excludable by domain too.
    """
    first_stamp = _parse_stamp(older.first_seen)
    newer_first = _parse_stamp(newer.first_seen)
    keep_older_first = older.first_seen and (
        newer_first is None or first_stamp is None or first_stamp <= newer_first
    )
    last_stamp = _parse_stamp(older.last_seen)
    newer_last = _parse_stamp(newer.last_seen)
    keep_newer_last = not older.last_seen or (
        newer_last is not None and (last_stamp is None or newer_last >= last_stamp)
    )
    return RegistryEntry(
        key=older.key,
        place_id=newer.place_id or older.place_id,
        phone=newer.phone or older.phone,
        domain=newer.domain or older.domain,
        name_city=newer.name_city or older.name_city,
        first_seen=older.first_seen if keep_older_first else newer.first_seen,
        first_seen_batch=older.first_seen_batch if keep_older_first else newer.first_seen_batch,
        last_seen=newer.last_seen if keep_newer_last else older.last_seen,
        last_seen_batch=newer.last_seen_batch if keep_newer_last else older.last_seen_batch,
    )


class PlaceRegistry:
    """Append-only JSONL ledger of businesses the corpus has already seen.

    Append-only rather than rewrite-in-place because a search run is
    interruptible and partially-billed: a crashed run must not be able to lose
    the record of the calls it already paid for. Reads fold the file by key, so
    repeated appends for one business collapse on load.

    Append-only is also what makes the format safe for the parallel-batch mode
    `run_agent_pipeline.py --lock-file` documents: two search runs share this
    one file, and O_APPEND plus one `write()` per sub-4096-byte line means
    neither can interleave inside a record. They can each miss the other's
    in-flight rows and re-bill an overlapping business once, which is a bounded
    cost; a rewrite-in-place would instead lose one run's rows entirely.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._entries: Dict[str, RegistryEntry] = {}
        self.malformed_lines = 0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    # A corrupt line costs at most one re-billed business, and
                    # rebuild_from_corpus heals it. Refusing to load the whole
                    # ledger would cost the entire corpus.
                    self.malformed_lines += 1
                    continue
                if not isinstance(data, dict):
                    self.malformed_lines += 1
                    continue
                entry = RegistryEntry.from_json(data)
                if entry is None:
                    self.malformed_lines += 1
                    continue
                existing = self._entries.get(entry.key)
                self._entries[entry.key] = _merge(existing, entry) if existing else entry

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> Dict[str, RegistryEntry]:
        return dict(self._entries)

    def _fresh_entries(
        self,
        refresh_older_than_days: Optional[int] = None,
        *,
        now: Optional[datetime] = None,
        exclude_batch: str = "",
    ) -> List[RegistryEntry]:
        """Entries still considered "known" — i.e. still worth skipping.

        With no refresh window every known business is skipped. With one, an
        entry last seen before the cutoff is dropped from the exclusion set and
        therefore re-admitted to the search. An entry whose ``last_seen`` will
        not parse stays excluded: the failure mode of guessing wrong here is
        spending money, so it fails towards not spending.

        ``exclude_batch`` drops the entries a given batch put here itself. A
        batch must not be blocked by its own history, or the second run of one
        batch name — which is what ``--overwrite`` and any crash-resume are —
        would silently produce zero leads and read as an exhausted market.
        """
        cutoff = (
            None
            if refresh_older_than_days is None
            else (now or utc_now()) - timedelta(days=max(0, int(refresh_older_than_days)))
        )
        fresh: List[RegistryEntry] = []
        for entry in self._entries.values():
            if exclude_batch and exclude_batch in (entry.first_seen_batch, entry.last_seen_batch):
                continue
            if cutoff is not None:
                seen = _parse_stamp(entry.last_seen)
                if seen is not None and seen < cutoff:
                    continue
            fresh.append(entry)
        return fresh

    def known_place_ids(
        self,
        refresh_older_than_days: Optional[int] = None,
        *,
        now: Optional[datetime] = None,
        exclude_batch: str = "",
    ) -> Set[str]:
        """Place ids to skip *before* the billed Place Details call."""
        return {
            entry.place_id
            for entry in self._fresh_entries(
                refresh_older_than_days, now=now, exclude_batch=exclude_batch
            )
            if entry.place_id
        }

    def known_keys(
        self,
        refresh_older_than_days: Optional[int] = None,
        *,
        now: Optional[datetime] = None,
        exclude_batch: str = "",
    ) -> Set[str]:
        """All namespaced class keys to exclude, for post-collection dedupe.

        Broader than :meth:`known_place_ids` on purpose: a business that comes
        back under a fresh place_id (Google re-issues them) is only catchable on
        phone, domain or name+city.
        """
        keys: Set[str] = set()
        for entry in self._fresh_entries(
            refresh_older_than_days, now=now, exclude_batch=exclude_batch
        ):
            keys |= entry.namespaced_keys()
        return keys

    def record(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        batch: str = "",
        now: Optional[datetime] = None,
    ) -> int:
        """Register rows this run paid for. Returns the number of keys written."""
        stamp = _stamp(now)
        pending: Dict[str, RegistryEntry] = {}
        for row in rows:
            keys = row_keys(row)
            key = primary_key(row)
            if not key or key == "unknown":
                continue
            candidate = RegistryEntry(
                key=key,
                place_id=keys.get("place_id", ""),
                phone=keys.get("phone", ""),
                domain=keys.get("domain", ""),
                name_city=keys.get("name_city", ""),
                first_seen=stamp,
                first_seen_batch=batch,
                last_seen=stamp,
                last_seen_batch=batch,
            )
            existing = pending.get(key) or self._entries.get(key)
            pending[key] = _merge(existing, candidate) if existing else candidate

        if not pending:
            return 0

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for entry in pending.values():
                handle.write(json.dumps(entry.to_json(), ensure_ascii=False) + "\n")
        self._entries.update(pending)
        return len(pending)

    def rewrite(self, entries: Iterable[RegistryEntry]) -> int:
        """Replace the ledger wholesale. Only :func:`rebuild_from_corpus` calls this."""
        folded: Dict[str, RegistryEntry] = {}
        for entry in entries:
            existing = folded.get(entry.key)
            folded[entry.key] = _merge(existing, entry) if existing else entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            for entry in folded.values():
                handle.write(json.dumps(entry.to_json(), ensure_ascii=False) + "\n")
        self._entries = folded
        self.malformed_lines = 0
        return len(folded)


def _batch_for(row: Mapping[str, Any], fallback: str) -> str:
    return pick(dict(row), "source_batch", "Source Batch") or fallback


def _read_deduped_csv(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _read_enriched_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            google = record.get("google")
            rows.append(dict(google) if isinstance(google, dict) else record)
    return rows


def corpus_artifacts(root: Path | str = Path(".")) -> List[Path]:
    """The artifacts a rebuild reads, oldest first.

    ``data/deduped/*.csv`` is every business a search run kept after in-batch
    dedupe — i.e. every business a Place Details call was billed for.
    ``output/enriched_*.jsonl`` covers batches whose deduped CSV has since been
    archived or deleted. Ordering by mtime is what makes ``first_seen`` mean the
    earliest batch: the records themselves carry no timestamp.
    """
    root = Path(root)
    paths = list((root / "data" / "deduped").glob("*.csv")) + list((root / "output").glob("enriched_*.jsonl"))
    return sorted(paths, key=lambda path: (path.stat().st_mtime, str(path)))


def rebuild_from_corpus(
    root: Path | str = Path("."),
    *,
    registry_path: Optional[Path | str] = None,
) -> PlaceRegistry:
    """Reconstruct the registry from the artifacts on disk.

    This is what keeps the ledger from becoming a second source of truth that
    can quietly drift from the corpus. Run it once to backfill, and any time the
    registry looks wrong. Idempotent: the file is rewritten, not appended to.

    ``last_seen`` comes from the artifact's mtime because enriched records carry
    no timestamp of their own. That is an approximation — a file copied forward
    reads as recent — and it only ever affects the ``--refresh-older-than``
    window, never whether a business is known at all.
    """
    root = Path(root)
    registry = PlaceRegistry(registry_path or registry_path_for(root))
    entries: List[RegistryEntry] = []
    for path in corpus_artifacts(root):
        stamp = _stamp(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))
        if path.suffix == ".csv":
            rows = _read_deduped_csv(path)
            fallback_batch = path.stem
        else:
            rows = _read_enriched_jsonl(path)
            fallback_batch = path.stem[len("enriched_"):] if path.stem.startswith("enriched_") else path.stem
        for row in rows:
            keys = row_keys(row)
            key = primary_key(row)
            if not key or key == "unknown":
                continue
            batch = _batch_for(row, fallback_batch)
            entries.append(
                RegistryEntry(
                    key=key,
                    place_id=keys.get("place_id", ""),
                    phone=keys.get("phone", ""),
                    domain=keys.get("domain", ""),
                    name_city=keys.get("name_city", ""),
                    first_seen=stamp,
                    first_seen_batch=batch,
                    last_seen=stamp,
                    last_seen_batch=batch,
                )
            )
    registry.rewrite(entries)
    return registry


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or rebuild the cross-batch place registry")
    parser.add_argument("--root", default=".", help="Repo root holding data/ and output/")
    parser.add_argument("--registry", default="", help="Registry path (default <root>/run_state/place_registry.jsonl)")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Rewrite the registry from data/deduped/*.csv and output/enriched_*.jsonl",
    )
    args = parser.parse_args(argv)

    root = Path(args.root)
    path = Path(args.registry) if args.registry else registry_path_for(root)

    if args.rebuild:
        artifacts = corpus_artifacts(root)
        registry = rebuild_from_corpus(root, registry_path=path)
        print(f"[DONE] Artifacts read: {len(artifacts)}")
        print(f"[DONE] Unique businesses: {len(registry)} -> {path}")
        return 0

    registry = PlaceRegistry(path)
    print(f"[INFO] Registry: {path}")
    print(f"[INFO] Unique businesses: {len(registry)}")
    print(f"[INFO] With a place_id: {len(registry.known_place_ids())}")
    if registry.malformed_lines:
        print(f"[WARN] Skipped {registry.malformed_lines} malformed line(s); rerun with --rebuild")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
