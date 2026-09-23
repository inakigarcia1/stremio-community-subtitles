import importlib.util
import json
import time
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "lib" / "embedded_reference_index.py"
_spec = importlib.util.spec_from_file_location("embedded_reference_index", _MODULE_PATH)
embedded_reference_index = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(embedded_reference_index)

listing_search_languages = embedded_reference_index.listing_search_languages
embedded_identity_key = embedded_reference_index.embedded_identity_key
save_embedded_index = embedded_reference_index.save_embedded_index
lookup_embedded_index = embedded_reference_index.lookup_embedded_index
resolve_sync_english_reference = embedded_reference_index.resolve_sync_english_reference
embedded_meta_to_english_reference = embedded_reference_index.embedded_meta_to_english_reference


def test_listing_search_languages_does_not_append_english():
    langs = listing_search_languages(["spa"], embedded_reference=None)
    assert langs == ["spa"]
    langs_with_embedded = listing_search_languages(["spa"], embedded_reference={"provider_subtitle_id": "abc"})
    assert langs_with_embedded == ["spa"]


def test_embedded_index_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("FFSUBSYNC_EMBEDDED_INDEX_DIR", str(tmp_path))
    meta = {
        "provider_subtitle_id": "a" * 64,
        "ext": ".srt",
        "lang": "eng",
    }
    save_embedded_index("tt1", "hash", 123, "file.mkv", meta)
    loaded = lookup_embedded_index("tt1", "hash", 123, "file.mkv")
    assert loaded is not None
    assert loaded["provider_subtitle_id"] == meta["provider_subtitle_id"]


def test_embedded_index_expires(tmp_path, monkeypatch):
    monkeypatch.setenv("FFSUBSYNC_EMBEDDED_INDEX_DIR", str(tmp_path))
    monkeypatch.setenv("FFSUBSYNC_CACHE_TTL_SECONDS", "60")
    meta = {"provider_subtitle_id": "b" * 64, "ext": ".srt"}
    save_embedded_index("tt2", None, None, None, meta)
    key = embedded_identity_key("tt2", None, None, None)
    path = tmp_path / f"{key}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["saved_at"] = time.time() - 120
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert lookup_embedded_index("tt2", None, None, None) is None


def test_resolve_prefers_index_over_token_community_eng():
    indexed = embedded_meta_to_english_reference(
        {"provider_subtitle_id": "c" * 64, "ext": ".srt", "lang": "eng"}
    )
    token_meta = {
        "eng_provider": "opensubtitles",
        "eng_id": "999",
        "spa_provider": "opensubtitles",
        "spa_id": "1",
    }

    def fake_lookup(content_id, video_hash, video_size, video_filename):
        return {"provider_subtitle_id": "c" * 64, "ext": ".srt", "lang": "eng"}

    original_lookup = embedded_reference_index.lookup_embedded_index
    embedded_reference_index.lookup_embedded_index = fake_lookup
    try:
        resolved = resolve_sync_english_reference("tt3", "h", 1, "f.mkv", token_meta)
    finally:
        embedded_reference_index.lookup_embedded_index = original_lookup

    assert resolved is not None
    assert resolved["provider_name"] == "embedded"
    assert resolved["provider_subtitle_id"] == "c" * 64
