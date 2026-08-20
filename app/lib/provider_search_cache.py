"""Disk-backed per-provider subtitle search cache."""
import hashlib
import json
import os
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from ..providers.base import SubtitleResult

DEFAULT_TTL_SECONDS = 21600


def _cache_dir() -> str:
    base = os.environ.get("PROVIDER_SEARCH_CACHE_DIR", "/app/data/search-cache")
    os.makedirs(base, exist_ok=True)
    return base


def _ttl_seconds() -> int:
    raw = os.environ.get("PROVIDER_SEARCH_CACHE_TTL_SECONDS", str(DEFAULT_TTL_SECONDS))
    try:
        return max(60, int(raw))
    except ValueError:
        return DEFAULT_TTL_SECONDS


def make_cache_key(
    content_type: str,
    content_id: str,
    video_hash: Optional[str],
    video_size: Optional[int],
    video_filename: Optional[str],
    languages: List[str],
) -> str:
    payload = "|".join([
        content_type or "",
        content_id or "",
        video_hash or "",
        str(video_size) if video_size is not None else "",
        video_filename or "",
        ",".join(sorted(languages or [])),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_path(cache_key: str) -> str:
    return os.path.join(_cache_dir(), f"{cache_key}.json")


def _result_to_dict(result: SubtitleResult) -> dict:
    data = asdict(result)
    return data


def _dict_to_result(data: dict) -> SubtitleResult:
    return SubtitleResult(**data)


def load_provider_cache(cache_key: str) -> Dict[str, dict]:
    path = _cache_path(cache_key)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            return raw
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def get_cached_results(
    cache_key: str,
    provider_names: List[str],
) -> tuple[Dict[str, List[SubtitleResult]], List[str]]:
    """Return cached results and provider names that still need a live search."""
    now = time.time()
    stored = load_provider_cache(cache_key)
    cached: Dict[str, List[SubtitleResult]] = {}
    missing: List[str] = []

    for name in provider_names:
        entry = stored.get(name)
        if not entry:
            missing.append(name)
            continue
        expires_at = entry.get("expires_at", 0)
        if expires_at < now:
            missing.append(name)
            continue
        results_raw = entry.get("results") or []
        cached[name] = [_dict_to_result(item) for item in results_raw]

    return cached, missing


def merge_and_store(
    cache_key: str,
    existing: Dict[str, List[SubtitleResult]],
    fresh: Dict[str, List[SubtitleResult]],
) -> Dict[str, List[SubtitleResult]]:
    now = time.time()
    ttl = _ttl_seconds()
    stored = load_provider_cache(cache_key)

    merged: Dict[str, List[SubtitleResult]] = dict(existing)
    merged.update(fresh)

    for name, results in fresh.items():
        stored[name] = {
            "expires_at": now + ttl,
            "results": [_result_to_dict(r) for r in results],
        }

    # Keep still-valid entries from disk that were not refreshed.
    for name, entry in stored.items():
        if name in fresh:
            continue
        if entry.get("expires_at", 0) >= now and name not in merged:
            merged[name] = [_dict_to_result(item) for item in (entry.get("results") or [])]

    path = _cache_path(cache_key)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stored, f)
    os.replace(tmp, path)
    return merged
