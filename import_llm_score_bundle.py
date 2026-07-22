#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

from lead_machine.manual_scores import validate_manual_score
from lead_machine.score_schema import V2_SCORE_FIELDS
from lead_machine.sendability import normalize_evidence_refs


REQUIRED_BUNDLE_SCORE_KEYS = {
    "subscores",
    "total_score",
    "grade",
    "confidence",
    "hard_disqualifiers",
    "best_angle",
    "recommended_action",
    "why_this_lead",
    "why_this_might_be_wrong",
    "opener",
    "evidence_refs",
    "safe_outreach_claims",
    "unsafe_claims",
    "airtable_fields",
    *V2_SCORE_FIELDS,
}


@dataclass(frozen=True)
class ImportResult:
    scores_imported: int
    files_written: int
    validation_failures: List[str]


def load_bundle(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON bundle: {exc.msg} at line {exc.lineno} column {exc.colno}") from exc

    if not isinstance(data, dict):
        raise ValueError("Bundle must be one JSON object")
    return data


def validate_score_file(score_file: Any) -> str:
    if not isinstance(score_file, str) or not score_file.strip():
        raise ValueError("score_file must be a non-empty string")

    cleaned = score_file.strip()
    if not cleaned.endswith(".json"):
        raise ValueError(f"score_file must end with .json: {cleaned}")
    if "/" in cleaned or "\\" in cleaned or Path(cleaned).name != cleaned or Path(cleaned).is_absolute():
        raise ValueError(f"unsafe score_file path: {cleaned}")

    return cleaned


def allowed_ids_from_score(score: Dict[str, Any]) -> set[str]:
    return set(normalize_evidence_refs(score.get("evidence_refs")))


def validate_bundle_scores(bundle: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    scores = bundle.get("scores")
    if not isinstance(scores, list):
        raise ValueError("Bundle must have scores array")
    if not scores:
        raise ValueError("scores array must contain at least one score")

    failures: List[str] = []
    validated_scores: List[Tuple[str, Dict[str, Any]]] = []
    seen_files: set[str] = set()

    for index, item in enumerate(scores):
        location = f"scores[{index}]"
        if not isinstance(item, dict):
            failures.append(f"{location}: item must be an object")
            continue

        try:
            score_file = validate_score_file(item.get("score_file"))
        except ValueError as exc:
            failures.append(f"{location}: {exc}")
            continue

        if score_file in seen_files:
            failures.append(f"{location}: duplicate score_file: {score_file}")
            continue
        seen_files.add(score_file)

        score = item.get("score")
        if not isinstance(score, dict):
            failures.append(f"{location}: score must be an object")
            continue

        missing = sorted(REQUIRED_BUNDLE_SCORE_KEYS - set(score.keys()))
        if missing:
            failures.append(f"{location} ({score_file}): score missing required keys: {', '.join(missing)}")
            continue

        try:
            validated = validate_manual_score(
                score,
                allowed_evidence_ids=allowed_ids_from_score(score),
            )
        except ValueError as exc:
            failures.append(f"{location} ({score_file}): {exc}")
            continue

        validated_scores.append((score_file, validated))

    if failures:
        raise ValueError("Validation failures:\n" + "\n".join(f"- {failure}" for failure in failures))

    return validated_scores


def import_score_bundle(input_path: Path, scores_dir: Path) -> ImportResult:
    bundle = load_bundle(input_path)
    validated_scores = validate_bundle_scores(bundle)

    scores_dir.mkdir(parents=True, exist_ok=True)
    for score_file, score in validated_scores:
        path = scores_dir / score_file
        path.write_text(json.dumps(score, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return ImportResult(
        scores_imported=len(validated_scores),
        files_written=len(validated_scores),
        validation_failures=[],
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="JSON response bundle copied from ChatGPT or Claude")
    parser.add_argument("--scores-dir", required=True, help="Directory where individual score JSON files should be written")
    args = parser.parse_args()

    try:
        result = import_score_bundle(Path(args.input), Path(args.scores_dir))
    except ValueError as exc:
        print("[FAIL] Scores imported: 0")
        print("[FAIL] Files written: 0")
        print("[FAIL] Validation failures: 1")
        print(f"[FAIL] {exc}")
        return 1

    print(f"[DONE] Scores imported: {result.scores_imported}")
    print(f"[DONE] Files written: {result.files_written}")
    print(f"[DONE] Validation failures: {len(result.validation_failures)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
