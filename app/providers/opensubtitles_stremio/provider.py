"""OpenSubtitles addon used by Stremio, separate from the official API provider.

No credentials. It is not part of the parallel provider search. The addon
calls it only after OpenSubtitles, SubDL and SubSource returned no Spanish.
"""
import random
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp
from quart import current_app

from ...languages import is_spanish_language
from ...routes.utils import calculate_filename_similarity
from ..base import BaseSubtitleProvider, SubtitleResult

LISTING_ORIGIN = "https://opensubtitles-v3.strem.io"
PRIMARY_PROVIDERS = ("opensubtitles", "subdl", "subsource")


def build_listing_url(
    content_type: Optional[str],
    imdb_id: str,
    filename: Optional[str] = None,
    video_size: Optional[Any] = None,
    video_hash: Optional[str] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> str:
    """Path-only listing URL. The episode colon is %3A. No query string."""
    if content_type == "series" and season is not None and episode is not None:
        kind = "series"
        content_key = quote(f"{imdb_id}:{int(season)}:{int(episode)}", safe="")
    else:
        kind = "movie"
        content_key = quote(str(imdb_id), safe="")

    tail = []
    if filename:
        tail.append("filename=" + quote(filename, safe="()"))
    if video_size not in (None, ""):
        tail.append("videoSize=" + quote(str(video_size), safe=""))
    if video_hash:
        tail.append("videoHash=" + quote(str(video_hash), safe=""))
    segment = "&".join(tail)
    return f"{LISTING_ORIGIN}/subtitles/{kind}/{content_key}/{segment}.json"


def primary_providers_returned_spanish(cached_results) -> bool:
    if not cached_results:
        return False
    for name in PRIMARY_PROVIDERS:
        for result in cached_results.get(name) or []:
            if is_spanish_language(getattr(result, "language", None)):
                return True
    return False


def _release_names(item: Dict[str, Any]) -> List[str]:
    names = []
    for key in ("movieReleaseName", "subtitleFileName", "release_name"):
        value = item.get(key)
        if value:
            names.append(str(value))
    return names


def score_listing_item(video_filename: Optional[str], item: Dict[str, Any]) -> float:
    names = _release_names(item)
    if not video_filename or not names:
        return 0.0
    return max(calculate_filename_similarity(video_filename, name) for name in names)


def pick_spanish_listing_item(video_filename: Optional[str], items: List[Dict[str, Any]], rng=None):
    """Keep spa, spl and other Spanish codes. Always return one if any exist."""
    spanish = [item for item in items if is_spanish_language(item.get("lang") or item.get("language"))]
    if not spanish:
        return None
    scored = [(score_listing_item(video_filename, item), item) for item in spanish]
    best = max(score for score, _item in scored)
    tied = [item for score, item in scored if score == best]
    chooser = rng or random
    return chooser.choice(tied)


def best_release_name(video_filename: Optional[str], item: Dict[str, Any]) -> str:
    names = _release_names(item)
    if not names:
        return str(item.get("id") or "")
    if not video_filename:
        return names[0]
    return max(names, key=lambda name: calculate_filename_similarity(video_filename, name))


def listing_item_to_result(item: Dict[str, Any], video_filename: Optional[str] = None, score: Optional[float] = None) -> SubtitleResult:
    url = item.get("url") or ""
    encoding = item.get("SubEncoding") or item.get("encoding")
    return SubtitleResult(
        provider_name="opensubtitles_stremio",
        subtitle_id=url or str(item.get("id") or ""),
        language="spa",
        release_name=best_release_name(video_filename, item),
        metadata={
            "url": url,
            "encoding": encoding,
            "source_lang": item.get("lang") or item.get("language"),
            "hash_match": False,
            "filename_score": score,
        },
    )


class OpenSubtitlesStremioProvider(BaseSubtitleProvider):
    name = "opensubtitles_stremio"
    display_name = "OpenSubtitles Stremio"
    badge_color = "secondary"
    requires_auth = False
    supports_search = True
    supports_hash_matching = False
    can_return_ass = False
    include_in_parallel_search = False
    listed_in_settings = False

    async def authenticate(self, user, credentials: Dict[str, str]) -> Dict[str, Any]:
        return {"active": True}

    async def logout(self, user) -> bool:
        return True

    async def is_authenticated(self, user) -> bool:
        return True

    async def search(
        self,
        user,
        imdb_id: Optional[str] = None,
        query: Optional[str] = None,
        languages: Optional[List[str]] = None,
        video_hash: Optional[str] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        content_type: Optional[str] = None,
        **kwargs
    ) -> List[SubtitleResult]:
        if languages and not any(is_spanish_language(lang) for lang in languages):
            return []
        if not imdb_id:
            return []

        filename = kwargs.get("video_filename")
        video_size = kwargs.get("video_size")
        url = build_listing_url(
            content_type=content_type,
            imdb_id=imdb_id,
            filename=filename,
            video_size=video_size,
            video_hash=video_hash,
            season=season,
            episode=episode,
        )
        current_app.logger.info("OpenSubtitles Stremio listing %s", url)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as response:
                    response.raise_for_status()
                    payload = await response.json(content_type=None)
        except Exception as exc:
            current_app.logger.warning("OpenSubtitles Stremio search failed: %s", exc)
            return []

        items = payload.get("subtitles") if isinstance(payload, dict) else None
        if not items:
            return []
        chosen = pick_spanish_listing_item(filename, items)
        if not chosen:
            return []
        score = score_listing_item(filename, chosen)
        return [listing_item_to_result(chosen, filename, score)]

    async def get_download_url(self, user, subtitle_id: str) -> Optional[str]:
        if subtitle_id and subtitle_id.startswith("http"):
            return subtitle_id
        return None

    def get_settings_template(self) -> str:
        return "providers/opensubtitles_form.html"
