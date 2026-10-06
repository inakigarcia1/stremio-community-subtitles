"""Subs.ro provider.

Requires an API key saved from the connection form. Without that row it
does not authenticate and does not search.
"""
from typing import List, Dict, Optional, Any
from quart import current_app

from ..base import (
    BaseSubtitleProvider, SubtitleResult,
    ProviderAuthError, ProviderSearchError, ProviderDownloadError,
)
from . import client


_LANG_TO_ISO = {
    "ro": "ron",
    "en": "eng",
    "ita": "ita",
    "fra": "fra",
    "ger": "deu",
    "ung": "hun",
    "gre": "ell",
    "por": "por",
    "spa": "spa",
}

_ISO_TO_LANG = {v: k for k, v in _LANG_TO_ISO.items()}


class SubsRoProvider(BaseSubtitleProvider):
    name = "subsro"
    display_name = "Subs.ro"
    badge_color = "info"
    requires_auth = True
    supports_search = True
    supports_hash_matching = False
    can_return_ass = False
    has_additional_settings = False
    supported_languages = list(_LANG_TO_ISO.values())
    returns_zip = True

    async def authenticate(self, user, credentials: Dict[str, str]) -> Dict[str, Any]:
        api_key = (credentials.get("api_key") or "").strip()
        if not api_key:
            raise ProviderAuthError("API key required", self.name)
        try:
            await client.search_by_imdb(api_key, "tt0111161", language="ro")
            return {"api_key": api_key, "active": True}
        except client.SubsRoError as e:
            raise ProviderAuthError(
                f"Invalid API key: {e}", self.name, getattr(e, "status_code", None)
            )

    async def logout(self, user) -> bool:
        return True

    async def is_authenticated(self, user) -> bool:
        creds = await self.get_credentials(user)
        return bool(creds and creds.get("active") and creds.get("api_key"))

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
        **kwargs,
    ) -> List[SubtitleResult]:
        if not await self.is_authenticated(user):
            return []

        creds = await self.get_credentials(user)
        api_key = creds["api_key"]

        if video_hash and not imdb_id and not query:
            return []

        subsro_langs = self._requested_languages(languages)
        all_items = []
        seen_ids = set()

        for lang_code in subsro_langs:
            items = []
            if imdb_id:
                try:
                    items = await client.search_by_imdb(api_key, imdb_id, language=lang_code)
                except client.SubsRoError:
                    pass
            if not items and query:
                try:
                    items = await client.search_by_title(api_key, query, language=lang_code)
                except client.SubsRoError:
                    pass
            if not items and imdb_id and not query:
                try:
                    from ...lib.metadata import get_metadata
                    cid = imdb_id
                    if season and episode:
                        cid = f"{imdb_id}:{season}:{episode}"
                    metadata = await get_metadata(cid, content_type)
                    if metadata and metadata.get("title"):
                        items = await client.search_by_title(
                            api_key, metadata["title"], language=lang_code
                        )
                except Exception:
                    pass

            for item in items:
                sid = item.get("id")
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)
                all_items.append(item)

        return self._parse_results(all_items)

    async def get_download_url(self, user, subtitle_id: str) -> Optional[str]:
        return None

    async def download_subtitle(self, user, subtitle_id: str) -> bytes:
        if not await self.is_authenticated(user):
            raise ProviderDownloadError("Not authenticated", self.name)
        creds = await self.get_credentials(user)
        try:
            return await client.download_subtitle(creds["api_key"], int(subtitle_id))
        except client.SubsRoError as e:
            raise ProviderDownloadError(str(e), self.name, getattr(e, "status_code", None))

    def get_settings_template(self) -> str:
        return "providers/subsro_form.html"

    def _requested_languages(self, languages: Optional[List[str]]) -> List[str]:
        if not languages:
            return list(_ISO_TO_LANG.values())
        codes = []
        for lang in languages:
            code = _ISO_TO_LANG.get(lang)
            if code:
                codes.append(code)
        return codes

    def _parse_results(self, items: list) -> List[SubtitleResult]:
        results = []
        for item in items:
            sid = item.get("id")
            if not sid:
                continue
            lang_raw = item.get("language", "ro")
            iso_lang = _LANG_TO_ISO.get(lang_raw, lang_raw)
            release = item.get("description") or item.get("title") or "unknown"
            results.append(SubtitleResult(
                provider_name=self.name,
                subtitle_id=str(sid),
                language=iso_lang,
                release_name=release,
                uploader=item.get("translator"),
                metadata={
                    "url": item.get("downloadLink", ""),
                    "hash_match": False,
                    "type": item.get("type"),
                    "year": item.get("year"),
                },
            ))
        return results
