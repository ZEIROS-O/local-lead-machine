"""Best-first ordering for the dial queue.

Nine checks came out of `sendability` because they answered the wrong question.
Score, grade, confidence, rating, review count, demand, angle, send trigger and
the maturity-scaled pain threshold do not say whether what we would tell a
business is true — they say whether the lead looks worth the call. As gates they
destroyed the tail: 2,595 of 2,995 scored leads were refused, most of them by
those nine, on an offer with zero closes behind it.

Ranking preserves what they were protecting — the rep's time — without the
destruction. A weak lead sorts last instead of not existing, and the judgement
stays revisable, because a weight is a number to tune where a gate is a lead
that never happened.

Seven of the nine rank. Two do not, and saying "all nine became weights" would
be false:

  * `Offer Fit` (the old "Missing clear outreach angle") is present on all
    1,042 currently sendable leads. It was a presence check over a categorical
    label, not a quality, so there is no direction to sort it in and nothing to
    separate — weighting it would move no lead past any other.
  * `Send Trigger` (the old "Missing concrete send trigger") is absent on 124
    of those 1,042, so unlike Offer Fit it would discriminate. It is left out
    anyway, deliberately: adding it means inventing a seventh weight, and the
    six below have never been checked against a close. Substituting a second
    invention for the first buys no accuracy and costs the ability to read the
    experiment. Revisit it when accept-rate data exists — that is a decision
    for outcomes to make, not this module.

`tests/test_dial_rank.py` pins both, so restoring either is a deliberate edit
rather than a silent one.

Why this and not `scoring_v3`. That module already computes a 0-100 rank and was
the obvious candidate, but it resolves a per-vertical profile and returns *no
rank at all* when there is no exact match. `scoring_profiles/` covers detailing,
plumbing and water restoration; the live verticals are epoxy and garage doors. It
scores 59 of the 1,042 currently sendable leads. Sorting the queue on a column
blank for 94% of it would be worse than the grade sort it replaced. This reads
only fields every scored row carries, so it works the day a vertical is added
rather than after someone authors a profile for it.

The weights are a starting point, not a finding. Nothing here has been validated
against a close, because there has not been one. They are declared in one table
so that when accept-rate data arrives it lands as an edit to that table.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

from .buyer_signals import money_signal_from_summary, money_signal_state

RANK_MAX = 100.0

# Sum to RANK_MAX. Ordered by how directly each answers "will this call go well".
WEIGHTS: Mapping[str, float] = {
    # The leak itself. A specific, severe, visible problem is the whole pitch,
    # so it carries the most and is the only component that can dominate.
    "pain": 35.0,
    # Proof the market is pulling on them: reviews and rating together. Demand is
    # what makes a fixable leak worth money.
    "demand": 25.0,
    # Evidence that they already spend on their business.
    "money": 15.0,
    # The model's confidence in its own read, and how much it cited. Low here
    # means dial later, not never.
    "confidence": 10.0,
    "evidence": 5.0,
    # The holistic score. Deliberately small: it is a summary of the same
    # signals above, so a large weight would double-count them.
    "score": 10.0,
}

# Reviews at which the demand component saturates. Past this the business is
# established and more reviews say nothing new about whether they need us — the
# review *ceiling* was deleted for exactly that reason (AGENTS.md), so this is a
# flattening, never a penalty.
DEMAND_SATURATION_REVIEWS = 150.0
DEMAND_REVIEW_SHARE = 0.7  # the rest is rating

CONFIDENCE_POINTS = {"high": 1.0, "medium": 0.6, "low": 0.25}
# `unknown` sits between `yes` and `no`, never level with `no`. AGENTS.md: it
# means there was no crawlable text to read a paid stack from — an unanswered
# question, not evidence the business does not spend. A rank that scored it as
# `no` would re-import the bug the tri-state was introduced to fix.
MONEY_POINTS = {"yes": 1.0, "unknown": 0.5, "no": 0.0}

# Grade ranks rows that tie on everything else. It is not a component: it is
# derived from the same subscores the components read, so scoring it too would
# count them twice.
_GRADE_ORDER = {"A": 0, "B": 1, "C": 2}
_UNGRADED = 9


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _pick(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in ("", None, [], {}):
            return _clean(value)
    return ""


def _number(row: Mapping[str, Any], *keys: str) -> float:
    """Parse a numeric cell, treating anything unparseable as absent.

    Absent is 0.0 here rather than an error on purpose: this is an ordering, and
    a row missing a signal should sink rather than take down the queue. The gate
    is where a missing value is allowed to refuse a lead.
    """
    raw = _pick(row, *keys)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _unit(value: float, ceiling: float) -> float:
    """Clamp *value* into 0..1 against *ceiling*."""
    if ceiling <= 0:
        return 0.0
    return max(0.0, min(1.0, value / ceiling))


def _pain_fraction(row: Mapping[str, Any]) -> float:
    return _unit(_number(row, "Visible Pain Score", "visible_pain_score"), 100.0)


def _demand_fraction(row: Mapping[str, Any]) -> float:
    reviews = _unit(
        _number(row, "Reviews", "reviews", "google_review_count"),
        DEMAND_SATURATION_REVIEWS,
    )
    # 4.0 is the floor a lead used to be refused below, and 5.0 is the ceiling,
    # so the band that actually discriminates is one star wide.
    rating = _unit(_number(row, "Rating", "rating", "google_rating") - 4.0, 1.0)
    return reviews * DEMAND_REVIEW_SHARE + rating * (1.0 - DEMAND_REVIEW_SHARE)


def _confidence_fraction(row: Mapping[str, Any]) -> float:
    return CONFIDENCE_POINTS.get(_pick(row, "AI Confidence", "confidence").lower(), 0.0)


def _money_fraction(row: Mapping[str, Any]) -> float:
    """Money credit, reading the capsule when the direct column is absent.

    A queue row read back from Airtable has no `Money Signal` column — the base
    does not have one — so the direct read answers `unknown` for every lead and
    this component becomes a constant that orders nothing. The capsule in
    `Buyer Intent Signals` carries the same state and *is* a base column, so it
    is consulted whenever the direct read comes back unanswered.
    """
    record = dict(row)
    state = money_signal_state(record)
    if state == "unknown":
        state = money_signal_from_summary(record)
    return MONEY_POINTS.get(state, 0.5)


def _evidence_fraction(row: Mapping[str, Any]) -> float:
    refs = _pick(row, "Evidence Refs", "evidence_refs")
    if not refs:
        return 0.0
    count = len([part for part in refs.replace(";", ",").split(",") if part.strip()])
    return _unit(float(count), 3.0)


def _score_fraction(row: Mapping[str, Any]) -> float:
    return _unit(_number(row, "Score Total", "total_score", "score"), 100.0)


def rank_components(row: Mapping[str, Any]) -> Dict[str, float]:
    """The rank broken into its parts, for explaining an order to a human.

    Kept public because a queue nobody can interrogate is one nobody trusts: the
    first question about a surprising order is "why is this lead above that one",
    and it should be answerable without reading this module.
    """
    fractions = {
        "pain": _pain_fraction(row),
        "demand": _demand_fraction(row),
        "money": _money_fraction(row),
        "confidence": _confidence_fraction(row),
        "evidence": _evidence_fraction(row),
        "score": _score_fraction(row),
    }
    return {name: round(WEIGHTS[name] * value, 4) for name, value in fractions.items()}


def dial_rank(row: Mapping[str, Any]) -> float:
    """A 0-100 ordering for *row*. Deterministic, no model call."""
    return round(min(RANK_MAX, max(0.0, sum(rank_components(row).values()))), 4)


def dial_sort_key(row: Mapping[str, Any]) -> tuple:
    """Sort key placing the best lead first under a plain ascending `sorted`.

    Grade breaks ties, then business name. The name is there so the order is
    total: two leads scoring identically must not swap places between runs, or
    "call the top ten" means a different ten each morning.
    """
    grade = _pick(row, "Lead Priority", "grade", "Grade").upper()
    return (
        -dial_rank(row),
        _GRADE_ORDER.get(grade, _UNGRADED),
        _pick(row, "Business Name", "business_name").lower(),
    )
