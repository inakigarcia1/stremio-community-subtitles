"""Offline checks for the community-subtitles port. No network."""
import asyncio

from app.lib.subtitle_id_codec import decode_subtitle_id, encode_subtitle_id
from app.providers.base import SubtitleResult
from app.providers.opensubtitles_stremio.provider import (
    OpenSubtitlesStremioProvider,
    build_listing_url,
    pick_spanish_listing_item,
    primary_providers_returned_spanish,
)
from app.providers.registry import ProviderRegistry
from app.routes.utils import _choose_best_scored, _opensubtitles_stremio_fallback

BB_FILENAME = (
    "Breaking Bad (2008) S01E01 (2160p AMZN WEB-DL H265 SDR DDP 5.1 English - HONE).mkv"
)
BB_LISTING_URL = (
    "https://opensubtitles-v3.strem.io/subtitles/series/tt0903747%3A1%3A1/"
    "filename=Breaking%20Bad%20(2008)%20S01E01%20(2160p%20AMZN%20WEB-DL%20H265%20SDR%20DDP%205.1%20English%20-%20HONE).mkv"
    "&videoSize=6730238562.json"
)


def test_subtitle_id_base64_roundtrip_keeps_url_out_of_the_path():
    raw = "https://dl.subdl.com/subtitle/12345/file.srt?lang=es&type=zip"
    token = encode_subtitle_id(raw)
    assert "/" not in token
    assert "?" not in token
    assert decode_subtitle_id(token) == raw
    assert decode_subtitle_id("plain-id") == "plain-id"


def test_spanish_filter_keeps_spa_and_spl():
    items = [
        {"lang": "spa", "id": "spa", "movieReleaseName": "One"},
        {"lang": "spl", "id": "spl", "movieReleaseName": "Two"},
        {"lang": "por", "id": "por", "movieReleaseName": "Three"},
        {"lang": "eng", "id": "eng", "movieReleaseName": "Four"},
        {"lang": "es-419", "id": "latam", "movieReleaseName": "Five"},
    ]

    class Rng:
        def choice(self, pool):
            self.pool = list(pool)
            return pool[0]

    rng = Rng()
    chosen = pick_spanish_listing_item(None, items, rng=rng)
    kept = {item["lang"] for item in rng.pool}
    assert kept == {"spa", "spl", "es-419"}
    assert chosen["lang"] in kept
    assert "por" not in kept


def test_filename_score_fallback_returns_the_least_bad_and_breaks_ties_at_random(monkeypatch):
    worse = {"score": -0.4, "id": "worse"}
    least_bad = {"score": -0.1, "id": "least"}
    assert _choose_best_scored([worse, least_bad])["id"] == "least"

    positive = {"score": 0.2, "id": "good"}
    assert _choose_best_scored([worse, positive])["id"] == "good"

    tied = [{"score": 0.0, "id": "a"}, {"score": 0.0, "id": "b"}]
    monkeypatch.setattr("app.routes.utils.random.choice", lambda pool: pool[1])
    assert _choose_best_scored(tied)["id"] == "b"


def test_series_listing_url_encodes_colon_in_the_path():
    url = build_listing_url(
        content_type="series",
        imdb_id="tt0903747",
        filename=BB_FILENAME,
        video_size=6730238562,
        season=1,
        episode=1,
    )
    assert url == BB_LISTING_URL
    assert "?" not in url
    assert "%3A" in url

    with_hash = build_listing_url(
        content_type="series",
        imdb_id="tt0903747",
        filename=BB_FILENAME,
        video_size=6730238562,
        video_hash="8e245d9679d31e12",
        season=1,
        episode=1,
    )
    assert "?" not in with_hash
    assert "videoHash=8e245d9679d31e12" in with_hash
    assert with_hash.index("filename=") < with_hash.index("videoSize=") < with_hash.index("videoHash=")

    movie = build_listing_url(
        content_type="movie",
        imdb_id="tt37287335",
        filename="Obsession.2025.2160p.mkv",
        video_size=123,
    )
    assert movie.startswith("https://opensubtitles-v3.strem.io/subtitles/movie/tt37287335/filename=")
    assert "?" not in movie


