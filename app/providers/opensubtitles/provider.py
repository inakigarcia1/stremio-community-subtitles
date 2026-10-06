"""OpenSubtitles provider implementation"""
import os
from typing import List, Dict, Optional, Any

import aiohttp
from quart import current_app
from iso639 import Lang

from ..base import BaseSubtitleProvider, SubtitleResult, ProviderAuthError, ProviderSearchError, ProviderDownloadError
from ...lib.anime_episode_index import absolute_episode, filter_absolute_response
from . import client as opensubtitles_client

_season_counts = {}


async def _cinemeta_season_counts(imdb_id):
    imdb = imdb_id if str(imdb_id).startswith("tt") else f"tt{imdb_id}"
    cached = _season_counts.get(imdb)
    if cached is not None:
        return cached
    url = f"https://v3-cinemeta.strem.io/meta/series/{imdb}.json"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
    except Exception as exc:
        current_app.logger.info("OpenSubtitles absolute retry skipped, cinemeta failed for %s: %s", imdb, exc)
        return None
    counts = {}
    for video in (payload.get("meta") or {}).get("videos") or []:
        try:
            season = int(video.get("season") or 0)
        except (TypeError, ValueError):
            continue
        if season < 0:
            continue
        counts[season] = counts.get(season, 0) + 1
    _season_counts[imdb] = counts
    return counts


