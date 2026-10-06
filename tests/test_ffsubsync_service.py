import importlib.util
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "lib" / "ffsubsync_service.py"
_spec = importlib.util.spec_from_file_location("ffsubsync_service", _MODULE_PATH)
ffsubsync_service = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(ffsubsync_service)

build_sync_metadata = ffsubsync_service.build_sync_metadata
make_sync_cache_key = ffsubsync_service.make_sync_cache_key
maybe_apply_ffsubsync = ffsubsync_service.maybe_apply_ffsubsync
read_cached_vtt = ffsubsync_service.read_cached_vtt
should_ffsubsync = ffsubsync_service.should_ffsubsync
sync_spanish_with_english_reference = ffsubsync_service.sync_spanish_with_english_reference
write_cached_vtt = ffsubsync_service.write_cached_vtt


def _dense_reference_srt(events: int = 24) -> str:
    blocks = []
    for index in range(events):
        start = index * 20
        minutes = start // 60
        seconds = start % 60
        blocks.append(
            f"{index + 1}\n"
            f"00:{minutes:02d}:{seconds:02d},000 --> 00:{minutes:02d}:{seconds:02d},800\n"
            f"Reference line {index} spoken clearly\n"
        )
    return "\n".join(blocks)


REFERENCE_SRT = """1
00:00:01,000 --> 00:00:03,000
Hello world

2
00:00:05,000 --> 00:00:07,000
Second line
"""

DENSE_REFERENCE_SRT = _dense_reference_srt()

UNSYNCED_SPANISH_SRT = """1
00:00:04,000 --> 00:00:06,000
Hola mundo

2
00:00:08,000 --> 00:00:10,000
Segunda linea
"""


def test_should_ffsubsync_skips_hash_matches():
    assert not should_ffsubsync({"match_kind": "hash", "eng_provider": "opensubtitles", "eng_id": "1", "spa_provider": "opensubtitles", "spa_id": "2"})
    assert not should_ffsubsync({"match_kind": "local_hash", "eng_provider": "opensubtitles", "eng_id": "1", "spa_provider": "opensubtitles", "spa_id": "2"})


def test_should_ffsubsync_skips_high_filename_score():
    assert not should_ffsubsync(
        {
            "match_kind": "filename",
            "filename_score": 0.8,
            "eng_provider": "opensubtitles",
            "eng_id": "1",
            "spa_provider": "opensubtitles",
            "spa_id": "2",
        }
    )


def test_should_ffsubsync_runs_when_english_filename_score_is_better():
    assert should_ffsubsync(
        {
            "match_kind": "filename",
            "filename_score": 0.2,
            "eng_provider": "opensubtitles",
            "eng_id": "1",
            "eng_match_kind": "filename",
            "eng_filename_score": 0.6,
            "spa_provider": "opensubtitles",
            "spa_id": "2",
        }
    )


def test_should_ffsubsync_runs_when_english_filename_beats_a_high_spanish_score():
    assert should_ffsubsync(
        {
            "match_kind": "filename",
            "filename_score": 0.8,
            "eng_provider": "opensubtitles",
            "eng_id": "1",
            "eng_match_kind": "filename",
            "eng_filename_score": 0.9,
            "spa_provider": "opensubtitles",
            "spa_id": "2",
        }
    )


def test_should_ffsubsync_skips_when_english_filename_score_is_not_better():
    assert not should_ffsubsync(
        {
            "match_kind": "filename",
            "filename_score": 0.5,
            "eng_provider": "opensubtitles",
            "eng_id": "1",
            "eng_match_kind": "filename",
            "eng_filename_score": 0.5,
            "spa_provider": "opensubtitles",
            "spa_id": "2",
        }
    )


def test_build_sync_metadata_includes_english_reference():
    metadata = build_sync_metadata(
        {
            "match_kind": "fallback",
            "filename_score": None,
            "provider_name": "opensubtitles",
            "provider_subtitle_id": "spa-1",
        },
        {
            "type": "opensubtitles_auto",
            "provider_name": "opensubtitles",
            "provider_subtitle_id": "eng-1",
        },
    )
    assert metadata["spa_id"] == "spa-1"
    assert metadata["eng_id"] == "eng-1"


def test_save_and_read_embedded_reference(tmp_path, monkeypatch):
    monkeypatch.setenv("FFSUBSYNC_EMBEDDED_DIR", str(tmp_path))
    monkeypatch.setenv("FFSUBSYNC_CACHE_TTL_SECONDS", "3600")
    save_embedded_reference = ffsubsync_service.save_embedded_reference
    read_embedded_reference_bytes = ffsubsync_service.read_embedded_reference_bytes

    saved = save_embedded_reference(DENSE_REFERENCE_SRT.encode("utf-8"), "ref.srt")
    assert saved is not None
    assert saved["provider_name"] == "embedded"
    loaded = read_embedded_reference_bytes(saved["provider_subtitle_id"])
    assert loaded is not None
    data, ext = loaded
    assert ext == ".srt"
    assert b"Reference line 0" in data


def test_save_embedded_reference_rejects_oversized(tmp_path, monkeypatch):
    monkeypatch.setenv("FFSUBSYNC_EMBEDDED_DIR", str(tmp_path))
    huge = b"x" * (ffsubsync_service.MAX_EMBEDDED_REFERENCE_BYTES + 1)
    assert ffsubsync_service.save_embedded_reference(huge, "ref.srt") is None


