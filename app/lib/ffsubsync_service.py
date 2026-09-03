"""Subtitle-to-subtitle sync via ffsubsync (English reference -> Spanish input)."""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
from typing import Any, Dict, Optional, Tuple

from pysubs2 import SSAFile, load

DEFAULT_MIN_FILENAME_SCORE = 0.5
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_CACHE_TTL_SECONDS = 21600


def min_filename_score() -> float:
    raw = os.environ.get("FFSUBSYNC_MIN_FILENAME_SCORE", str(DEFAULT_MIN_FILENAME_SCORE))
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_MIN_FILENAME_SCORE


def ffsubsync_timeout_seconds() -> int:
    raw = os.environ.get("FFSUBSYNC_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    try:
        return max(5, int(raw))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def sync_cache_ttl_seconds() -> int:
    raw = os.environ.get(
        "FFSUBSYNC_CACHE_TTL_SECONDS",
        os.environ.get("PROVIDER_SEARCH_CACHE_TTL_SECONDS", str(DEFAULT_CACHE_TTL_SECONDS)),
    )
    try:
        return max(60, int(raw))
    except ValueError:
        return DEFAULT_CACHE_TTL_SECONDS


def sync_cache_dir() -> str:
    base = os.environ.get("FFSUBSYNC_CACHE_DIR", "/app/subtitles/synced")
    os.makedirs(base, exist_ok=True)
    return base


def should_ffsubsync(sync_meta: Optional[Dict[str, Any]]) -> bool:
    if not sync_meta:
        return False
    match_kind = sync_meta.get("match_kind")
    if match_kind in {"hash", "local_hash"}:
        return False
    score = sync_meta.get("filename_score")
    if match_kind == "filename" and score is not None and score >= min_filename_score():
        return False
    return bool(
        sync_meta.get("eng_provider")
        and sync_meta.get("eng_id")
        and sync_meta.get("spa_provider")
        and sync_meta.get("spa_id")
    )


def make_sync_cache_key(context: Dict[str, Any], sync_meta: Dict[str, Any]) -> str:
    payload = "|".join(
        [
            context.get("content_id") or "",
            context.get("v_hash") or "",
            str(context.get("v_size") if context.get("v_size") is not None else ""),
            context.get("v_fname") or "",
            sync_meta.get("spa_provider") or "",
            sync_meta.get("spa_id") or "",
            sync_meta.get("eng_provider") or "",
            sync_meta.get("eng_id") or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_file_path(cache_key: str) -> str:
    return os.path.join(sync_cache_dir(), f"{cache_key}.vtt")


def read_cached_vtt(cache_key: str) -> Optional[str]:
    path = _cache_file_path(cache_key)
    if not os.path.exists(path):
        return None
    age = time.time() - os.path.getmtime(path)
    if age > sync_cache_ttl_seconds():
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def write_cached_vtt(cache_key: str, vtt_content: str) -> None:
    path = _cache_file_path(cache_key)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(vtt_content)


def subtitle_bytes_to_srt(data: bytes, extension: str) -> str:
    ext = (extension or ".srt").lower()
    if not ext.startswith("."):
        ext = f".{ext}"
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_in:
        temp_in.write(data)
        temp_in_path = temp_in.name
    try:
        subs = load(temp_in_path)
        if not isinstance(subs, SSAFile):
            subs = SSAFile.load_from_string(subs.to_string("srt"))
        return subs.to_string("srt")
    finally:
        try:
            os.remove(temp_in_path)
        except OSError:
            pass


def run_ffsubsync(reference_srt: str, input_srt: str, output_srt: str) -> bool:
    cmd = [
        "ffsubsync",
        reference_srt,
        "-i",
        input_srt,
        "-o",
        output_srt,
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=ffsubsync_timeout_seconds(),
            check=False,
        )
        return completed.returncode == 0 and os.path.exists(output_srt)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def srt_to_vtt(srt_content: str) -> str:
    subs = SSAFile.from_string(srt_content)
    return subs.to_string("vtt")


def sync_spanish_with_english_reference(reference_srt: str, spanish_srt: str) -> Optional[str]:
    with tempfile.TemporaryDirectory(prefix="ffsubsync-") as workdir:
        reference_path = os.path.join(workdir, "reference.srt")
        input_path = os.path.join(workdir, "input.srt")
        output_path = os.path.join(workdir, "output.srt")
        with open(reference_path, "w", encoding="utf-8") as handle:
            handle.write(reference_srt)
        with open(input_path, "w", encoding="utf-8") as handle:
            handle.write(spanish_srt)
        if not run_ffsubsync(reference_path, input_path, output_path):
            return None
        with open(output_path, "r", encoding="utf-8") as handle:
            synced_srt = handle.read()
        return srt_to_vtt(synced_srt)


def build_sync_metadata(active_info: Dict[str, Any], english_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    metadata = {
        "match_kind": active_info.get("match_kind"),
        "filename_score": active_info.get("filename_score"),
        "spa_provider": active_info.get("provider_name"),
        "spa_id": active_info.get("provider_subtitle_id"),
    }
    if english_info and english_info.get("type") != "none":
        metadata["eng_provider"] = english_info.get("provider_name")
        metadata["eng_id"] = english_info.get("provider_subtitle_id")
    return metadata


async def maybe_apply_ffsubsync(user, vtt_content: str, context: Dict[str, Any], sync_meta: Dict[str, Any], episode=None) -> Optional[str]:
    from ..lib.subtitles import normalize_vtt_for_players

    if not should_ffsubsync(sync_meta):
        return None

    cache_key = make_sync_cache_key(context, sync_meta)
    cached = read_cached_vtt(cache_key)
    if cached:
        return cached

    from ..routes.utils import download_provider_subtitle_bytes

    eng_provider = sync_meta.get("eng_provider")
    eng_id = sync_meta.get("eng_id")
    if not eng_provider or not eng_id:
        return None

    try:
        eng_bytes, eng_ext = await download_provider_subtitle_bytes(user, eng_provider, eng_id, episode=episode)
        reference_srt = subtitle_bytes_to_srt(eng_bytes, eng_ext)
        spanish_srt = SSAFile.from_string(vtt_content).to_string("srt")
        synced_vtt = sync_spanish_with_english_reference(reference_srt, spanish_srt)
        if not synced_vtt:
            return None
        synced_vtt = normalize_vtt_for_players(synced_vtt)
        write_cached_vtt(cache_key, synced_vtt)
        return synced_vtt
    except Exception:
        return None