class OpenSubtitlesProvider(BaseSubtitleProvider):
    """OpenSubtitles.com subtitle provider"""
    
    name = 'opensubtitles'
    display_name = 'OpenSubtitles.com'
    badge_color = 'warning'
    requires_auth = True
    supports_search = True
    supports_hash_matching = True
    
    async def authenticate(self, user, credentials: Dict[str, str]) -> Dict[str, Any]:
        """Authenticate with OpenSubtitles"""
        username = credentials.get('username')
        password = credentials.get('password')
        
        if not username or not password:
            raise ProviderAuthError("Username and password required", self.name)
        
        try:
            result = await opensubtitles_client.login(username, password, user)
            import time
            return {
                'token': result['token'],
                'base_url': result['base_url'],
                'active': True,
                'username': username,
                'password': password,  # Store for auto-refresh
                'token_timestamp': int(time.time())
            }
        except opensubtitles_client.OpenSubtitlesError as e:
            raise ProviderAuthError(str(e), self.name, getattr(e, 'status_code', None))
    
    async def logout(self, user) -> bool:
        """Logout from OpenSubtitles"""
        creds = await self.get_credentials(user)
        if not creds:
            return True
        
        try:
            # Create temporary user object with old-style attributes for backward compatibility
            class TempUser:
                def __init__(self, token, base_url):
                    self.opensubtitles_token = token
                    self.opensubtitles_base_url = base_url
            
            temp_user = TempUser(creds['token'], creds['base_url'])
            await opensubtitles_client.logout(creds['token'], temp_user)
            return True
        except Exception as e:
            current_app.logger.error(f"OpenSubtitles logout error: {e}")
            return False
    
    async def is_authenticated(self, user) -> bool:
        """Check if user has valid OpenSubtitles credentials.
        This is a fast, non-blocking check — no network requests.
        Token refresh happens lazily on first actual API usage (search/download)."""
        creds = await self.get_credentials(user)
        if not creds or not creds.get('active') or not creds.get('username') or not creds.get('password'):
            return False
        
        # Just check if we have a token — don't try to refresh here
        # Refresh will happen on demand in search/download methods
        if not creds.get('token'):
            return False
        
        return True
    
    async def ensure_fresh_token(self, user) -> bool:
        """Ensure token is fresh before making API calls. Call this before search/download.
        Returns True if token is valid, False if refresh failed."""
        creds = await self.get_credentials(user)
        if not creds or not creds.get('active'):
            return False
        
        import time
        token_age = time.time() - creds.get('token_timestamp', 0)
        if token_age <= (46 * 3600):  # Token still fresh (< 46 hours)
            return True
        
        # Skip refresh if we recently failed (5 min cooldown)
        last_fail = creds.get('_refresh_failed_at', 0)
        if time.time() - last_fail < 300:
            return False
        
        current_app.logger.info(f"OpenSubtitles token expired (age: {token_age/3600:.1f}h), refreshing...")
        try:
            await self._refresh_token(user, creds)
            return True
        except Exception as e:
            if '429' in str(e):
                current_app.logger.debug(f"OpenSubtitles token refresh rate limited for user {user.id}")
            else:
                current_app.logger.warning(f"Failed to refresh OpenSubtitles token: {e}")
            creds['_refresh_failed_at'] = int(time.time())
            await self.save_credentials(user, creds)
            return False
    
    async def check_token_validity(self, user) -> bool:
        """Check if OpenSubtitles token is still valid by making API request.
        Returns True if valid, False if expired/invalid."""
        if not await self.is_authenticated(user):
            return False
        
        creds = await self.get_credentials(user)
        
        # Create temp user for client
        class TempUser:
            def __init__(self, token, base_url):
                self.opensubtitles_token = token
                self.opensubtitles_base_url = base_url
        
        temp_user = TempUser(creds['token'], creds['base_url'])
        
        # Check token validity
        is_valid = await opensubtitles_client.get_user_info(temp_user)
        
        # If token is invalid (401), try to refresh and check again
        if not is_valid:
            current_app.logger.info(f"OpenSubtitles token invalid, attempting token refresh...")
            try:
                await self._refresh_token(user, creds)
                # Check again with new token
                temp_user = TempUser(creds['token'], creds['base_url'])
                return await opensubtitles_client.get_user_info(temp_user)
            except Exception as refresh_error:
                current_app.logger.error(f"Token refresh failed: {refresh_error}")
                return False
        
        return is_valid
    
    async def _refresh_token(self, user, creds: Dict[str, Any]) -> None:
        """Refresh OpenSubtitles token using stored credentials"""
        username = creds.get('username')
        password = creds.get('password')
        
        if not username or not password:
            raise ProviderAuthError("Missing stored credentials for token refresh", self.name)
        
        try:
            result = await opensubtitles_client.login(username, password, user)
            import time
            
            # Update credentials with new token
            creds['token'] = result['token']
            creds['base_url'] = result['base_url']
            creds['token_timestamp'] = int(time.time())
            
            # Save updated credentials
            await self.save_credentials(user, creds)
            current_app.logger.info(f"OpenSubtitles token refreshed successfully for user {user.id}")
        except opensubtitles_client.OpenSubtitlesError as e:
            raise ProviderAuthError(f"Token refresh failed: {str(e)}", self.name, getattr(e, 'status_code', None))

    def _credentials_for_406_relogin(self, creds: Dict[str, Any]):
        """Login pair for a dead bearer on download.

        Production keeps the account in OPENSUBTITLES_USERNAME / OPENSUBTITLES_PASSWORD.
        Use those when they belong to this user. Any other account keeps its stored pair,
        which is what 401/403 refresh already does.
        """
        env_username = (os.environ.get("OPENSUBTITLES_USERNAME") or "").strip()
        env_password = (os.environ.get("OPENSUBTITLES_PASSWORD") or "").strip()
        stored_username = (creds.get("username") or "").strip()
        stored_password = creds.get("password") or ""
        if isinstance(stored_password, str):
            stored_password = stored_password.strip()

        if env_username and env_password and (not stored_username or stored_username == env_username):
            return env_username, env_password
        return stored_username, stored_password

    async def _relogin_after_406(self, user, creds: Dict[str, Any]) -> None:
        """Drop the bearer that OpenSubtitles rejected and log in once."""
        username, password = self._credentials_for_406_relogin(creds)
        if not username or not password:
            raise ProviderAuthError("Missing credentials for OpenSubtitles re-login", self.name)

        previous_token = creds.get("token")
        previous_username = creds.get("username")
        previous_password = creds.get("password")
        creds["username"] = username
        creds["password"] = password
        creds["token"] = None
        await self.save_credentials(user, creds)

        try:
            await self._refresh_token(user, creds)
        except Exception:
            creds["token"] = previous_token
            creds["username"] = previous_username
            creds["password"] = previous_password
            await self.save_credentials(user, creds)
            raise

        await self._persist_provider_credentials(user)

    async def _persist_provider_credentials(self, user) -> None:
        """Store the new bearer. Download runs after the user session is already closed."""
        user_id = getattr(user, "id", None)
        if user_id is None:
            return
        try:
            from ...extensions import async_session_maker
        except Exception:
            return
        if async_session_maker is None:
            return
        try:
            from sqlalchemy import select
            from sqlalchemy.orm.attributes import flag_modified
            from ...models import User

            async with async_session_maker() as session:
                result = await session.execute(select(User).filter_by(id=user_id))
                db_user = result.scalar_one_or_none()
                if db_user is None:
                    return
                current = dict(db_user.provider_credentials or {})
                updated = dict(user.provider_credentials or {})
                current[self.name] = updated.get(self.name)
                db_user.provider_credentials = current
                flag_modified(db_user, "provider_credentials")
                await session.commit()
        except Exception as exc:
            current_app.logger.warning(
                "OpenSubtitles credential update was not saved (%s)",
                type(exc).__name__,
            )
    
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
        """Search OpenSubtitles"""
        if not await self.is_authenticated(user):
            raise ProviderSearchError("Not authenticated", self.name)
        
        # Ensure token is fresh before making API call (may do network refresh)
        if not await self.ensure_fresh_token(user):
            raise ProviderSearchError("Token expired and refresh failed", self.name)
        
        creds = await self.get_credentials(user)
        
        # Convert languages to OpenSubtitles format
        os_languages = self._convert_languages(languages) if languages else None
        
        # Build search params
        search_params = {}
        if imdb_id:
            search_params['imdb_id'] = imdb_id
        if query:
            search_params['query'] = query
        if os_languages:
            search_params['languages'] = os_languages
        if video_hash:
            search_params['moviehash'] = video_hash
        if season is not None:
            search_params['season_number'] = season
        if episode is not None:
            search_params['episode_number'] = episode
        if content_type:
            search_params['type'] = 'episode' if content_type == 'series' else content_type
        
        # Create temp user for backward compatibility
        class TempUser:
            def __init__(self, token, base_url):
                self.opensubtitles_token = token
                self.opensubtitles_base_url = base_url
        
        temp_user = TempUser(creds['token'], creds['base_url'])
        
        try:
            results = await opensubtitles_client.search_subtitles(**search_params, user=temp_user)
            parsed = self._parse_results(results, query=query, season=season, episode=episode)
            return await self._maybe_absolute_retry(temp_user, parsed, search_params, query)
        except opensubtitles_client.OpenSubtitlesError as e:
            # If auth error, try to refresh token and retry once
            if getattr(e, 'status_code', None) in (401, 403):
                current_app.logger.info(f"OpenSubtitles auth error during search, attempting token refresh...")
                try:
                    await self._refresh_token(user, creds)
                    # Retry with new token
                    temp_user = TempUser(creds['token'], creds['base_url'])
                    results = await opensubtitles_client.search_subtitles(**search_params, user=temp_user)
                    parsed = self._parse_results(results, query=query, season=season, episode=episode)
                    return await self._maybe_absolute_retry(temp_user, parsed, search_params, query)
                except Exception as refresh_error:
                    current_app.logger.error(f"Token refresh failed: {refresh_error}")
            raise ProviderSearchError(str(e), self.name, getattr(e, 'status_code', None))
    
    async def get_download_url(self, user, subtitle_id: str) -> str:
        """Get download URL for OpenSubtitles subtitle"""
        if not await self.is_authenticated(user):
            raise ProviderDownloadError("Not authenticated", self.name)
        
        # Ensure token is fresh before making API call (may do network refresh)
        if not await self.ensure_fresh_token(user):
            raise ProviderDownloadError("Token expired and refresh failed", self.name)
        
        creds = await self.get_credentials(user)
        
        # Create temp user
        class TempUser:
            def __init__(self, token, base_url):
                self.opensubtitles_token = token
                self.opensubtitles_base_url = base_url
        
        temp_user = TempUser(creds['token'], creds['base_url'])
        
        try:
            result = await opensubtitles_client.request_download_link(int(subtitle_id), temp_user)
            return result.get('link')
        except opensubtitles_client.OpenSubtitlesError as e:
            status_code = getattr(e, 'status_code', None)
            # If auth error, try to refresh token and retry once
            if status_code in (401, 403):
                current_app.logger.info(f"OpenSubtitles auth error during download, attempting token refresh...")
                try:
                    await self._refresh_token(user, creds)
                    # Retry with new token
                    temp_user = TempUser(creds['token'], creds['base_url'])
                    result = await opensubtitles_client.request_download_link(int(subtitle_id), temp_user)
                    return result.get('link')
                except Exception as refresh_error:
                    current_app.logger.error(f"Token refresh failed: {refresh_error}")
            elif status_code == 406:
                # 406 on /download is how a dead bearer shows up. Log in once and retry once.
                current_app.logger.info(
                    "OpenSubtitles download returned 406; discarding the stored bearer and logging in again"
                )
                try:
                    await self._relogin_after_406(user, creds)
                    temp_user = TempUser(creds['token'], creds['base_url'])
                    result = await opensubtitles_client.request_download_link(int(subtitle_id), temp_user)
                    return result.get('link')
                except Exception as refresh_error:
                    current_app.logger.error(
                        "OpenSubtitles download failed again after re-login: %s",
                        refresh_error,
                    )
            raise ProviderDownloadError(str(e), self.name, status_code)
    
    def get_settings_template(self) -> str:
        """Get settings template path"""
        return 'providers/opensubtitles_form.html'
    
    async def _maybe_absolute_retry(self, temp_user, parsed, search_params, query):
        """Anime often has no subs at TVDB SxE, and does at the absolute episode number."""
        if parsed or search_params.get("type") != "episode":
            return parsed
        season = search_params.get("season_number")
        episode = search_params.get("episode_number")
        imdb_id = search_params.get("imdb_id")
        if season is None or episode is None or not imdb_id:
            return parsed
        counts = await _cinemeta_season_counts(imdb_id)
        if not counts:
            return parsed
        absolute = absolute_episode(counts, season, episode)
        if not absolute or (int(season) == 1 and absolute == int(episode)):
            return parsed
        retry_params = {
            "imdb_id": imdb_id,
            "languages": search_params.get("languages"),
            "episode_number": absolute,
            "type": "episode",
        }
        try:
            raw = await opensubtitles_client.search_subtitles(**retry_params, user=temp_user)
        except Exception as exc:
            current_app.logger.info("OpenSubtitles absolute retry failed for %s: %s", imdb_id, exc)
            return parsed
        filtered = filter_absolute_response(raw, counts)
        kept = self._parse_results(filtered, query=query)
        current_app.logger.info(
            "OpenSubtitles absolute retry %s S%sE%s -> E%s kept %s",
            imdb_id, season, episode, absolute, len(kept),
        )
        return kept

    def _convert_languages(self, languages: List[str]) -> str:
        """Convert ISO 639-3 to ISO 639-1 for OpenSubtitles"""
        converted = []
        for lang in languages:
            if lang == 'pob':
                converted.append('pt-br')
            elif lang == 'por':
                converted.append('pt-pt')
            else:
                try:
                    converted.append(Lang(lang).pt1)
                except KeyError:
                    current_app.logger.warning(f"Could not convert language {lang}")
                    converted.append(lang)
        return ','.join(converted)
    
    def _convert_from_provider_language(self, lang_code: str) -> str:
        """Convert OpenSubtitles ISO 639-1 to ISO 639-3"""
        if lang_code.lower() == 'pt-br':
            return 'pob'
        elif lang_code.lower() == 'pt-pt':
            return 'por'
        elif len(lang_code) == 2:
            try:
                return Lang(lang_code).pt3
            except KeyError:
                current_app.logger.warning(f"Could not convert language {lang_code} to ISO 639-3")
                return lang_code
        return lang_code
    
    def _parse_results(self, api_response: Dict, query: Optional[str] = None, season: Optional[int] = None, episode: Optional[int] = None) -> List[SubtitleResult]:
        """Parse OpenSubtitles API response to SubtitleResult objects"""
        results = []
        
        if not api_response or not api_response.get('data'):
            return results
        
        for item in api_response['data']:
            attrs = item.get('attributes', {})
            
            # Filter by parent_title when searching by query (title)
            if query:
                feature_details = attrs.get('feature_details', {})
                if feature_details:
                    parent_title = feature_details.get('parent_title', '')
                    # Check if query is in parent_title (case insensitive)
                    if parent_title and query.lower() not in parent_title.lower():
                        continue
            
            files = attrs.get('files', [])
            
            if not files:
                continue
            
            file_info = files[0]
            file_id = file_info.get('file_id')
            
            if not file_id:
                continue
            
            # Convert language from ISO 639-1 to ISO 639-3
            lang_code = attrs.get('language', '')
            lang_code = self._convert_from_provider_language(lang_code)
            
            results.append(SubtitleResult(
                provider_name=self.name,
                subtitle_id=str(file_id),
                language=lang_code,
                release_name=file_info.get('file_name'),
                uploader=attrs.get('uploader', {}).get('name'),
                download_count=attrs.get('download_count'),
                rating=attrs.get('ratings'),
                hearing_impaired=attrs.get('hearing_impaired', False),
                ai_translated=attrs.get('ai_translated', False) or attrs.get('machine_translated', False),
                fps=attrs.get('fps'),
                forced=attrs.get('foreign_parts_only', False),
                metadata={
                    'hash_match': attrs.get('moviehash_match', False),
                    'url': attrs.get('url'),
                    'original_file_id': file_id
                }
            ))
        
        return results
