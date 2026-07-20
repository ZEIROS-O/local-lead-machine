from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Sequence

import requests

from .signals import SignalObservation


DATAFORSEO_REVIEWS_TASK_POST = "https://api.dataforseo.com/v3/business_data/google/reviews/task_post"
DATAFORSEO_REVIEWS_TASK_GET = "https://api.dataforseo.com/v3/business_data/google/reviews/task_get/{task_id}"
SEMRUSH_MAP_BASE = "https://api.semrush.com/apis/v4/map-rank-tracker/v0"
PAGESPEED_API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


class ExternalSignalError(RuntimeError):
    pass


class BudgetExceeded(ExternalSignalError):
    pass


@dataclass
class RunBudget:
    max_usd: float = 10.0
    max_units: int = 2500
    used_usd: float = 0.0
    used_units: int = 0

    def reserve_usd(self, amount: float) -> None:
        amount = max(0.0, float(amount))
        if self.used_usd + amount > self.max_usd + 1e-9:
            raise BudgetExceeded(
                f"[STOP] MAX_NEW_DATA_USD_PER_RUN would be exceeded: "
                f"{self.used_usd + amount:.4f} > {self.max_usd:.4f}"
            )
        self.used_usd += amount

    def reserve_units(self, units: int) -> None:
        units = max(0, int(units))
        if self.used_units + units > self.max_units:
            raise BudgetExceeded(
                f"[STOP] MAX_SEMRUSH_API_UNITS_PER_RUN would be exceeded: "
                f"{self.used_units + units} > {self.max_units}"
            )
        self.used_units += units

    def settle_usd(self, *, reserved: float, actual: float) -> None:
        reserved = max(0.0, float(reserved))
        actual = max(0.0, float(actual))
        self.used_usd = max(0.0, self.used_usd - reserved)
        self.reserve_usd(actual)


class DataForSEOReviewsClient:
    def __init__(
        self,
        *,
        login: str,
        password: str,
        budget: RunBudget,
        estimated_usd_per_task: float = 0.20,
        timeout: int = 30,
        session: requests.Session | None = None,
    ) -> None:
        if not login or not password:
            raise ValueError("DataForSEO login and password are required")
        self.login = login
        self.password = password
        self.budget = budget
        self.estimated_usd_per_task = max(0.01, float(estimated_usd_per_task))
        self.timeout = timeout
        self.session = session or requests.Session()

    def submit_reviews(self, tasks: Sequence[Mapping[str, Any]], *, depth: int = 60) -> List[Dict[str, Any]]:
        if not tasks:
            return []
        if len(tasks) > 25:
            raise ValueError("Deep review enrichment is capped at 25 leads per run")
        if depth < 10 or depth > 60 or depth % 10:
            raise ValueError("Review depth must be a multiple of 10 from 10 through 60")
        reserved_usd = len(tasks) * self.estimated_usd_per_task
        self.budget.reserve_usd(reserved_usd)
        payload: List[Dict[str, Any]] = []
        for task in tasks:
            place_id = str(task.get("place_id") or "").strip()
            keyword = str(task.get("keyword") or task.get("business_name") or "").strip()
            if not place_id and not keyword:
                raise ValueError("Each review task needs place_id or business_name/keyword")
            item: Dict[str, Any] = {
                "language_code": "en",
                "depth": depth,
                "sort_by": "newest",
                "tag": str(task.get("tag") or place_id or keyword)[:255],
            }
            if place_id:
                item["place_id"] = place_id
            else:
                item["keyword"] = keyword
            location_name = str(task.get("location_name") or "").strip()
            if location_name:
                item["location_name"] = location_name
            payload.append(item)
        response = self.session.post(
            DATAFORSEO_REVIEWS_TASK_POST,
            auth=(self.login, self.password),
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout,
        )
        data = _checked_json(response, source="DataForSEO review task POST")
        normalized_tasks = _dataforseo_tasks(data)
        actual_usd = sum(float(task.get("cost") or 0.0) for task in normalized_tasks)
        self.budget.settle_usd(reserved=reserved_usd, actual=actual_usd or reserved_usd)
        return normalized_tasks

    def get_reviews(self, task_id: str) -> Dict[str, Any]:
        response = self.session.get(
            DATAFORSEO_REVIEWS_TASK_GET.format(task_id=task_id),
            auth=(self.login, self.password),
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
        )
        data = _checked_json(response, source="DataForSEO review task GET")
        tasks = _dataforseo_tasks(data, allow_pending=True)
        if not tasks:
            return {"task_id": task_id, "state": "pending", "items": []}
        task = tasks[0]
        result = task.get("result") if isinstance(task.get("result"), list) else []
        if not result:
            return {
                "task_id": task_id,
                "state": "pending",
                "status_code": task.get("status_code"),
                "status_message": task.get("status_message"),
                "items": [],
            }
        first = result[0] if isinstance(result[0], Mapping) else {}
        items = first.get("items") if isinstance(first.get("items"), list) else []
        return {
            "task_id": task_id,
            "state": "complete",
            "place_id": first.get("place_id") or "",
            "reviews_count": first.get("reviews_count"),
            "items_count": first.get("items_count", len(items)),
            "observed_at": first.get("datetime") or "",
            "items": items,
            "cost_usd": float(task.get("cost") or 0.0),
            "source": "dataforseo_google_reviews_newest",
        }


