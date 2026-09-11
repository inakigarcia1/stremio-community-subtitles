"""Subtitle-to-subtitle sync via ffsubsync (English reference -> Spanish input)."""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import tempfile
import time
from typing import Any, Dict, Optional, Tuple

from pysubs2 import SSAFile, load

logger = logging.getLogger(__name__)
LOG_PREFIX = "[ffsubsync]"

DEFAULT_MIN_FILENAME_SCORE = 0.5
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_CACHE_TTL_SECONDS = 21600
EMBEDDED_PROVIDER = "embedded"
MAX_EMBEDDED_REFERENCE_BYTES = 5 * 1024 * 1024
ALLOWED_REFERENCE_EXTENSIONS = {".srt", ".vtt", ".ass", ".ssa"}


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


def embedded_reference_dir() -> str:
    base = os.environ.get("FFSUBSYNC_EMBEDDED_DIR", "/app/subtitles/embedded")
    os.makedirs(base, exist_ok=True)
    return base


def reference_extension(filename: Optional[str]) -> str:
    if not filename:
        return ".srt"
    ext = os.path.splitext(filename)[1].lower()
    if ext in ALLOWED_REFERENCE_EXTENSIONS:
        return ext
    return ".srt"


def save_embedded_reference(data: bytes, filename: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not data or len(data) > MAX_EMBEDDED_REFERENCE_BYTES:
        logger.warning(
            "%s Rejected embedded reference (bytes=%d max=%d)",
            LOG_PREFIX,
            0 if not data else len(data),
            MAX_EMBEDDED_REFERENCE_BYTES,
        )
        return None
    digest = hashlib.sha256(data).hexdigest()
    ext = reference_extension(filename)
    path = os.path.join(embedded_reference_dir(), f"{digest}{ext}")
    if not os.path.exists(path):
        with open(path, "wb") as handle:
            handle.write(data)
    logger.info(
        "%s Stored embedded reference hash=%s ext=%s bytes=%d",
        LOG_PREFIX,
        digest,
        ext,
        len(data),
    )
    return {
        "type": EMBEDDED_PROVIDER,
        "provider_name": EMBEDDED_PROVIDER,
        "provider_subtitle_id": digest,
        "ext": ext,
    }


def _is_sha256_hex(value: str) -> bool:
    if len(value) != 64:
        return False
    return all(char in "0123456789abcdef" for char in value.lower())


def read_embedded_reference_bytes(file_hash: str) -> Optional[Tuple[bytes, str]]:
    if not file_hash or not _is_sha256_hex(file_hash):
        return None
    digest = file_hash.lower()
    ttl = sync_cache_ttl_seconds()
    for ext in ALLOWED_REFERENCE_EXTENSIONS:
        path = os.path.join(embedded_reference_dir(), f"{digest}{ext}")
        if not os.path.isfile(path):
            continue
        age = time.time() - os.path.getmtime(path)
        if age > ttl:
            try:
                os.remove(path)
            except OSError:
                pass
            logger.info(
                "%s Embedded reference expired (age=%.0fs ttl=%ds) hash=%s",
                LOG_PREFIX,
                age,
                ttl,
                digest,
            )
            continue
        with open(path, "rb") as handle:
            return handle.read(), ext
    return None


def ffsubsync_force_always() -> bool:
    """When true, run ffsubsync whenever ENG+SPA refs exist (local testing only)."""
    return os.environ.get("FFSUBSYNC_FORCE_ALWAYS", "").strip().lower() in {"1", "true", "yes"}


def ffsubsync_skip_reason(sync_meta: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return human-readable skip reason, or None if ffsubsync should run."""
    if not sync_meta:
        return "no sync metadata"
    if not sync_meta.get("eng_provider") or not sync_meta.get("eng_id"):
        return "missing English reference (eng_provider/eng_id)"
    if not sync_meta.get("spa_provider") or not sync_meta.get("spa_id"):
        return "missing Spanish subtitle (spa_provider/spa_id)"
    if ffsubsync_force_always():
        return None
    match_kind = sync_meta.get("match_kind")
    if match_kind in {"hash", "local_hash"}:
        return f"match_kind={match_kind} (already hash-aligned)"
    score = sync_meta.get("filename_score")
    if match_kind == "filename" and score is not None and score >= min_filename_score():
        return f"filename_score={score:.4f} >= threshold={min_filename_score()}"
    return None


def should_ffsubsync(sync_meta: Optional[Dict[str, Any]]) -> bool:
    return ffsubsync_skip_reason(sync_meta) is None


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
        logger.info(
            "%s Cache entry expired (age=%.0fs ttl=%ds) cache_key=%s",
            LOG_PREFIX,
            age,
            sync_cache_ttl_seconds(),
            cache_key,
        )
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
    start = time.perf_counter()
    logger.info(
        "%s Starting CLI: %s (timeout=%ds)",
        LOG_PREFIX,
        " ".join(cmd),
        ffsubsync_timeout_seconds(),
    )
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=ffsubsync_timeout_seconds(),
            check=False,
        )
        elapsed = time.perf_counter() - start
        if completed.returncode == 0 and os.path.exists(output_srt):
            output_size = os.path.getsize(output_srt)
            logger.info(
                "%s CLI finished successfully in %.3fs (output_size=%d bytes)",
                LOG_PREFIX,
                elapsed,
                output_size,
            )
            return True
        logger.warning(
            "%s CLI failed in %.3fs returncode=%s output_exists=%s stderr=%r stdout=%r",
            LOG_PREFIX,
            elapsed,
            completed.returncode,
            os.path.exists(output_srt),
            (completed.stderr or "").strip()[:500],
            (completed.stdout or "").strip()[:500],
        )
        return False
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - start
        logger.warning(
            "%s CLI timed out after %.3fs (limit=%ds)",
            LOG_PREFIX,
            elapsed,
            ffsubsync_timeout_seconds(),
        )
        return False
    except FileNotFoundError:
        logger.error("%s ffsubsync binary not found in PATH", LOG_PREFIX)
        return False
    except OSError as exc:
        logger.error("%s CLI OS error: %s", LOG_PREFIX, exc)
        return False


def srt_to_vtt(srt_content: str) -> str:
    subs = SSAFile.from_string(srt_content)
    return subs.to_string("vtt")


def sync_spanish_with_english_reference(reference_srt: str, spanish_srt: str) -> Optional[str]:
    ref_lines = reference_srt.count("\n") + 1 if reference_srt else 0
    spa_lines = spanish_srt.count("\n") + 1 if spanish_srt else 0
    start = time.perf_counter()
    logger.info(
        "%s sync_spanish_with_english_reference started (ref_srt_lines=%d spa_srt_lines=%d ref_bytes=%d spa_bytes=%d)",
        LOG_PREFIX,
        ref_lines,
        spa_lines,
        len(reference_srt.encode("utf-8")),
        len(spanish_srt.encode("utf-8")),
    )
    with tempfile.TemporaryDirectory(prefix="ffsubsync-") as workdir:
        reference_path = os.path.join(workdir, "reference.srt")
        input_path = os.path.join(workdir, "input.srt")
        output_path = os.path.join(workdir, "output.srt")
        with open(reference_path, "w", encoding="utf-8") as handle:
            handle.write(reference_srt)
        with open(input_path, "w", encoding="utf-8") as handle:
            handle.write(spanish_srt)
        if not run_ffsubsync(reference_path, input_path, output_path):
            elapsed = time.perf_counter() - start
            logger.warning("%s sync_spanish_with_english_reference failed in %.3fs", LOG_PREFIX, elapsed)
            return None
        with open(output_path, "r", encoding="utf-8") as handle:
            synced_srt = handle.read()
        synced_vtt = srt_to_vtt(synced_srt)
        elapsed = time.perf_counter() - start
        logger.info(
            "%s sync_spanish_with_english_reference finished in %.3fs (output_vtt_bytes=%d)",
            LOG_PREFIX,
            elapsed,
            len(synced_vtt.encode("utf-8")),
        )
        return synced_vtt


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

    content_id = context.get("content_id", "?")
    if ffsubsync_force_always():
        logger.warning(
            "%s FFSUBSYNC_FORCE_ALWAYS enabled — skipping score/hash gates for content_id=%s",
            LOG_PREFIX,
            content_id,
        )
    skip_reason = ffsubsync_skip_reason(sync_meta)
    if skip_reason:
        logger.info(
            "%s Skipped content_id=%s reason=%s match_kind=%s filename_score=%s "
            "spa=%s/%s eng=%s/%s",
            LOG_PREFIX,
            content_id,
            skip_reason,
            sync_meta.get("match_kind"),
            sync_meta.get("filename_score"),
            sync_meta.get("spa_provider"),
            sync_meta.get("spa_id"),
            sync_meta.get("eng_provider"),
            sync_meta.get("eng_id"),
        )
        return None

    total_start = time.perf_counter()
    logger.info(
        "%s Triggered content_id=%s match_kind=%s filename_score=%s "
        "spa_ref=%s/%s eng_ref=%s/%s input_vtt_bytes=%d",
        LOG_PREFIX,
        content_id,
        sync_meta.get("match_kind"),
        sync_meta.get("filename_score"),
        sync_meta.get("spa_provider"),
        sync_meta.get("spa_id"),
        sync_meta.get("eng_provider"),
        sync_meta.get("eng_id"),
        len(vtt_content.encode("utf-8")),
    )

    cache_key = make_sync_cache_key(context, sync_meta)
    cached = read_cached_vtt(cache_key)
    if cached:
        total_elapsed = time.perf_counter() - total_start
        logger.info(
            "%s Cache HIT content_id=%s cache_key=%s cached_bytes=%d lookup_time=%.3fs",
            LOG_PREFIX,
            content_id,
            cache_key,
            len(cached.encode("utf-8")),
            total_elapsed,
        )
        return cached

    logger.info("%s Cache MISS content_id=%s cache_key=%s", LOG_PREFIX, content_id, cache_key)

    eng_provider = sync_meta.get("eng_provider")
    eng_id = sync_meta.get("eng_id")
    if not eng_provider or not eng_id:
        logger.warning(
            "%s Aborted content_id=%s: eng_provider/eng_id missing after gate check",
            LOG_PREFIX,
            content_id,
        )
        return None

    try:
        download_start = time.perf_counter()
        if eng_provider == EMBEDDED_PROVIDER:
            embedded = read_embedded_reference_bytes(str(eng_id))
            if embedded is None:
                logger.warning(
                    "%s Aborted content_id=%s: embedded reference missing hash=%s",
                    LOG_PREFIX,
                    content_id,
                    eng_id,
                )
                return None
            eng_bytes, eng_ext = embedded
        else:
            from ..routes.utils import download_provider_subtitle_bytes
            eng_bytes, eng_ext = await download_provider_subtitle_bytes(user, eng_provider, eng_id, episode=episode)
        download_elapsed = time.perf_counter() - download_start
        logger.info(
            "%s English reference loaded in %.3fs content_id=%s provider=%s id=%s ext=%s bytes=%d",
            LOG_PREFIX,
            download_elapsed,
            content_id,
            eng_provider,
            eng_id,
            eng_ext,
            len(eng_bytes),
        )

        convert_start = time.perf_counter()
        reference_srt = subtitle_bytes_to_srt(eng_bytes, eng_ext)
        spanish_srt = SSAFile.from_string(vtt_content).to_string("srt")
        convert_elapsed = time.perf_counter() - convert_start
        logger.info(
            "%s Converted to SRT in %.3fs content_id=%s ref_srt_bytes=%d spa_srt_bytes=%d",
            LOG_PREFIX,
            convert_elapsed,
            content_id,
            len(reference_srt.encode("utf-8")),
            len(spanish_srt.encode("utf-8")),
        )

        sync_start = time.perf_counter()
        synced_vtt = sync_spanish_with_english_reference(reference_srt, spanish_srt)
        sync_elapsed = time.perf_counter() - sync_start

        if not synced_vtt:
            total_elapsed = time.perf_counter() - total_start
            logger.warning(
                "%s Sync FAILED content_id=%s total_time=%.3fs "
                "(eng_download=%.3fs convert=%.3fs sync=%.3fs) eng_ref=%s/%s",
                LOG_PREFIX,
                content_id,
                total_elapsed,
                download_elapsed,
                convert_elapsed,
                sync_elapsed,
                eng_provider,
                eng_id,
            )
            return None

        synced_vtt = normalize_vtt_for_players(synced_vtt)
        write_cached_vtt(cache_key, synced_vtt)
        total_elapsed = time.perf_counter() - total_start
        logger.info(
            "%s Sync SUCCEEDED content_id=%s sync_time=%.3fs total_time=%.3fs "
            "input_vtt_bytes=%d output_vtt_bytes=%d eng_ref=%s/%s spa=%s/%s cache_key=%s",
            LOG_PREFIX,
            content_id,
            sync_elapsed,
            total_elapsed,
            len(vtt_content.encode("utf-8")),
            len(synced_vtt.encode("utf-8")),
            eng_provider,
            eng_id,
            sync_meta.get("spa_provider"),
            sync_meta.get("spa_id"),
            cache_key,
        )
        return synced_vtt
    except Exception:
        total_elapsed = time.perf_counter() - total_start
        logger.exception(
            "%s Sync ERROR content_id=%s after %.3fs eng_ref=%s/%s spa=%s/%s",
            LOG_PREFIX,
            content_id,
            total_elapsed,
            eng_provider,
            eng_id,
            sync_meta.get("spa_provider"),
            sync_meta.get("spa_id"),
        )
        return None
