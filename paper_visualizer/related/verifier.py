"""External bibliographic verification with bounded retries and safe caching."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from ..artifacts import atomic_write_json
from .planner import validate_related_work_plan


class VerificationError(RuntimeError):
    """Raised when a provider cannot perform a requested verification."""


def _digest(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def apply_related_review_overlay(
    plan: Mapping[str, Any],
    overlay: Mapping[str, Any],
    *,
    project_root: Path,
    ir: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply a hash-bound human metadata review without changing citation context."""

    allowed = {"approval", "current_paper", "nodes", "include_node_ids"}
    unknown = set(overlay) - allowed
    if unknown:
        raise VerificationError(f"unsupported related review overlay fields: {sorted(unknown)}")
    approval = overlay.get("approval", {})
    if not (
        approval.get("kind") == "human_review"
        and approval.get("status") == "approved"
        and approval.get("base_ir_sha256") == plan.get("source_ir_sha256")
        and approval.get("base_plan_sha256") == _digest(plan)
        and str(approval.get("reviewer") or "").strip()
        and str(approval.get("reason") or "").strip()
    ):
        raise VerificationError("related review overlay requires approved, hash-bound human review")

    result = copy.deepcopy(plan)
    include = set(str(item) for item in overlay.get("include_node_ids", []))
    if "include_node_ids" in overlay:
        known = {str(item["id"]) for item in result.get("nodes", [])}
        if not include <= known:
            raise VerificationError("related review overlay selects an unknown node")
        result["nodes"] = [item for item in result["nodes"] if item["id"] in include]
        result["clusters"] = [
            {**cluster, "member_node_ids": [item for item in cluster["member_node_ids"] if item in include]}
            for cluster in result.get("clusters", [])
        ]
        result["clusters"] = [item for item in result["clusters"] if item["member_node_ids"]]
        cluster_ids = {item["id"] for item in result["clusters"]}
        result["edges"] = [
            item for item in result.get("edges", [])
            if item.get("target_id") in include
            and (item.get("source_id") == result.get("paper_id") or item.get("source_id") in include)
            and (not item.get("cluster_id") or item.get("cluster_id") in cluster_ids)
        ]

    patches = {str(item.get("id")): item for item in overlay.get("nodes", [])}
    for node in result.get("nodes", []):
        patch = patches.pop(str(node["id"]), None)
        if patch is None:
            continue
        if set(patch) - {"id", "metadata", "verification"}:
            raise VerificationError("related node overlay may change metadata and verification only")
        node["metadata"].update(copy.deepcopy(dict(patch.get("metadata", {}))))
        node["verification"] = copy.deepcopy(dict(patch.get("verification", node["verification"])))
    if patches:
        raise VerificationError("related node overlay contains an unknown or excluded node")

    current_patch = overlay.get("current_paper")
    if current_patch:
        if set(current_patch) - {"metadata", "verification"}:
            raise VerificationError("current-paper overlay may change metadata and verification only")
        result["current_paper"]["metadata"].update(copy.deepcopy(dict(current_patch.get("metadata", {}))))
        result["current_paper"]["verification"] = copy.deepcopy(dict(current_patch.get("verification", result["current_paper"]["verification"])))

    unresolved = [
        item["id"] for item in [result["current_paper"], *result.get("nodes", [])]
        if item.get("verification", {}).get("status") != "verified"
        or not str(item.get("metadata", {}).get("url") or "").startswith(("http://", "https://"))
    ]
    result["review"] = {
        "status": "needs_review" if unresolved else "passed",
        "warnings": list(result.get("review", {}).get("warnings", [])),
        "needs_human_review": [{"id": item, "reason": "metadata verification incomplete"} for item in unresolved],
    }
    errors = validate_related_work_plan(ir, result, project_root)
    if errors:
        raise VerificationError("reviewed Related Work plan failed validation: " + "; ".join(errors[:5]))
    return result


class MetadataProvider(Protocol):
    name: str
    unit_cost_cny: float

    def search(self, title: str) -> list[dict[str, Any]]: ...


