from __future__ import annotations

import json
from typing import Any, Dict, List

from .evidence import THIRD_PARTY_TOOLS, _detect_from_text
from .review_velocity import build_review_velocity


UNKNOWN_ADS = {
    "google_ad_transparency": "unknown",
    "meta_ad_library": "unknown",
    "lsa_badge": "unknown",
    "errors": [],
}

# money_signal is tri-state. "no" and "unknown" are not the same claim: paid-CRM
# detection reads text scraped *from the website*, so for a business with no site
# there was nothing to read. Collapsing that to False told the scorer "this
# business does not spend" when the truth was "we never looked" — and the LLM
# rubric rejected the lead for not connecting its gap to paid acquisition.
MONEY_SIGNAL_YES = "yes"
MONEY_SIGNAL_NO = "no"
MONEY_SIGNAL_UNKNOWN = "unknown"

DEMAND_SIGNAL_YES = "yes"
DEMAND_SIGNAL_NO = "no"
DEMAND_SIGNAL_UNKNOWN = "unknown"

ACTIVE_VELOCITY_BANDS = frozenset({"surging", "active", "steady"})
DEMAND_BANDS_WITH_PULL = frozenset({"ideal_growth", "established", "mature"})

# Neither check has a parser. They used to issue a request, discard the response
# and return "unknown" regardless — a real network round trip that could not
# change the answer. They are named here so `_ads_signals` can say so out loud
# in ads["errors"] instead of leaving an unqualified "unknown" that reads like a
# check which ran and found nothing.
UNIMPLEMENTED_AD_CHECKS = frozenset({"google_ad_transparency", "meta_ad_library"})

REVIEW_DEMAND_THRESHOLDS = {
    "detailing": {
        "early_proof_min": 5,
        "ideal_growth_min": 15,
        "established_min": 126,
        "mature_min": 401,
    },
    "default": {
        "early_proof_min": 5,
        "ideal_growth_min": 15,
        "established_min": 76,
        "mature_min": 201,
    },
}

PAID_CRM = {
    "Jobber",
    "Housecall Pro",
    "GHL",
    "ServiceTitan",
    "Thryv",
    "Urable",
    "Booksy",
    "Square Appointments",
    "Mobile Tech RX",
}

BUYER_INTENT_LOGIC_VERSION = 4


def build_buyer_intent(record: Dict[str, Any], *, ads_check: bool = False) -> Dict[str, Any]:
    detected_tools = _detected_tools(record)
    paid_tools = sorted(set(detected_tools) & PAID_CRM)
    has_paid_crm = bool(paid_tools)
    crm_evidence = [f"Detected paid CRM/booking tool: {tool}" for tool in paid_tools]

    review_profile = _review_profile(record)
    review_velocity = build_review_velocity(record)
    ads = _ads_signals(record, ads_check=ads_check)

    known_ad_yes = any(
        ads.get(field) == "yes"
        for field in ("google_ad_transparency", "meta_ad_library", "lsa_badge")
    )
    money_signal = _money_signal_state(
        record, has_paid_crm=has_paid_crm, known_ad_yes=known_ad_yes
    )
    demand_signal = _demand_signal(review_profile, review_velocity)

    evidence: List[str] = []
    evidence.extend(crm_evidence)
    if review_profile["demand_band"] != "thin":
        evidence.extend(review_profile["evidence"])
    for field in ("google_ad_transparency", "meta_ad_library", "lsa_badge"):
        if ads.get(field) == "yes":
            evidence.append(f"{field.replace('_', ' ')} detected")
    if money_signal == MONEY_SIGNAL_UNKNOWN:
        evidence.append(
            "Paid-acquisition spend was not observable: there was no crawlable site text "
            "to read a CRM or booking stack from. This is absence of observation, not "
            "evidence that the business does not spend."
        )
    evidence.extend(demand_signal.get("evidence") or [])

    buyer_intent = {
        "logic_version": BUYER_INTENT_LOGIC_VERSION,
        "crm_booking_stack": {
            "detected_tools": detected_tools,
            "has_paid_crm": has_paid_crm,
            "evidence": crm_evidence,
        },
        "review_profile": review_profile,
        "review_demand_signal": review_profile,
        "review_velocity": review_velocity,
        "ads": ads,
        "money_signal": money_signal,
        "demand_signal": demand_signal,
        "evidence": evidence,
    }
    record["buyer_intent"] = buyer_intent
    return buyer_intent


