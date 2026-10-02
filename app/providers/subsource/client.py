import asyncio
import aiohttp
from typing import List, Dict, Optional
from quart import current_app
from ...version import USER_AGENT

class SubSourceClient:
    BASE_URL = "https://api.subsource.net/api/v1"

    @staticmethod
    def _season_matches(item: Dict, season: int) -> bool:
        for key in ('season', 'seasonNumber', 'season_number'):
            if key not in item or item.get(key) in (None, ''):
                continue
            try:
                return int(item.get(key)) == int(season)
            except (TypeError, ValueError):
                return False
        return False
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {
            'X-API-Key': api_key,
            'User-Agent': USER_AGENT
        }
    
    async def search_movie(self, imdb_id: str = None, query: str = None, season: int = None, content_type: str = None) -> Optional[Dict]:
        """Search for movie/series by IMDB ID or text query"""
        url = f"{self.BASE_URL}/movies/search"
        params = {}
        
        if imdb_id:
            params['searchType'] = 'imdb'
            params['imdb'] = imdb_id
        elif query:
            params['searchType'] = 'text'
            params['q'] = query
        else:
            return None
        
        if season is not None:
            params['season'] = season
        
        if content_type:
            if content_type == 'movie':
                params['type'] = 'movie'
            elif content_type == 'series':
                params['type'] = 'series'
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers, params=params, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status in (502, 503, 504):
                    current_app.logger.debug(f"SubSource API returned {response.status} for {url}")
                    return None  # SubSource API temporarily unavailable
                response.raise_for_status()
                data = await response.json()
                if data.get('success') and data.get('data'):
                    match = data['data'][0]
                    if season is not None and not self._season_matches(match, season):
                        current_app.logger.info(
                            "SubSource season %s did not match %s; retrying without season",
                            season, match.get('season', match.get('seasonNumber')),
                        )
                        return await self.search_movie(
                            imdb_id=imdb_id,
                            query=query,
                            season=None,
                            content_type=content_type,
                        )
                    return match
                return None
    
    async def get_subtitles(self, movie_id: int, language: str = None, page: int = 1, limit: int = 20) -> Dict:
        """Get subtitles for a movie/series"""
        url = f"{self.BASE_URL}/subtitles"
        params = {
            'movieId': movie_id,
            'page': page,
            'limit': limit
        }
        
        if language:
            params['language'] = language
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers, params=params, timeout=aiohttp.ClientTimeout(total=5)) as response:
                response.raise_for_status()
                return await response.json()
    
    async def download_subtitle(self, subtitle_id: int) -> bytes:
        """Download subtitle ZIP file"""
        url = f"{self.BASE_URL}/subtitles/{subtitle_id}/download"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=5)) as response:
                response.raise_for_status()
                # SubSource returns JSON with body field containing ZIP stream
                body = await response.read()
                return body