class SemrushMapRankClient:
    """Read-only direct client for existing Semrush Map Rank Tracker campaigns."""

    def __init__(
        self,
        *,
        token: str,
        budget: RunBudget,
        timeout: int = 30,
        session: requests.Session | None = None,
    ) -> None:
        if not token:
            raise ValueError("Semrush bearer token is required")
        self.token = token
        self.budget = budget
        self.timeout = timeout
        self.session = session or requests.Session()

    def list_campaigns(self, *, query: str = "", size: int = 100) -> Dict[str, Any]:
        self.budget.reserve_units(1)
        return self._get(
            "/campaigns",
            params={"query": query, "size": min(max(1, int(size)), 100)},
        )

    def keyword_metrics(
        self,
        *,
        campaign_id: str,
        keyword_id: str,
        place_id: str,
        cid: str = "",
        query: str = "",
        grid: str = "",
        device: str = "",
        location: str = "",
    ) -> Dict[str, Any]:
        self.budget.reserve_units(1)
        params: Dict[str, Any] = {"keywordId": keyword_id, "placeIds": place_id}
        if cid:
            params["cid"] = cid
        data = self._get(f"/campaigns/{campaign_id}/metrics", params=params)
        payload = data.get("data") if isinstance(data.get("data"), Mapping) else {}
        observed_at = _latest_metric_timestamp(payload)
        context = {
            "campaign_id": campaign_id,
            "keyword_id": keyword_id,
            "place_id": place_id,
            "query": query,
            "grid": grid,
            "device": device,
            "location": location,
        }
        return {
            "source": "semrush_map_rank_tracker",
            "campaign_id": campaign_id,
            "keyword_id": keyword_id,
            "place_id": place_id,
            "query": query,
            "grid": grid,
            "device": device,
            "location": location,
            "observed_at": observed_at,
            "average_positions": payload.get("average_positions") or {},
            "share_of_voices": payload.get("share_of_voices") or {},
            "observation": SignalObservation(
                signal_id=f"semrush-map:{campaign_id}:{keyword_id}:{place_id}",
                kind="map_pack_visibility",
                state="present" if payload else "unknown",
                value=dict(payload) if payload else None,
                source="semrush_map_rank_tracker",
                observed_at=observed_at,
                expires_at=_expires_at(observed_at, 30),
                confidence=0.9 if payload else 0.0,
                metadata=context,
            ).to_dict(),
        }

    def _get(self, path: str, *, params: Mapping[str, Any]) -> Dict[str, Any]:
        response = self.session.get(
            SEMRUSH_MAP_BASE + path,
            headers={"Authorization": f"Bearer {self.token}"},
            params=dict(params),
            timeout=self.timeout,
        )
        return _checked_json(response, source="Semrush Map Rank Tracker")


