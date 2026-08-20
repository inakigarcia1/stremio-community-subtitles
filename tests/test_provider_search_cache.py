import pytest
import time

from app.lib.provider_search_cache import (
    get_cached_results,
    make_cache_key,
    merge_and_store,
)
from app.providers.base import SubtitleResult


def _sample_result(provider: str, subtitle_id: str) -> SubtitleResult:
    return SubtitleResult(
        provider_name=provider,
        subtitle_id=subtitle_id,
        language="spa",
    )


def test_cache_hit_avoids_requery(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_SEARCH_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("PROVIDER_SEARCH_CACHE_TTL_SECONDS", "3600")

    key = make_cache_key("movie", "tt123", None, None, "file.mkv", ["spa"])
    merge_and_store(key, {}, {"opensubtitles": [_sample_result("opensubtitles", "1")]})

    cached, missing = get_cached_results(key, ["opensubtitles", "subdl"])
    assert "opensubtitles" in cached
    assert cached["opensubtitles"][0].subtitle_id == "1"
    assert missing == ["subdl"]


def test_new_provider_only_queries_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_SEARCH_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("PROVIDER_SEARCH_CACHE_TTL_SECONDS", "3600")

    key = make_cache_key("movie", "tt123", None, None, None, ["spa"])
    merge_and_store(key, {}, {"opensubtitles": [_sample_result("opensubtitles", "1")]})

    merged = merge_and_store(
        key,
        {"opensubtitles": [_sample_result("opensubtitles", "1")]},
        {"subdl": [_sample_result("subdl", "2")]},
    )
    assert "opensubtitles" in merged
    assert "subdl" in merged
    assert merged["subdl"][0].subtitle_id == "2"


def test_expired_provider_is_requeried(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_SEARCH_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("PROVIDER_SEARCH_CACHE_TTL_SECONDS", "1")

    key = make_cache_key("series", "tt999:1:1", "hash", 123, "ep.mkv", ["spa"])
    merge_and_store(key, {}, {"subdl": [_sample_result("subdl", "old")]})

    time.sleep(1.1)
    cached, missing = get_cached_results(key, ["subdl"])
    assert cached == {}
    assert missing == ["subdl"]