def ensure_buyer_intent(record: Dict[str, Any], *, ads_check: bool = False) -> Dict[str, Any]:
    buyer_intent = record.get("buyer_intent")
    if _valid_buyer_intent(buyer_intent):
        return buyer_intent
    return build_buyer_intent(record, ads_check=ads_check)


def has_money_signal(record: Dict[str, Any]) -> bool:
    """True only for a confirmed *yes*.

    Every gate that reads this must treat "unknown" the way it used to treat
    False. `sendability.required_visible_pain_score` drops the mature-band pain
    threshold from 40 to 35 when money_signal is truthy, so an "unknown" leaking
    through as True would loosen the gate on exactly the leads we know least
    about.
    """
    return money_signal_state(record) == MONEY_SIGNAL_YES


def money_signal_state(record: Dict[str, Any]) -> str:
    """Return the tri-state money signal for *record*: yes / no / unknown."""
    buyer_intent = _buyer_intent_from_record(record)
    if not buyer_intent:
        raw = record.get("Money Signal") or record.get("money_signal")
        return _normalize_money_signal(raw)
    return _normalize_money_signal(buyer_intent.get("money_signal"))


def money_signal_label(record: Dict[str, Any]) -> str:
    """Title-cased tri-state for row columns and the LLM capsule."""
    return money_signal_state(record).capitalize()


def demand_signal_state(record: Dict[str, Any]) -> str:
    buyer_intent = _buyer_intent_from_record(record)
    signal = buyer_intent.get("demand_signal") if isinstance(buyer_intent, dict) else None
    if isinstance(signal, dict):
        state = str(signal.get("state") or "").strip().lower()
        if state in {DEMAND_SIGNAL_YES, DEMAND_SIGNAL_NO, DEMAND_SIGNAL_UNKNOWN}:
            return state
    return DEMAND_SIGNAL_UNKNOWN


def _normalize_money_signal(value: Any) -> str:
    """Map any historical or current representation onto the tri-state.

    Records written before logic_version 4 stored a bool. `ensure_buyer_intent`
    rebuilds those, but rows read back from a CSV or the base still carry the
    old "Yes"/"No" text, and those must keep meaning what they meant.
    """
    if isinstance(value, bool):
        return MONEY_SIGNAL_YES if value else MONEY_SIGNAL_NO
    text = str(value or "").strip().lower()
    if text in {"yes", "true", "1"}:
        return MONEY_SIGNAL_YES
    if text in {"no", "false", "0"}:
        return MONEY_SIGNAL_NO
    return MONEY_SIGNAL_UNKNOWN