class PageSpeedClient:
    def __init__(self, *, api_key: str = "", timeout: int = 45, session: requests.Session | None = None) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.session = session or requests.Session()

    def analyze(self, url: str, *, strategy: str = "mobile") -> Dict[str, Any]:
        params: Dict[str, Any] = {"url": url, "strategy": strategy, "category": "performance"}
        if self.api_key:
            params["key"] = self.api_key
        response = self.session.get(PAGESPEED_API, params=params, timeout=self.timeout)
        data = _checked_json(response, source="PageSpeed Insights")
        lighthouse = data.get("lighthouseResult") if isinstance(data.get("lighthouseResult"), Mapping) else {}
        categories = lighthouse.get("categories") if isinstance(lighthouse.get("categories"), Mapping) else {}
        performance = categories.get("performance") if isinstance(categories.get("performance"), Mapping) else {}
        loading = data.get("loadingExperience") if isinstance(data.get("loadingExperience"), Mapping) else {}
        observed_at = str(lighthouse.get("fetchTime") or "")
        return {
            "source": "pagespeed_insights_v5",
            "url": url,
            "strategy": strategy,
            "performance_score": performance.get("score"),
            "field_overall_category": loading.get("overall_category") or "unknown",
            "observed_at": observed_at,
            "observation": SignalObservation(
                signal_id=f"pagespeed:{strategy}:{url}",
                kind="website_performance",
                state="present" if lighthouse else "unknown",
                value={
                    "performance_score": performance.get("score"),
                    "field_overall_category": loading.get("overall_category") or "unknown",
                }
                if lighthouse
                else None,
                source="pagespeed_insights_v5",
                observed_at=observed_at,
                expires_at=_expires_at(observed_at, 30),
                confidence=0.9 if lighthouse else 0.0,
                metadata={"url": url, "strategy": strategy},
            ).to_dict(),
        }


def select_deep_enrichment(rows: Sequence[Mapping[str, Any]], *, limit: int = 25, reject_sample: int = 5) -> List[Mapping[str, Any]]:
    limit = min(25, max(0, int(limit)))
    reject_sample = min(max(0, int(reject_sample)), limit)
    primary_limit = limit - reject_sample
    primary = [row for row in rows if str(row.get("Scoring V3 Disposition") or "") != "reject"]
    primary.sort(key=lambda row: (-_number(row.get("Scoring V3 Rank")), _stable_row_key(row)))
    rejects = [row for row in rows if str(row.get("Scoring V3 Disposition") or "") == "reject"]
    rejects.sort(key=_stable_hash)
    selected = primary[:primary_limit]
    used = {_stable_row_key(row) for row in selected}
    selected.extend(row for row in rejects[:reject_sample] if _stable_row_key(row) not in used)
    if len(selected) < limit:
        remaining = [row for row in primary[primary_limit:] if _stable_row_key(row) not in used]
        selected.extend(remaining[: limit - len(selected)])
    return selected


def _dataforseo_tasks(data: Mapping[str, Any], *, allow_pending: bool = False) -> List[Dict[str, Any]]:
    status_code = int(data.get("status_code") or 0)
    if status_code and status_code != 20000:
        raise ExternalSignalError(
            f"DataForSEO status {status_code}: {data.get('status_message') or 'unknown error'}"
        )
    tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
    normalized = [dict(task) for task in tasks if isinstance(task, Mapping)]
    if not allow_pending:
        failed = [task for task in normalized if int(task.get("status_code") or 0) >= 40000]
        if failed:
            first = failed[0]
            raise ExternalSignalError(
                f"DataForSEO task status {first.get('status_code')}: {first.get('status_message')}"
            )
    return normalized


def _checked_json(response: requests.Response, *, source: str) -> Dict[str, Any]:
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ExternalSignalError(f"{source} HTTP failure: {exc}") from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise ExternalSignalError(f"{source} returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise ExternalSignalError(f"{source} returned a non-object payload")
    return data


def _stable_row_key(row: Mapping[str, Any]) -> str:
    return str(
        row.get("Place Key")
        or row.get("place_id")
        or row.get("Business Name")
        or row.get("business_name")
        or ""
    )


def _stable_hash(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(_stable_row_key(row).encode("utf-8")).hexdigest()


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return -1.0


def _latest_metric_timestamp(payload: Mapping[str, Any]) -> str:
    timestamps: List[str] = []
    for key in ("average_positions", "share_of_voices"):
        values = payload.get(key)
        if isinstance(values, Mapping):
            timestamps.extend(str(value) for value in values if str(value).strip())
    return max(timestamps) if timestamps else ""


def _expires_at(observed_at: str, days: int) -> str:
    text = str(observed_at or "").strip().replace("Z", "+00:00")
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed.astimezone(timezone.utc) + timedelta(days=days)).isoformat().replace("+00:00", "Z")
