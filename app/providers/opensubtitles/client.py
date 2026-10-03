import asyncio
import os
import re
import aiohttp
import time
from quart import current_app
import functools # Import functools for lru_cache
import datetime # Import datetime for cache expiration
from ...version import USER_AGENT

# Global base URL for non-authenticated or initial calls like login
GLOBAL_OS_BASE_URL = "https://api.opensubtitles.com/api/v1"

# Common headers — explicitly exclude brotli to avoid intermittent decoding issues with Cloudflare
_COMMON_HEADERS = {
    'Accept-Encoding': 'gzip, deflate',
}

# Custom decorator for time-based LRU cache
def timed_lru_cache(seconds: int, maxsize: int = 128):
    def wrapper_cache(func):
        func = functools.lru_cache(maxsize=maxsize)(func)
        func.expiration = datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)

        @functools.wraps(func)
        def wrapped_func(*args, **kwargs):
            if datetime.datetime.utcnow() >= func.expiration:
                func.cache_clear()
                func.expiration = datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)
            return func(*args, **kwargs)
        return wrapped_func
    return wrapper_cache


def _get_api_key():
    """Safely get API key with proper error handling"""
    api_key = current_app.config.get('OPENSUBTITLES_API_KEY')
    if not api_key:
        raise ValueError("OPENSUBTITLES_API_KEY not found in configuration")
    return api_key


_JSON_SECRET_RE = re.compile(
    r'(?i)("(?:token|api[_-]?key|password|authorization|access_token)"\s*:\s*")(.*?)(")'
)
_ASSIGNMENT_SECRET_RE = re.compile(
    r'(?i)\b(password|api[_-]?key|authorization|token)\b(\s*[=:]\s*)(\S+)'
)
_BEARER_RE = re.compile(r'(?i)\bBearer\s+[A-Za-z0-9\-._~+/=]+')


