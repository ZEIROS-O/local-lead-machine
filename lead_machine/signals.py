from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping


SIGNAL_STATES = {"present", "absent", "unknown", "error", "not_applicable"}


@dataclass(frozen=True)
class SignalObservation:
    signal_id: str
    kind: str
    state: str
    value: Any = None
    source: str = ""
    observed_at: str = ""
    expires_at: str = ""
    confidence: float = 0.0
    evidence_refs: tuple[str, ...] = ()
    cost_usd: float = 0.0
    metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.signal_id.strip() or not self.kind.strip():
            raise ValueError("signal_id and kind are required")
        if self.state not in SIGNAL_STATES:
            raise ValueError(f"invalid signal state: {self.state}")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("signal confidence must be from 0 through 1")
        if self.state == "unknown" and self.value not in (None, "", [], {}):
            raise ValueError("unknown observations cannot carry an asserted value")

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["evidence_refs"] = list(self.evidence_refs)
        value["metadata"] = dict(self.metadata or {})
        return value
