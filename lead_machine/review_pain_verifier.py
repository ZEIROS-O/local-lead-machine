from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping


VERIFIER_PATTERNS: Mapping[str, tuple[str, ...]] = {
    "unreachable": (
        r"\bno one answered\b",
        r"\bcould not reach\b",
        r"\bcouldn't reach\b",
        r"\bnever picked up\b",
    ),
    "no_callback": (
        r"\bnever called back\b",
        r"\bno call ?back\b",
        r"\bdid not return (?:my |the )?call\b",
        r"\bdidn't return (?:my |the )?call\b",
    ),
    "quote_followup": (
        r"\bnever (?:sent|received|got) (?:the |a )?(?:quote|estimate)\b",
        r"\bno follow[- ]?up\b",
        r"\bghosted (?:me|us)\b",
    ),
    "scheduling": (
        r"\bmissed (?:the |my |our )?appointment\b",
        r"\bnever showed\b",
        r"\bno[- ]show\b",
        r"\bcould not (?:schedule|book)\b",
    ),
    "logistics": (
        r"\b(?:hours?|days?) late\b",
        r"\bdid not arrive\b",
        r"\bdidn't arrive\b",
        r"\bwrong (?:address|location|vehicle|job)\b",
    ),
    "retention": (
        r"\bwon't use (?:them|again)\b",
        r"\bwill not use (?:them|again)\b",
        r"\bcancelled (?:my |our )?(?:service|appointment)\b",
    ),
    "service_quality": (
        r"\bterrible job\b",
        r"\bbad job\b",
        r"\bdamaged\b",
        r"\bunfinished\b",
        r"\bincomplete work\b",
    ),
    "integrity_or_safety": (
        r"\bscam\b",
        r"\bfraud\b",
        r"\bstole\b",
        r"\bunsafe\b",
        r"\bdangerous\b",
    ),
}


@dataclass(frozen=True)
class ReviewPainVerification:
    category: str
    verdict: str
    rule_id: str


def verify_review_pain(category: str, text: str) -> ReviewPainVerification:
    patterns = VERIFIER_PATTERNS.get(category, ())
    matched = any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)
    return ReviewPainVerification(
        category=category,
        verdict="PASS" if matched else "FAIL",
        rule_id=f"review-pain-verifier-v1:{category}",
    )
