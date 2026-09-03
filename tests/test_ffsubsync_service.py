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


REFERENCE_SRT = """1
00:00:01,000 --> 00:00:03,000
Hello world

2
00:00:05,000 --> 00:00:07,000
Second line
"""

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


def test_should_ffsubsync_runs_for_low_score_with_english_reference():
    assert should_ffsubsync(
        {
            "match_kind": "filename",
            "filename_score": 0.2,
            "eng_provider": "opensubtitles",
            "eng_id": "1",
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

