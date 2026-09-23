"""Embedded reference index + listing sidecar for ffsubsync (no extra provider searches)."""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional

EMBEDDED_PROVIDER = "embedded"


def sync_cache_ttl_seconds() -> int:
    raw = os.environ.get(
        "FFSUBSYNC_CACHE_TTL_SECONDS",
        os.environ.get("PROVIDER_SEARCH_CACHE_TTL_SECONDS", "21600"),
    )
    try:
        return max(60, int(raw))
    except ValueError:
        return 21600


def embedded_index_dir() -> str:
    base = os.environ.get("FFSUBSYNC_EMBEDDED_INDEX_DIR", "/app/subtitles/embedded_index")
    os.makedirs(base, exist_ok=True)
    return base


def listing_sidecar_dir() -> str:
    base = os.environ.get("FFSUBSYNC_LISTING_SIDECAR_DIR", "/app/subtitles/listing_sidecar")
    os.makedirs(base, exist_ok=True)
    return base


def embedded_identity_key(
    content_id: str,
    video_hash: Optional[str],
    video_size: Optional[int],
    video_filename: Optional[str],
) -> str:
    payload = "|".join(
        [
            content_id or "",
            video_hash or "",
            str(video_size if video_size is not None else ""),
            video_filename or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def listing_search_languages(preferred_langs: List[str], embedded_reference: Optional[Dict[str, Any]]) -> List[str]:
    """Do not append English for ffsubsync when listing; client attaches embedded reference separately."""
    return list(preferred_langs)


def save_embedded_index(
    content_id: str,
    video_hash: Optional[str],
    video_size: Optional[int],
    video_filename: Optional[str],
    embedded_meta: Dict[str, Any],
) -> None:
    key = embedded_identity_key(content_id, video_hash, video_size, video_filename)
    path = os.path.join(embedded_index_dir(), f"{key}.json")
    payload = {
        "saved_at": time.time(),
        "content_id": content_id,
        "video_hash": video_hash,
        "video_size": video_size,
        "video_filename": video_filename,
        "embedded": embedded_meta,
    }
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp, path)


def lookup_embedded_index(
    content_id: str,
    video_hash: Optional[str],
    video_size: Optional[int],
    video_filename: Optional[str],
) -> Optional[Dict[str, Any]]:
    key = embedded_identity_key(content_id, video_hash, video_size, video_filename)
    path = os.path.join(embedded_index_dir(), f"{key}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    saved_at = payload.get("saved_at", 0)
    if time.time() - saved_at > sync_cache_ttl_seconds():
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    embedded = payload.get("embedded")
    if not isinstance(embedded, dict):
        return None
    return embedded


def save_listing_sidecar(
    content_id: str,
    video_hash: Optional[str],
    video_size: Optional[int],
    video_filename: Optional[str],
    spa_info: Dict[str, Any],
    download_context: Dict[str, Any],
    sync_meta: Dict[str, Any],
) -> None:
    key = embedded_identity_key(content_id, video_hash, video_size, video_filename)
    path = os.path.join(listing_sidecar_dir(), f"{key}.json")
    payload = {
        "saved_at": time.time(),
        "spa_info": spa_info,
        "download_context": download_context,
        "sync_meta": sync_meta,
    }
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp, path)


def load_listing_sidecar(
    content_id: str,
    video_hash: Optional[str],
    video_size: Optional[int],
    video_filename: Optional[str],
) -> Optional[Dict[str, Any]]:
    key = embedded_identity_key(content_id, video_hash, video_size, video_filename)
    path = os.path.join(listing_sidecar_dir(), f"{key}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    saved_at = payload.get("saved_at", 0)
    if time.time() - saved_at > sync_cache_ttl_seconds():
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    return payload


def embedded_meta_to_english_reference(embedded_meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": EMBEDDED_PROVIDER,
        "provider_name": EMBEDDED_PROVIDER,
        "provider_subtitle_id": embedded_meta.get("provider_subtitle_id"),
        "ext": embedded_meta.get("ext"),
        "lang": embedded_meta.get("lang"),
    }


def resolve_sync_english_reference(
    content_id: str,
    video_hash: Optional[str],
    video_size: Optional[int],
    video_filename: Optional[str],
    token_sync_meta: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    indexed = lookup_embedded_index(content_id, video_hash, video_size, video_filename)
    if indexed:
        return embedded_meta_to_english_reference(indexed)
    if not token_sync_meta:
        return None
    eng_provider = token_sync_meta.get("eng_provider")
    eng_id = token_sync_meta.get("eng_id")
    if not eng_provider or not eng_id:
        return None
    if eng_provider == EMBEDDED_PROVIDER:
        return embedded_meta_to_english_reference(
            {
                "provider_subtitle_id": eng_id,
                "ext": ".srt",
            }
        )
    return {
        "type": "provider",
        "provider_name": eng_provider,
        "provider_subtitle_id": eng_id,
    }