def test_korra_spa_loses_to_breaking_bad_release():
    items = [
        {
            "lang": "spa",
            "id": "korra",
            "movieReleaseName": "Avatar The Legend of Korra S01E01",
            "subtitleFileName": "Avatar The Legend of Korra S01E01.srt",
            "url": "https://example.invalid/korra.srt",
        },
        {
            "lang": "spa",
            "id": "bb",
            "movieReleaseName": "Breaking.Bad.S01E01.Pilot.720p.BluRay.X264-REWARD",
            "subtitleFileName": "Breaking.Bad.S01E01.Pilot.720p.BluRay.X264-REWARD.srt",
            "url": "https://example.invalid/bb.srt",
        },
        {
            "lang": "eng",
            "id": "english",
            "movieReleaseName": "Breaking.Bad.S01E01.2160p.AMZN.WEB-DL.HONE",
        },
    ]
    chosen = pick_spanish_listing_item(BB_FILENAME, items)
    assert chosen["id"] == "bb"


def test_stremio_provider_is_not_called_when_a_primary_already_has_spanish(monkeypatch):
    calls = {"search": 0}

    async def fake_search(*args, **kwargs):
        calls["search"] += 1
        return []

    monkeypatch.setattr(OpenSubtitlesStremioProvider, "search", fake_search)
    monkeypatch.setattr(
        ProviderRegistry,
        "get",
        classmethod(lambda cls, name: OpenSubtitlesStremioProvider() if name == "opensubtitles_stremio" else None),
    )

    cached = {
        "opensubtitles": [],
        "subdl": [SubtitleResult(provider_name="subdl", subtitle_id="1", language="spa")],
        "subsource": [],
    }
    assert primary_providers_returned_spanish(cached) is True
    result = asyncio.run(
        _opensubtitles_stremio_fallback(
            user=object(),
            imdb_id="tt0903747",
            video_filename=BB_FILENAME,
            content_type="series",
            lang="spa",
            season=1,
            episode=1,
            cached_results=cached,
        )
    )
    assert result is None
    assert calls["search"] == 0


def test_stremio_provider_is_called_only_after_primaries_have_no_spanish(monkeypatch):
    calls = {"search": 0}

    async def fake_search(*args, **kwargs):
        calls["search"] += 1
        return []

    monkeypatch.setattr(OpenSubtitlesStremioProvider, "search", fake_search)
    monkeypatch.setattr(
        ProviderRegistry,
        "get",
        classmethod(lambda cls, name: OpenSubtitlesStremioProvider() if name == "opensubtitles_stremio" else None),
    )

    result = asyncio.run(
        _opensubtitles_stremio_fallback(
            user=object(),
            imdb_id="tt0903747",
            video_filename=BB_FILENAME,
            content_type="series",
            lang="spa",
            season=1,
            episode=1,
            cached_results={"opensubtitles": [], "subdl": [], "subsource": []},
        )
    )
    assert result is None
    assert calls["search"] == 1


def test_stremio_provider_stays_out_of_parallel_search():
    saved = dict(ProviderRegistry._providers)
    saved_init = ProviderRegistry._initialized

    class Primary(OpenSubtitlesStremioProvider):
        name = "test_primary_for_parallel"
        display_name = "Test primary"
        include_in_parallel_search = True

        async def is_authenticated(self, user):
            return True

    try:
        ProviderRegistry._providers = {}
        ProviderRegistry._initialized = True
        ProviderRegistry.register(Primary)
        ProviderRegistry.register(OpenSubtitlesStremioProvider)
        active = asyncio.run(ProviderRegistry.get_active_for_user(object()))
        names = [provider.name for provider in active]
        assert names == ["test_primary_for_parallel"]
    finally:
        ProviderRegistry._providers = saved
        ProviderRegistry._initialized = saved_init
