"""One home for the outreach follow-up cadence.

`mark_sent.py` sets the first follow-up date and `mark_outcome.py` advances or
clears it. Both used to be free to disagree about what "the cadence" is; the
config lives here so there is exactly one definition of it.

The cadence documented in operator-agent's `SKILLS/outreach` is Day 0/4/11/14.
Only the Day 0 -> Day 4 step is mechanical today: `mark_sent` fires once per
lead, and `mark_outcome` re-arms the same step each time a prospect moves but
stays open. The later touches are Loop C's job and are not built.
"""
from __future__ import annotations

import datetime as dt
import os


DEFAULT_FOLLOW_UP_CADENCE_DAYS = 4


def follow_up_cadence_days() -> int:
    """Days between a touch and the next follow-up.

    Read lazily so a `.env` value picked up by `load_dotenv()` in a script's
    `main()` is honored rather than frozen at import time.
    """
    return int(os.getenv("FOLLOW_UP_CADENCE_DAYS", str(DEFAULT_FOLLOW_UP_CADENCE_DAYS)))


def next_follow_up_date(anchor: str) -> str:
    """`anchor` (an ISO date) plus one cadence step, as an ISO date."""
    return (dt.date.fromisoformat(anchor) + dt.timedelta(days=follow_up_cadence_days())).isoformat()
