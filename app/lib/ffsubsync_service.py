"""Subtitle-to-subtitle sync via ffsubsync (English reference -> Spanish input)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from typing import Any, Dict, Optional, Tuple

from pysubs2 import SSAFile, load

logger = logging.getLogger(__name__)
LOG_PREFIX = "[ffsubsync]"


def ffsubsync_logger():
    """Dedicated logger; always INFO on stdout (see app.create_app)."""
    return logger


def describe_eng_reference_source(sync_meta: Optional[Dict[str, Any]]) -> str:
    """Human-readable label for which English reference ffsubsync would use."""
    if not sync_meta:
        return "none (no sync metadata)"
    eng_provider = sync_meta.get("eng_provider")
    eng_id = sync_meta.get("eng_id")
    if not eng_provider or not eng_id:
        return "none (missing eng_provider/eng_id)"
    if eng_provider == EMBEDDED_PROVIDER:
        return f"client embedded upload (hash={eng_id})"
    return f"community provider {eng_provider} (id={eng_id})"

DEFAULT_MIN_FILENAME_SCORE = 0.5
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_CACHE_TTL_SECONDS = 21600
EMBEDDED_PROVIDER = "embedded"
MAX_EMBEDDED_REFERENCE_BYTES = 5 * 1024 * 1024
ALLOWED_REFERENCE_EXTENSIONS = {".srt", ".vtt", ".ass", ".ssa"}
# Reject the CLI result only when ffsubsync itself reports a weak match.
# A non-1.0 scale is how PAL/NTSC (and similar) remuxes get fixed; ep8 was
# score=27938 / scale=1.001. Garbage alignments show up as low/negative scores
# (ep9=5327, ep15=-5816), not as "scale != 1". S02E01 scored 11056 with
# offset=2.01 / scale=1.001 — applying that beat serving the unsynced file.
MIN_ALIGNMENT_SCORE = 10000.0
MIN_REFERENCE_EVENTS = 24
MIN_REFERENCE_SRT_BYTES = 800
SYNC_ALGO_VERSION = "score-v2"


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


def _decode_reference_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def count_reference_events(data: bytes, filename: Optional[str] = None) -> int:
    text = _decode_reference_text(data)
    ext = reference_extension(filename)
    if ext in {".ass", ".ssa"}:
        return sum(1 for line in text.splitlines() if line.lower().startswith("dialogue:"))
    return text.count("-->")


def save_embedded_reference(data: bytes, filename: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not data or len(data) > MAX_EMBEDDED_REFERENCE_BYTES:
        ffsubsync_logger().warning(
            "%s Rejected embedded reference (bytes=%d max=%d)",
            LOG_PREFIX,
            0 if not data else len(data),
            MAX_EMBEDDED_REFERENCE_BYTES,
        )
        return None
    events = count_reference_events(data, filename)
    if events < MIN_REFERENCE_EVENTS or len(data) < MIN_REFERENCE_SRT_BYTES:
        ffsubsync_logger().warning(
            "%s Rejected embedded reference too thin (bytes=%d events=%d min_bytes=%d min_events=%d)",
            LOG_PREFIX,
            len(data),
            events,
            MIN_REFERENCE_SRT_BYTES,
            MIN_REFERENCE_EVENTS,
        )
        return None
    digest = hashlib.sha256(data).hexdigest()
    ext = reference_extension(filename)
    path = os.path.join(embedded_reference_dir(), f"{digest}{ext}")
    if not os.path.exists(path):
        with open(path, "wb") as handle:
            handle.write(data)
    ffsubsync_logger().info(
        "%s Stored embedded reference hash=%s ext=%s bytes=%d filename=%s",
        LOG_PREFIX,
        digest,
        ext,
        len(data),
        filename or "(unknown)",
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


def english_filename_not_better_reason(sync_meta: Dict[str, Any]) -> Optional[str]:
    """Sync against a provider English sub only when its filename score beats Spanish.

    An embedded English track is the file itself, and a hash match is already
    aligned to this copy. Those are not filename guesses.
    """
    if sync_meta.get("eng_provider") == EMBEDDED_PROVIDER:
        return None
    if sync_meta.get("eng_match_kind") in {"hash", "local_hash"}:
        return None
    english_score = sync_meta.get("eng_filename_score")
    spanish_score = sync_meta.get("filename_score")
    if english_score is None or spanish_score is None:
        return "english filename score is not better than spanish"
    if float(english_score) <= float(spanish_score):
        return (
            f"english filename score {float(english_score):.4f} "
            f"<= spanish {float(spanish_score):.4f}"
        )
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
    worse_english = english_filename_not_better_reason(sync_meta)
    if worse_english:
        return worse_english
    if (
        sync_meta.get("eng_provider") != EMBEDDED_PROVIDER
        and sync_meta.get("eng_match_kind") not in {"hash", "local_hash"}
        and sync_meta.get("eng_filename_score") is not None
    ):
        return None
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
            SYNC_ALGO_VERSION,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def playback_identity_key(context: Dict[str, Any]) -> str:
    payload = "|".join(
        [
            context.get("content_id") or "",
            context.get("v_hash") or "",
            str(context.get("v_size") if context.get("v_size") is not None else ""),
            context.get("v_fname") or "",
            SYNC_ALGO_VERSION,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _playback_index_path(identity: str) -> str:
    return os.path.join(sync_cache_dir(), f"{identity}.playback.json")


def remember_playback_sync(context: Dict[str, Any], sync_meta: Dict[str, Any]) -> None:
    cache_key = make_sync_cache_key(context, sync_meta)
    if read_cached_vtt(cache_key) is None:
        return
    path = _playback_index_path(playback_identity_key(context))
    payload = {"sync": sync_meta, "cache_key": cache_key}
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(temporary, path)


def lookup_playback_sync(context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    path = _playback_index_path(playback_identity_key(context))
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    cache_key = payload.get("cache_key")
    sync = payload.get("sync") or {}
    if not cache_key or read_cached_vtt(cache_key) is None:
        return None
    return {"sync": sync, "cache_key": cache_key}


def _cache_file_path(cache_key: str) -> str:
    return os.path.join(sync_cache_dir(), f"{cache_key}.vtt")


def _score_file_path(cache_key: str) -> str:
    return os.path.join(sync_cache_dir(), f"{cache_key}.score.json")


def log_ffsubsync_score(
    content_id: str,
    alignment: Optional["FfsubsyncAlignment"],
    *,
    accepted: bool,
    cached: bool = False,
    reason: Optional[str] = None,
    cache_key: Optional[str] = None,
) -> None:
    if accepted and cache_key and alignment is not None:
        write_cached_alignment(cache_key, alignment)


def write_cached_alignment(cache_key: str, alignment: "FfsubsyncAlignment") -> None:
    payload = {
        "score": alignment.score,
        "offset_seconds": alignment.offset_seconds,
        "scale": alignment.scale,
    }
    with open(_score_file_path(cache_key), "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def read_cached_alignment(cache_key: str) -> Optional["FfsubsyncAlignment"]:
    path = _score_file_path(cache_key)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return FfsubsyncAlignment(
            score=payload.get("score"),
            offset_seconds=payload.get("offset_seconds"),
            scale=payload.get("scale"),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


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
        try:
            os.remove(_score_file_path(cache_key))
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


class FfsubsyncAlignment:
    def __init__(
        self,
        score: Optional[float] = None,
        offset_seconds: Optional[float] = None,
        scale: Optional[float] = None,
    ) -> None:
        self.score = score
        self.offset_seconds = offset_seconds
        self.scale = scale


def parse_ffsubsync_alignment(stdout: str, stderr: str) -> FfsubsyncAlignment:
    text = f"{stderr or ''}\n{stdout or ''}"
    alignment = FfsubsyncAlignment()
    for line in text.splitlines():
        lower = line.lower()
        if "score:" in lower:
            match = re.search(r"score:\s*([-+]?\d+(?:\.\d+)?)", lower)
            if match:
                alignment.score = float(match.group(1))
        if "offset seconds:" in lower:
            match = re.search(r"offset seconds:\s*([-+]?\d+(?:\.\d+)?)", lower)
            if match:
                alignment.offset_seconds = float(match.group(1))
        if "framerate scale factor:" in lower:
            match = re.search(r"framerate scale factor:\s*([-+]?\d+(?:\.\d+)?)", lower)
            if match:
                alignment.scale = float(match.group(1))
    return alignment


def alignment_reject_reason(alignment: FfsubsyncAlignment) -> Optional[str]:
    if alignment.score is None:
        return "alignment score missing"
    if alignment.score < MIN_ALIGNMENT_SCORE:
        return f"alignment score {alignment.score:.1f} below {MIN_ALIGNMENT_SCORE:.0f}"
    return None


def reference_too_thin_reason(reference_srt: str) -> Optional[str]:
    events = reference_srt.count("-->")
    nbytes = len(reference_srt.encode("utf-8"))
    if events < MIN_REFERENCE_EVENTS:
        return f"reference too thin ({events} events, need {MIN_REFERENCE_EVENTS})"
    if nbytes < MIN_REFERENCE_SRT_BYTES:
        return f"reference too small ({nbytes} bytes, need {MIN_REFERENCE_SRT_BYTES})"
    return None


def run_ffsubsync(
    reference_srt: str,
    input_srt: str,
    output_srt: str,
    extra_args: Optional[list] = None,
) -> Tuple[bool, FfsubsyncAlignment]:
    cmd = [
        "ffsubsync",
        reference_srt,
        "-i",
        input_srt,
        "-o",
        output_srt,
    ]
    if extra_args:
        cmd.extend(extra_args)
    start = time.perf_counter()
    logger.info(
        "%s Starting CLI: %s (timeout=%ds)",
        LOG_PREFIX,
        " ".join(cmd),
        ffsubsync_timeout_seconds(),
    )
    empty = FfsubsyncAlignment()
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=ffsubsync_timeout_seconds(),
            check=False,
        )
        elapsed = time.perf_counter() - start
        alignment = parse_ffsubsync_alignment(completed.stdout or "", completed.stderr or "")
        if completed.returncode == 0 and os.path.exists(output_srt):
            output_size = os.path.getsize(output_srt)
            logger.info(
                "%s CLI finished successfully in %.3fs (output_size=%d bytes "
                "score=%s offset=%s scale=%s)",
                LOG_PREFIX,
                elapsed,
                output_size,
                alignment.score,
                alignment.offset_seconds,
                alignment.scale,
            )
            return True, alignment
        logger.warning(
            "%s CLI failed in %.3fs returncode=%s output_exists=%s stderr=%r stdout=%r",
            LOG_PREFIX,
            elapsed,
            completed.returncode,
            os.path.exists(output_srt),
            (completed.stderr or "").strip()[:500],
            (completed.stdout or "").strip()[:500],
        )
        return False, alignment
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - start
        logger.warning(
            "%s CLI timed out after %.3fs (limit=%ds)",
            LOG_PREFIX,
            elapsed,
            ffsubsync_timeout_seconds(),
        )
        return False, empty
    except FileNotFoundError:
        logger.error("%s ffsubsync binary not found in PATH", LOG_PREFIX)
        return False, empty
    except OSError as exc:
        logger.error("%s CLI OS error: %s", LOG_PREFIX, exc)
        return False, empty


def srt_to_vtt(srt_content: str) -> str:
    subs = SSAFile.from_string(srt_content)
    return subs.to_string("vtt")


def sync_spanish_with_english_reference(
    reference_srt: str,
    spanish_srt: str,
    content_id: str = "?",
    cache_key: Optional[str] = None,
) -> Optional[str]:
    ref_lines = reference_srt.count("\n") + 1 if reference_srt else 0
    spa_lines = spanish_srt.count("\n") + 1 if spanish_srt else 0
    start = time.perf_counter()
    logger.info(
        "%s sync_spanish_with_english_reference started (ref_srt_lines=%d spa_srt_lines=%d "
        "ref_bytes=%d spa_bytes=%d)",
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
        ok, alignment = run_ffsubsync(reference_path, input_path, output_path)
        if not ok:
            elapsed = time.perf_counter() - start
            logger.warning("%s sync_spanish_with_english_reference failed in %.3fs", LOG_PREFIX, elapsed)
            log_ffsubsync_score(content_id, alignment, accepted=False, reason="cli_failed")
            return None
        reject = alignment_reject_reason(alignment)
        if reject:
            elapsed = time.perf_counter() - start
            logger.warning(
                "%s rejected alignment in %.3fs reason=%s score=%s offset=%s scale=%s",
                LOG_PREFIX,
                elapsed,
                reject,
                alignment.score,
                alignment.offset_seconds,
                alignment.scale,
            )
            log_ffsubsync_score(content_id, alignment, accepted=False, reason=reject)
            return None
        with open(output_path, "r", encoding="utf-8") as handle:
            synced_srt = handle.read()
        synced_vtt = srt_to_vtt(synced_srt)
        elapsed = time.perf_counter() - start
        logger.info(
            "%s sync_spanish_with_english_reference finished in %.3fs "
            "(output_vtt_bytes=%d score=%s offset=%s scale=%s)",
            LOG_PREFIX,
            elapsed,
            len(synced_vtt.encode("utf-8")),
            alignment.score,
            alignment.offset_seconds,
            alignment.scale,
        )
        log_ffsubsync_score(content_id, alignment, accepted=True, cache_key=cache_key)
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
        metadata["eng_match_kind"] = english_info.get("match_kind")
        metadata["eng_filename_score"] = english_info.get("filename_score")
    return metadata


async def maybe_apply_ffsubsync(user, vtt_content: str, context: Dict[str, Any], sync_meta: Dict[str, Any], episode=None) -> Optional[str]:
    from ..lib.subtitles import normalize_vtt_for_players

    content_id = context.get("content_id", "?")
    skip_reason = ffsubsync_skip_reason(sync_meta)
    eng_source = describe_eng_reference_source(sync_meta)
    if skip_reason:
        ffsubsync_logger().info(
            "%s Skipped sync content_id=%s reason=%s eng_source=%s match_kind=%s filename_score=%s "
            "spa=%s/%s",
            LOG_PREFIX,
            content_id,
            skip_reason,
            eng_source,
            sync_meta.get("match_kind"),
            sync_meta.get("filename_score"),
            sync_meta.get("spa_provider"),
            sync_meta.get("spa_id"),
        )
        return None

    cache_key = make_sync_cache_key(context, sync_meta)
    cached = read_cached_vtt(cache_key)
    if cached:
        cached_alignment = read_cached_alignment(cache_key)
        offset = cached_alignment.offset_seconds if cached_alignment else None
        ffsubsync_logger().info(
            "%s %s: sync cache hit eng=%s/%s offset=%ss",
            LOG_PREFIX,
            content_id,
            sync_meta.get("eng_provider"),
            sync_meta.get("eng_id"),
            offset,
        )
        remember_playback_sync(context, sync_meta)
        return cached

    eng_provider = sync_meta.get("eng_provider")
    eng_id = sync_meta.get("eng_id")
    if not eng_provider or not eng_id:
        logger.warning(
            "%s Aborted content_id=%s: eng_provider/eng_id missing after gate check",
            LOG_PREFIX,
            content_id,
        )
        return None

    total_start = time.perf_counter()
    try:
        download_start = time.perf_counter()
        if eng_provider == EMBEDDED_PROVIDER:
            embedded = read_embedded_reference_bytes(str(eng_id))
            if embedded is None:
                ffsubsync_logger().warning(
                    "%s Aborted content_id=%s: embedded reference file missing or expired hash=%s",
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

        convert_start = time.perf_counter()
        reference_srt = subtitle_bytes_to_srt(eng_bytes, eng_ext)
        spanish_srt = SSAFile.from_string(vtt_content).to_string("srt")
        convert_elapsed = time.perf_counter() - convert_start

        thin = reference_too_thin_reason(reference_srt)
        if thin:
            logger.warning(
                "%s Skip sync content_id=%s: %s",
                LOG_PREFIX,
                content_id,
                thin,
            )
            return None

        sync_start = time.perf_counter()
        synced_vtt = sync_spanish_with_english_reference(
            reference_srt,
            spanish_srt,
            content_id=content_id,
            cache_key=cache_key,
        )
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
        alignment = read_cached_alignment(cache_key)
        ffsubsync_logger().info(
            "%s %s: synced Spanish eng=%s/%s offset=%ss scale=%s",
            LOG_PREFIX,
            content_id,
            eng_provider,
            eng_id,
            alignment.offset_seconds if alignment else None,
            alignment.scale if alignment else None,
        )
        remember_playback_sync(context, sync_meta)
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