def buyer_intent_summary(record: Dict[str, Any]) -> str:
    buyer_intent = _buyer_intent_from_record(record)
    if not buyer_intent:
        return (
            "money_signal=unknown; demand_signal=unknown; tools=none; "
            "review_demand_band=unknown; ads=unknown"
        )

    stack = buyer_intent.get("crm_booking_stack") or {}
    review_signal = _review_signal_from_buyer_intent(buyer_intent)
    ads = buyer_intent.get("ads") or {}
    tools = stack.get("detected_tools") or []
    if isinstance(tools, str):
        tools = [part.strip() for part in tools.replace(",", ";").split(";") if part.strip()]
    known_ads = {
        key: value
        for key, value in ads.items()
        if key != "errors" and str(value or "").strip().lower() in {"yes", "no"}
    }
    ads_text = ", ".join(f"{key}={value}" for key, value in sorted(known_ads.items())) or "unknown"
    demand = buyer_intent.get("demand_signal") if isinstance(buyer_intent.get("demand_signal"), dict) else {}
    return (
        f"money_signal={_normalize_money_signal(buyer_intent.get('money_signal'))}; "
        f"demand_signal={str(demand.get('state') or 'unknown').lower()}; "
        f"tools={', '.join(str(tool) for tool in tools) or 'none'}; "
        f"review_demand_band={review_signal.get('demand_band') or 'unknown'}; "
        f"review_velocity={((buyer_intent.get('review_velocity') or {}).get('velocity_band') if isinstance(buyer_intent.get('review_velocity'), dict) else 'unknown') or 'unknown'}; "
        f"ads={ads_text}"
    )


def _buyer_intent_from_record(record: Dict[str, Any]) -> Dict[str, Any]:
    buyer_intent = record.get("buyer_intent")
    if isinstance(buyer_intent, dict):
        return buyer_intent
    raw = record.get("Raw Evidence")
    if raw:
        try:
            parsed = raw if isinstance(raw, dict) else json.loads(str(raw))
        except Exception:
            parsed = {}
        if isinstance(parsed, dict) and isinstance(parsed.get("buyer_intent"), dict):
            return parsed["buyer_intent"]
    return {}