def _norm(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _author_names(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        name = item.get("name") if isinstance(item, Mapping) else item
        if str(name or "").strip():
            result.append(str(name).strip())
    return result


class AMinerTitleSearch:
    """Free AMiner title lookup; the token is read only from the environment."""

    name = "aminer.paper_search"
    unit_cost_cny = 0.0
    endpoint = "https://datacenter.aminer.cn/gateway/open_platform/api/paper/search"

    def __init__(self, *, token_env: str = "AMINER_API_KEY", timeout: int = 20) -> None:
        self.token_env = token_env
        self.timeout = timeout

    def search(self, title: str) -> list[dict[str, Any]]:
        token = os.environ.get(self.token_env, "").strip()
        if not token:
            raise VerificationError(f"{self.token_env} is not set")
        query = urllib.parse.urlencode({"title": title, "page": 1, "size": 10})
        request = urllib.request.Request(
            f"{self.endpoint}?{query}",
            headers={"Authorization": token, "X-Platform": "openclaw"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if isinstance(data, Mapping):
            data = data.get("items", [])
        return [dict(item) for item in (data or []) if isinstance(item, Mapping)]


def _cache_path(cache_dir: Path, provider: MetadataProvider, title: str) -> Path:
    key = hashlib.sha256(f"{provider.name}\0{_norm(title)}".encode("utf-8")).hexdigest()
    return cache_dir / provider.name.replace("/", "_").replace(".", "_") / f"{key}.json"


def _search_cached(provider: MetadataProvider, title: str, cache_dir: Path | None, max_attempts: int) -> tuple[list[dict[str, Any]], bool, int]:
    path = _cache_path(cache_dir, provider, title) if cache_dir else None
    if path and path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        return list(payload.get("records", [])), True, 0
    last_error: Exception | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            records = provider.search(title)
            if path:
                atomic_write_json(path, {"provider": provider.name, "query_sha256": hashlib.sha256(title.encode("utf-8")).hexdigest(), "records": records})
            return records, False, 1
        except (OSError, ValueError, urllib.error.URLError) as error:
            last_error = error
            if attempt < max_attempts:
                time.sleep(min(0.1 * (2 ** (attempt - 1)), 0.8))
    raise VerificationError(f"metadata lookup failed after {max_attempts} attempts: {last_error}")


def _best_match(title: str, year: int | None, records: list[dict[str, Any]]) -> dict[str, Any] | None:
    wanted = _norm(title)
    exact = [row for row in records if _norm(row.get("title")) == wanted]
    candidates = exact or [row for row in records if wanted and (wanted in _norm(row.get("title")) or _norm(row.get("title")) in wanted)]
    if year is not None:
        year_match = [row for row in candidates if row.get("year") in {None, year}]
        candidates = year_match or candidates
    return candidates[0] if candidates else None


def _apply_record(target: dict[str, Any], record: Mapping[str, Any], provider: MetadataProvider) -> None:
    metadata = target["metadata"]
    conflicts: list[str] = []
    checked: list[str] = []
    record_title = str(record.get("title") or "").strip()
    if record_title:
        checked.append("title")
        if metadata.get("title") and _norm(metadata["title"]) != _norm(record_title):
            conflicts.append("title")
        metadata["title"] = record_title
    full_authors = _author_names(record.get("authors"))
    authors = full_authors or ([str(record["first_author"])] if record.get("first_author") else [])
    if authors:
        checked.append("authors")
        if metadata.get("authors") and _norm(metadata["authors"][0]) != _norm(authors[0]):
            conflicts.append("authors")
        # Free search cards expose only the first author.  That is sufficient
        # to verify paper identity together with exact title and year, but it
        # must not replace the fuller author list parsed from the bibliography.
        if full_authors or not metadata.get("authors"):
            metadata["authors"] = authors
    if record.get("year") is not None:
        checked.append("year")
        if metadata.get("year") is not None and int(metadata["year"]) != int(record["year"]):
            conflicts.append("year")
        metadata["year"] = int(record["year"])
    record_id = str(record.get("id") or record.get("record_id") or "").strip() or None
    provider_url = f"https://www.aminer.cn/pub/{record_id}" if record_id and provider.name.startswith("aminer") else ""
    url = str(record.get("url") or provider_url).strip() or None
    if url:
        checked.append("url")
        metadata["url"] = url
    if record.get("doi"):
        metadata["doi"] = str(record["doi"])
    venue = record.get("venue_name")
    if not venue and isinstance(record.get("venue"), Mapping):
        venue = (record.get("venue") or {}).get("raw")
    if venue:
        metadata["venue"] = str(venue)
    complete = all(field in checked for field in ("title", "authors", "year", "url"))
    status = "conflict" if conflicts else ("verified" if complete else "partial")
    target["verification"] = {
        "status": status,
        "checked_fields": list(dict.fromkeys(checked)),
        "conflicts": conflicts,
        "sources": [{"provider": provider.name, "record_id": record_id, "url": url, "retrieved_at": None}],
    }


def verify_related_work(
    plan: Mapping[str, Any],
    provider: MetadataProvider,
    *,
    cache_dir: Path | None = None,
    max_attempts: int = 3,
    max_queries: int = 20,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify current paper and cited nodes without changing descriptions/edges."""

    result = copy.deepcopy(plan)
    targets = [result["current_paper"], *result.get("nodes", [])]
    api_calls = 0
    cache_hits = 0
    warnings = list(result.get("review", {}).get("warnings", []))
    for target in targets[: max(0, max_queries)]:
        title = str(target.get("metadata", {}).get("title") or "").strip()
        if not title:
            warnings.append(f"{target.get('id')}: no title available for metadata verification")
            continue
        try:
            records, cached, calls = _search_cached(provider, title, cache_dir, max_attempts)
        except VerificationError as error:
            warnings.append(f"{target.get('id')}: {error}")
            continue
        api_calls += calls
        cache_hits += int(cached)
        match = _best_match(title, target.get("metadata", {}).get("year"), records)
        if match is None:
            warnings.append(f"{target.get('id')}: no unambiguous metadata match")
            continue
        original_description = copy.deepcopy(target.get("description"))
        _apply_record(target, match, provider)
        if original_description is not None and target.get("description") != original_description:
            raise VerificationError("external provider modified a current-paper description")
    unresolved = [target["id"] for target in targets if target["verification"]["status"] != "verified"]
    result["review"] = {
        "status": "needs_review" if unresolved else "passed",
        "warnings": list(dict.fromkeys(warnings)),
        "needs_human_review": [{"id": item, "reason": "metadata verification incomplete"} for item in unresolved],
    }
    cost = round(api_calls * float(provider.unit_cost_cny), 2)
    return result, {"provider": provider.name, "unit_cost_cny": provider.unit_cost_cny, "api_calls": api_calls, "cache_hits": cache_hits, "total_cost_cny": cost}