def redact_sensitive_text(text, secrets=None):
    """Remove bearer tokens, API keys, Authorization values, and passwords from text."""
    if not text:
        return ""

    redacted = text
    unique_secrets = []
    for secret in secrets or ():
        if not isinstance(secret, str):
            continue
        secret = secret.strip()
        if len(secret) < 8 or secret in unique_secrets:
            continue
        unique_secrets.append(secret)
    for secret in sorted(unique_secrets, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")

    redacted = _BEARER_RE.sub("Bearer [REDACTED]", redacted)
    redacted = _JSON_SECRET_RE.sub(r"\1[REDACTED]\3", redacted)
    redacted = _ASSIGNMENT_SECRET_RE.sub(r"\1\2[REDACTED]", redacted)
    return redacted


def _secrets_for_log(*values):
    secrets = list(values)
    secrets.append(os.environ.get("OPENSUBTITLES_PASSWORD"))
    secrets.append(os.environ.get("OPENSUBTITLES_API_KEY"))
    return secrets


class OpenSubtitlesError(Exception):
    """Custom exception for OpenSubtitles API errors."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


# Modified for aiohttp - returns response data directly
async def make_request_with_retry(request_func, max_retries=3, retry_delay=1.0):
    """
    Makes an HTTP request with retry logic for 5xx server errors and 429 rate limits.
    """
    last_exception = None

    for attempt in range(max_retries + 1):
        try:
            data = await request_func()
            return data

        except aiohttp.ClientResponseError as e:
            # 429 Too Many Requests — respect Retry-After header
            if e.status == 429:
                retry_after = 2  # default
                if hasattr(e, 'headers') and e.headers:
                    try:
                        retry_after = int(e.headers.get('Retry-After', 2))
                    except (ValueError, TypeError):
                        pass
                retry_after = min(retry_after, 10)  # cap at 10s
                if attempt < max_retries:
                    current_app.logger.debug(
                        f"OpenSubtitles 429 rate limited "
                        f"(attempt {attempt + 1}/{max_retries + 1}). "
                        f"Waiting {retry_after}s..."
                    )
                    await asyncio.sleep(retry_after)
                    last_exception = e
                    continue
                raise

            # For 5xx server errors, retry
            if 500 <= e.status < 600 and attempt < max_retries:
                current_app.logger.warning(
                    f"OpenSubtitles API returned {e.status} server error "
                    f"(attempt {attempt + 1}/{max_retries + 1}). "
                    f"Retrying in {retry_delay} seconds..."
                )
                await asyncio.sleep(retry_delay)
                last_exception = e
                continue
            raise

        except aiohttp.ClientError as e:
            last_exception = e
            if isinstance(e, aiohttp.ClientResponseError):
                raise
            if attempt < max_retries:
                current_app.logger.warning(
                    f"OpenSubtitles API request failed (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                    f"Retrying in {retry_delay} seconds..."
                )
                await asyncio.sleep(retry_delay)
                continue
            else:
                raise

    if last_exception:
        raise last_exception


async def login(username, password, user=None):
    """
    Logs in to OpenSubtitles.
    API Documentation: https://opensubtitles.stoplight.io/docs/opensubtitles-api/c2NoOjQ4MTA4NzYz-login
    Args:
        username (str): OpenSubtitles username.
        password (str): OpenSubtitles password.
        user (User, optional): The user object. If provided, the user's personal API key will be prioritized.
    Returns:
        dict: Contains token, user info, and base_url from OpenSubtitles.
    Raises:
        OpenSubtitlesError: If API key is missing, login fails, or API returns an error.
    """
    if not username or not password:
        raise OpenSubtitlesError("Username and password are required for login.")

    try:
        api_key = _get_api_key()
    except (ValueError, RuntimeError) as e:
        current_app.logger.error(f"API key error: {e}")
        raise OpenSubtitlesError(f"Configuration error: {e}")

    headers = {
        'Api-Key': api_key,
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'User-Agent': USER_AGENT,
        **_COMMON_HEADERS,
    }
    payload = {
        'username': username,
        'password': password
    }

    async def make_request():
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{GLOBAL_OS_BASE_URL}/login", headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as response:
                response.raise_for_status()
                return await response.json()

    try:
        current_app.logger.info(f"Attempting OpenSubtitles login for user: {username}")
        data = await make_request_with_retry(make_request)

        if 'token' not in data or 'base_url' not in data:
            current_app.logger.error(f"OpenSubtitles login response missing token or base_url: {data}")
            raise OpenSubtitlesError("Login failed: Invalid response from OpenSubtitles.")

        current_app.logger.info(
            f"OpenSubtitles login successful for user: {data.get('user', {}).get('username', username)}. Base URL: {data['base_url']}")
        return data
    except aiohttp.ClientResponseError as e:
        error_message = f"API error: {e.status} - {e.message}"
        
        # Log as warning for client errors (4xx), error for server errors (5xx)
        if 400 <= e.status < 500:
            current_app.logger.warning(f"OpenSubtitles API HTTP error during login: {error_message} | username={username}")
        else:
            current_app.logger.error(f"OpenSubtitles API HTTP error during login: {error_message} | username={username}")
        raise OpenSubtitlesError(error_message, status_code=e.status)
    except aiohttp.ClientError as e:
        current_app.logger.error(f"OpenSubtitles API request error during login: {e}")
        raise OpenSubtitlesError(f"Request failed during login: {e}")
    except ValueError as e:  # Includes JSONDecodeError
        current_app.logger.error(f"OpenSubtitles API JSON decode error during login: {e}")
        raise OpenSubtitlesError(f"Failed to decode API response during login: {e}")


async def logout(token, user):
    """
    Logs out from OpenSubtitles using the user-specific token and base_url from the user object.
    API Documentation: Uses the base_url from login response.
    Args:
        token (str): User's OpenSubtitles JWT token.
        user (User): The user object containing the base_url and API key.
    Returns:
        dict: JSON response from the API, or True if successful with no body.
    Raises:
        OpenSubtitlesError: If API key, token, or base_url are missing/invalid, or API returns an error.
    """
    if not token or not user or not hasattr(user, 'opensubtitles_base_url') or not user.opensubtitles_base_url:
        current_app.logger.error("OpenSubtitles logout: Token, user object, and user's base_url are required.")
        raise OpenSubtitlesError("Token, user object, and user's base_url are required for logout.")

    try:
        api_key = _get_api_key()
    except (ValueError, RuntimeError) as e:
        current_app.logger.error(f"API key error: {e}")
        raise OpenSubtitlesError(f"Configuration error: {e}")

    headers = {
        'Api-Key': api_key,
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
        'User-Agent': USER_AGENT,
        **_COMMON_HEADERS,
    }

    async def make_request():
        async with aiohttp.ClientSession() as session:
            async with session.delete(f"https://{user.opensubtitles_base_url}/api/v1/logout", headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as response:
                response.raise_for_status()
                try:
                    return await response.json()
                except ValueError:
                    return {"status": "success", "message": "Logout successful"}

    try:
        current_app.logger.info(f"Attempting OpenSubtitles logout using base_url: {user.opensubtitles_base_url}")
        data = await make_request_with_retry(make_request)
        current_app.logger.info("OpenSubtitles logout successful.")
        return data
    except aiohttp.ClientResponseError as e:
        error_message = f"API error: {e.status} - {e.message}"
        current_app.logger.error(f"OpenSubtitles API HTTP error during logout: {error_message}")
        raise OpenSubtitlesError(error_message, status_code=e.status)
    except aiohttp.ClientError as e:
        current_app.logger.error(f"OpenSubtitles API request error during logout: {e}")
        raise OpenSubtitlesError(f"Request failed during logout: {e}")


async def search_subtitles(imdb_id=None, query=None, languages=None, moviehash=None,
                     season_number=None, episode_number=None, type=None, user=None):
    """
    Searches for subtitles on OpenSubtitles. Requires user authentication.
    Args:
        imdb_id (int, optional): IMDb ID of the content.
        query (str, optional): Search query.
        languages (str, optional): Comma-separated language codes (e.g., "en,fr").
        moviehash (str, optional): Movie hash for file-based matching.
        season_number (int, optional): Season number for TV shows.
        episode_number (int, optional): Episode number for TV shows.
        type (str, optional): Content type ('movie' or 'episode').
        user (User): The user object containing the token, base_url, and API key.
    """

    if not user or not hasattr(user, 'opensubtitles_token') or not user.opensubtitles_token or \
            not hasattr(user, 'opensubtitles_base_url') or not user.opensubtitles_base_url:
        current_app.logger.error("OpenSubtitles search: user object with token and base_url is required.")
        raise OpenSubtitlesError(
            "User authentication (user object with token and base_url) is required for searching subtitles.")

    try:
        api_key = _get_api_key()
    except (ValueError, RuntimeError) as e:
        current_app.logger.error(f"API key error: {e}")
        raise OpenSubtitlesError(f"Configuration error: {e}")

    headers = {
        'Api-Key': api_key,
        'Authorization': f'Bearer {user.opensubtitles_token}',  # Read token from user object
        'Accept': '*/*',
        'User-Agent': USER_AGENT,
        **_COMMON_HEADERS,
    }

    params = {}
    if imdb_id: params['imdb_id'] = imdb_id
    if query: params['query'] = query
    if languages: params['languages'] = languages
    if moviehash:
        params['moviehash'] = moviehash
        params['moviehash_match'] = 'include'
    if season_number is not None: params['season_number'] = season_number
    if episode_number is not None: params['episode_number'] = episode_number
    if type: params['type'] = type

    if not any(params.values()):
        current_app.logger.warning("OpenSubtitles search called with no effective search parameters.")
        raise OpenSubtitlesError("No search criteria provided for subtitle search.")

    async def make_request():
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://{user.opensubtitles_base_url}/api/v1/subtitles", headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)) as response:
                response.raise_for_status()
                return await response.json()

    try:
        current_app.logger.info(
            f"Searching OpenSubtitles (authenticated) at {user.opensubtitles_base_url}/api/v1/subtitles with params: {params}")
        data = await make_request_with_retry(make_request)
        return data
    except aiohttp.ClientResponseError as e:
        error_message = f"API error: {e.status} - {e.message}"
        current_app.logger.error(f"OpenSubtitles API HTTP error during authenticated search: {error_message} | Request params: {params}")
        raise OpenSubtitlesError(error_message, status_code=e.status)
    except aiohttp.ClientError as e:
        current_app.logger.error(f"OpenSubtitles API request error during authenticated search: {e}")
        raise OpenSubtitlesError(f"Request failed during authenticated search: {e}")
    except ValueError as e:
        current_app.logger.error(f"OpenSubtitles API JSON decode error during authenticated search: {e}")
        raise OpenSubtitlesError(f"Failed to decode API response during authenticated search: {e}")


async def request_download_link(file_id, user=None):
    """
    Requests a download link for a specific subtitle file_id. Requires user authentication.
    Args:
        file_id (int): The OpenSubtitles file ID.
        user (User): The user object containing the base_url and API key.
    """
    if not file_id:
        raise OpenSubtitlesError("file_id is required for download request.")

    if not user or not hasattr(user, 'opensubtitles_token') or not user.opensubtitles_token or \
            not hasattr(user, 'opensubtitles_base_url') or not user.opensubtitles_base_url:
        current_app.logger.error("OpenSubtitles download request: user object with token and base_url is required.")
        raise OpenSubtitlesError(
            "User authentication (user object with token and base_url) is required for download request.")

    try:
        api_key = _get_api_key()
    except (ValueError, RuntimeError) as e:
        current_app.logger.error(f"API key error: {e}")
        raise OpenSubtitlesError(f"Configuration error: {e}")

    headers = {
        'Api-Key': api_key,
        'Authorization': f'Bearer {user.opensubtitles_token}',
        'Content-Type': 'application/json',
        'Accept': '*/*',
        'User-Agent': USER_AGENT,
        **_COMMON_HEADERS,
    }

    payload = {
        'file_id': file_id,
        'sub_format': 'webvtt'
    }

    async def make_request():
        async with aiohttp.ClientSession() as session:
            async with session.post(f"https://{user.opensubtitles_base_url}/api/v1/download", headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as response:
                # OpenSubtitles sometimes answers 406, not 401, when the bearer is dead.
                if response.status == 406:
                    body = ""
                    try:
                        body = await response.text()
                    except Exception as read_error:
                        current_app.logger.warning(
                            "OpenSubtitles download returned HTTP 406 for file_id=%s "
                            "but the response body could not be read (%s)",
                            file_id,
                            type(read_error).__name__,
                        )
                    else:
                        redacted = redact_sensitive_text(
                            body,
                            _secrets_for_log(getattr(user, "opensubtitles_token", None), api_key),
                        )
                        if len(redacted) > 2000:
                            redacted = redacted[:2000] + "...[truncated]"
                        current_app.logger.warning(
                            "OpenSubtitles download returned HTTP 406 for file_id=%s. Response body: %s",
                            file_id,
                            redacted or "<empty>",
                        )
                response.raise_for_status()
                return await response.json()

    try:
        data = await make_request_with_retry(make_request)
        return data
    except aiohttp.ClientResponseError as e:
        error_message = f"API error: {e.status} - {e.message}"
        
        # Log as warning for client errors (4xx), error for server errors (5xx)
        if 400 <= e.status < 500:
            current_app.logger.warning(f"OpenSubtitles API HTTP error during authenticated download request: {error_message} | file_id: {file_id}")
        else:
            current_app.logger.error(f"OpenSubtitles API HTTP error during authenticated download request: {error_message} | file_id: {file_id}")
        raise OpenSubtitlesError(error_message, status_code=e.status)
    except aiohttp.ClientError as e:
        current_app.logger.error(f"OpenSubtitles API request error during authenticated download request: {e}")
        raise OpenSubtitlesError(f"Request failed during authenticated download request: {e}")
    except ValueError as e:
        current_app.logger.error(f"OpenSubtitles API JSON decode error during authenticated download request: {e}")
        raise OpenSubtitlesError(f"Failed to decode API response during authenticated download request: {e}")



async def get_user_info(user):
    """Get user info from OpenSubtitles API to check token validity.
    Returns True if token is valid, False if 401 (expired), raises exception for other errors."""
    if not user or not hasattr(user, 'opensubtitles_token') or not user.opensubtitles_token or \
            not hasattr(user, 'opensubtitles_base_url') or not user.opensubtitles_base_url:
        current_app.logger.debug("OpenSubtitles get_user_info: user object missing token or base_url")
        return False
    
    try:
        api_key = _get_api_key()
    except (ValueError, RuntimeError) as e:
        current_app.logger.error(f"API key error: {e}")
        raise OpenSubtitlesError(f"Configuration error: {e}")
    
    headers = {
        'Api-Key': api_key,
        'Authorization': f'Bearer {user.opensubtitles_token}',
        'Content-Type': 'application/json',
        'Accept': '*/*',
        'User-Agent': USER_AGENT,
        **_COMMON_HEADERS,
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://{user.opensubtitles_base_url}/api/v1/infos/user",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=3)
            ) as response:
                if response.status == 401:
                    current_app.logger.warning(f"OpenSubtitles token expired (401) for base_url={user.opensubtitles_base_url}")
                    return False
                response.raise_for_status()
                return True
    except aiohttp.ClientResponseError as e:
        if e.status == 401:
            current_app.logger.warning(f"OpenSubtitles token expired: {e.status} - {e.message}")
            return False
        current_app.logger.debug(f"OpenSubtitles user info check error: {e.status} - {e.message}")
        return True  # Don't fail on other errors
    except Exception as e:
        current_app.logger.debug(f"OpenSubtitles user info check failed: {e}")
        return True  # Don't fail on network errors