def _valid_buyer_intent(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if _buyer_intent_logic_version(value) < BUYER_INTENT_LOGIC_VERSION:
        return False
    return all(
        key in value
        for key in (
            "crm_booking_stack",
            "review_velocity",
            "ads",
            "money_signal",
            "demand_signal",
            "evidence",
        )
    )


def _buyer_intent_logic_version(value: Dict[str, Any]) -> int:
    raw_version = value.get("logic_version", value.get("version"))
    try:
        return int(raw_version)
    except (TypeError, ValueError):
        return 0


def _money_signal_state(
    record: Dict[str, Any], *, has_paid_crm: bool, known_ad_yes: bool
) -> str:
    if has_paid_crm or known_ad_yes:
        return MONEY_SIGNAL_YES
    # "no" is only claimable where the observation was possible. Paid-CRM
    # detection reads scraped site text; with no text there is no negative to
    # report, only an unanswered question.
    if _site_text_observed(record):
        return MONEY_SIGNAL_NO
    return MONEY_SIGNAL_UNKNOWN


def _site_text_observed(record: Dict[str, Any]) -> bool:
    """Did the crawl actually return text a CRM could be detected in?

    A site that is missing, dead, or rendered nothing is indistinguishable here
    from one never visited — in all three cases the paid-stack question went
    unanswered.
    """
    website = record.get("website") if isinstance(record.get("website"), dict) else {}
    if str(website.get("combined_text") or "").strip():
        return True

    homepage = website.get("homepage") if isinstance(website.get("homepage"), dict) else {}
    if any(str(homepage.get(key) or "").strip() for key in ("text", "title", "meta")):
        return True

    pages = website.get("pages") if isinstance(website.get("pages"), dict) else {}
    for page in pages.values():
        if isinstance(page, dict) and str(page.get("text") or "").strip():
            return True

    # Tools already detected upstream are themselves proof the site was read.
    evidence = record.get("evidence") if isinstance(record.get("evidence"), dict) else {}
    return bool(evidence.get("third_party_tools"))


def _demand_signal(review_profile: Dict[str, Any], review_velocity: Dict[str, Any]) -> Dict[str, Any]:
    """Whether the market is pulling on this business — kept apart from spend.

    Deliberately *not* folded into `money_signal`: that value gates
    `sendability.required_visible_pain_score`, where a false positive drops the
    mature-band pain threshold from 40 to 35. Demand is a different claim from
    willingness to pay, and only the latter belongs in that gate.
    """
    velocity_state = str(review_velocity.get("state") or "unknown").strip().lower()
    velocity_band = str(review_velocity.get("velocity_band") or "unknown").strip().lower()
    demand_band = str(review_profile.get("demand_band") or "").strip().lower()
    reviews = _safe_int(review_profile.get("reviews"))
    rating = _safe_float(review_profile.get("rating"))

    if velocity_state == "present" and velocity_band in ACTIVE_VELOCITY_BANDS:
        rate = review_velocity.get("reviews_per_30d")
        return {
            "state": DEMAND_SIGNAL_YES,
            "source": "review_velocity",
            "velocity_band": velocity_band,
            "demand_band": demand_band,
            "evidence": [
                f"Reviews are still arriving ({rate} per 30 days, cohort band {velocity_band})."
            ],
        }
    if velocity_state == "present" and velocity_band == "stale":
        return {
            "state": DEMAND_SIGNAL_NO,
            "source": "review_velocity",
            "velocity_band": velocity_band,
            "demand_band": demand_band,
            "evidence": ["Measured review velocity has gone flat against its cohort."],
        }

    if not reviews and not rating:
        return {
            "state": DEMAND_SIGNAL_UNKNOWN,
            "source": "none",
            "velocity_band": velocity_band,
            "demand_band": demand_band,
            "evidence": ["No review observation at all; demand is unmeasured, not absent."],
        }

    # Lifetime count is a weaker claim than velocity, and it is labelled as one:
    # it says customers have arrived, not that they are arriving.
    pulls = demand_band in DEMAND_BANDS_WITH_PULL
    return {
        "state": DEMAND_SIGNAL_YES if pulls else DEMAND_SIGNAL_NO,
        "source": "review_count",
        "velocity_band": velocity_band,
        "demand_band": demand_band,
        "evidence": [
            f"Lifetime review count places this in the {demand_band or 'thin'} demand band "
            f"({reviews} reviews); velocity is unmeasured."
        ],
    }


def _review_signal_from_buyer_intent(buyer_intent: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("review_demand_signal", "review_profile", "review_velocity"):
        value = buyer_intent.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _detected_tools(record: Dict[str, Any]) -> List[str]:
    evidence = record.get("evidence", {}) if isinstance(record.get("evidence"), dict) else {}
    existing = evidence.get("third_party_tools") or []
    if isinstance(existing, str):
        existing = [part.strip() for part in existing.replace(",", ";").split(";") if part.strip()]
    if not isinstance(existing, list):
        existing = []

    text = _record_text(record)
    detected = _detect_from_text(text, THIRD_PARTY_TOOLS)
    return sorted({str(tool).strip() for tool in [*existing, *detected] if str(tool).strip()})


def _record_text(record: Dict[str, Any]) -> str:
    google = record.get("google", {}) if isinstance(record.get("google"), dict) else {}
    website = record.get("website", {}) if isinstance(record.get("website"), dict) else {}
    evidence = record.get("evidence", {}) if isinstance(record.get("evidence"), dict) else {}
    homepage = website.get("homepage", {}) if isinstance(website.get("homepage"), dict) else {}
    pages = website.get("pages", {}) if isinstance(website.get("pages"), dict) else {}
    parts = [
        google.get("business_name", ""),
        google.get("category", ""),
        google.get("website_url", ""),
        website.get("final_url", ""),
        website.get("combined_text", ""),
        homepage.get("title", ""),
        homepage.get("meta", ""),
        homepage.get("text", ""),
        " ".join(str(link) for link in website.get("social_links") or []),
        " ".join(str(tool) for tool in evidence.get("third_party_tools") or []),
    ]
    for page in pages.values():
        if isinstance(page, dict):
            parts.extend([page.get("url", ""), page.get("title", ""), page.get("text", "")])
    return " ".join(str(part or "") for part in parts)


def review_demand_band(record: Dict[str, Any], *, vertical: str = "") -> str:
    working = record
    if vertical:
        google = record.get("google", {}) if isinstance(record.get("google"), dict) else {}
        if not google.get("vertical"):
            working = dict(record)
            google = dict(google)
            google["vertical"] = vertical
            working["google"] = google
    return str(_review_profile(working).get("demand_band") or "thin")


def _review_profile(record: Dict[str, Any]) -> Dict[str, Any]:
    google = record.get("google", {}) if isinstance(record.get("google"), dict) else {}
    rating = _safe_float(
        record.get("Rating")
        or record.get("rating")
        or record.get("google_rating")
        or google.get("rating")
    )
    reviews = _safe_int(
        record.get("Reviews")
        or record.get("reviews")
        or record.get("google_review_count")
        or google.get("reviews")
    )

    thresholds = REVIEW_DEMAND_THRESHOLDS["detailing" if _vertical_family(record) == "detailing" else "default"]
    if reviews >= thresholds["mature_min"]:
        band = "mature"
    elif reviews >= thresholds["established_min"]:
        band = "established"
    elif reviews >= thresholds["ideal_growth_min"]:
        band = "ideal_growth"
    elif reviews >= thresholds["early_proof_min"]:
        band = "early_proof"
    else:
        band = "thin"

    evidence = [f"{reviews} Google reviews with {rating:g} rating"] if reviews or rating else []
    return {
        "demand_band": band,
        "source": "count_only",
        "reviews": reviews,
        "rating": rating,
        "evidence": evidence,
    }


def _vertical_family(record: Dict[str, Any]) -> str:
    google = record.get("google", {}) if isinstance(record.get("google"), dict) else {}
    parts = [
        record.get("vertical"),
        record.get("Vertical"),
        google.get("vertical"),
        google.get("source_query"),
        google.get("category"),
    ]
    text = " ".join(str(part or "").lower() for part in parts)
    if any(term in text for term in ("detailing", "detailer", "ceramic coating", "paint correction", "ppf")):
        return "detailing"
    return "default"


def _ads_signals(record: Dict[str, Any], *, ads_check: bool) -> Dict[str, Any]:
    ads = {
        "google_ad_transparency": "unknown",
        "meta_ad_library": "unknown",
        "lsa_badge": "unknown",
        "errors": [],
    }
    if not ads_check:
        return ads

    checks = (
        ("google_ad_transparency", _check_google_ad_transparency),
        ("meta_ad_library", _check_meta_ad_library),
        ("lsa_badge", _check_lsa_badge),
    )
    for field, check in checks:
        if field in UNIMPLEMENTED_AD_CHECKS:
            ads[field] = "unknown"
            ads["errors"].append(f"{field}: unimplemented")
            continue
        try:
            ads[field] = check(record)
        except Exception as exc:  # pragma: no cover - defensive path, behavior asserted via no-raise contract.
            ads[field] = "unknown"
            ads["errors"].append(f"{field}: {exc}")
    return ads


def _check_google_ad_transparency(record: Dict[str, Any]) -> str:
    """Not implemented. Listed in UNIMPLEMENTED_AD_CHECKS and never called.

    The previous body fetched the Ad Transparency Center page, discarded the
    response and returned "unknown" on every path — a live HTTP round trip per
    lead that could not change the answer. Reading it needs a parser for the
    page's rendered payload; until that exists, saying so costs nothing.
    """
    return "unknown"


def _check_meta_ad_library(record: Dict[str, Any]) -> str:
    """Not implemented. See `_check_google_ad_transparency`."""
    return "unknown"


def _check_lsa_badge(record: Dict[str, Any]) -> str:
    text = _record_text(record).lower()
    if "google guaranteed" in text or "local services ads" in text:
        return "yes"
    return "unknown"


def _safe_int(value: Any) -> int:
    try:
        if value in ("", None):
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        if value in ("", None):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