def test_save_embedded_reference_rejects_thin_sample(tmp_path, monkeypatch):
    monkeypatch.setenv("FFSUBSYNC_EMBEDDED_DIR", str(tmp_path))
    assert ffsubsync_service.save_embedded_reference(REFERENCE_SRT.encode("utf-8"), "ref.srt") is None


def test_reference_too_thin_reason():
    assert ffsubsync_service.reference_too_thin_reason(REFERENCE_SRT) is not None
    assert ffsubsync_service.reference_too_thin_reason(DENSE_REFERENCE_SRT) is None


def test_parse_and_reject_ep9_style_alignment():
    alignment = ffsubsync_service.parse_ffsubsync_alignment(
        "",
        "INFO     score: 5327.837\nINFO     offset seconds: 16.210\nINFO     framerate scale factor: 1.043",
    )
    assert alignment.score == pytest.approx(5327.837)
    assert alignment.offset_seconds == pytest.approx(16.210)
    assert alignment.scale == pytest.approx(1.043)
    assert ffsubsync_service.alignment_reject_reason(alignment) is not None


def test_accepts_borderline_high_score_with_unit_scale():
    alignment = ffsubsync_service.parse_ffsubsync_alignment(
        "",
        "INFO     score: 11056.396\nINFO     offset seconds: 2.01\nINFO     framerate scale factor: 1.001",
    )
    assert ffsubsync_service.alignment_reject_reason(alignment) is None
    alignment = ffsubsync_service.parse_ffsubsync_alignment(
        "",
        "INFO     score: 27938.426\nINFO     offset seconds: 2.060\nINFO     framerate scale factor: 1.001",
    )
    assert ffsubsync_service.alignment_reject_reason(alignment) is None


def test_accepts_high_score_with_nontrivial_framerate_scale():
    alignment = ffsubsync_service.parse_ffsubsync_alignment(
        "",
        "INFO     score: 22104.5\nINFO     offset seconds: 1.250\nINFO     framerate scale factor: 1.042",
    )
    assert ffsubsync_service.alignment_reject_reason(alignment) is None


def test_rejects_missing_or_negative_score():
    missing = ffsubsync_service.parse_ffsubsync_alignment("", "INFO     offset seconds: 1.0")
    assert ffsubsync_service.alignment_reject_reason(missing) is not None
    negative = ffsubsync_service.parse_ffsubsync_alignment(
        "",
        "INFO     score: -5816.0\nINFO     offset seconds: 52.66\nINFO     framerate scale factor: 0.405",
    )
    assert ffsubsync_service.alignment_reject_reason(negative) is not None


def test_make_sync_cache_key_includes_embedded_id():
    context = {"content_id": "tt1", "v_hash": "h", "v_size": 1, "v_fname": "a.mkv"}
    provider_meta = {
        "spa_provider": "opensubtitles",
        "spa_id": "spa",
        "eng_provider": "opensubtitles",
        "eng_id": "eng",
    }
    embedded_meta = {
        "spa_provider": "opensubtitles",
        "spa_id": "spa",
        "eng_provider": "embedded",
        "eng_id": "a" * 64,
    }
    assert make_sync_cache_key(context, provider_meta) != make_sync_cache_key(context, embedded_meta)


def test_should_ffsubsync_runs_for_embedded_low_score():
    assert should_ffsubsync(
        {
            "match_kind": "filename",
            "filename_score": 0.2,
            "eng_provider": "embedded",
            "eng_id": "a" * 64,
            "spa_provider": "opensubtitles",
            "spa_id": "2",
        }
    )


def test_should_ffsubsync_skips_embedded_when_filename_score_high():
    assert not should_ffsubsync(
        {
            "match_kind": "filename",
            "filename_score": 0.8,
            "eng_provider": "embedded",
            "eng_id": "a" * 64,
            "spa_provider": "opensubtitles",
            "spa_id": "2",
        }
    )


@pytest.mark.skipif(
    os.system("ffsubsync --version >nul 2>&1") != 0,
    reason="ffsubsync CLI not installed in test environment",
)
def test_sync_spanish_with_english_reference_aligns_timings():
    synced_vtt = sync_spanish_with_english_reference(REFERENCE_SRT, UNSYNCED_SPANISH_SRT)
    assert synced_vtt is not None
    assert "WEBVTT" in synced_vtt
    assert "Hola mundo" in synced_vtt
    assert "00:00:01." in synced_vtt or "00:00:01," in synced_vtt


def test_sync_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("FFSUBSYNC_CACHE_DIR", str(tmp_path))
    context = {"content_id": "tt123", "v_hash": "abc", "v_size": 10, "v_fname": "movie.mkv"}
    sync_meta = {
        "spa_provider": "opensubtitles",
        "spa_id": "1",
        "eng_provider": "opensubtitles",
        "eng_id": "2",
    }
    cache_key = make_sync_cache_key(context, sync_meta)
    assert read_cached_vtt(cache_key) is None
    write_cached_vtt(cache_key, "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nHola")
    assert "Hola" in read_cached_vtt(cache_key)

