"""Base64 codec for provider subtitle ids placed in a URL path."""
import base64


def encode_subtitle_id(value) -> str:
    if not value:
        return ''
    raw = str(value).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def decode_subtitle_id(token: str) -> str:
    """Decode a path segment. If it is not base64, return it unchanged."""
    if not token:
        return token
    padding = '=' * ((4 - len(token) % 4) % 4)
    try:
        return base64.urlsafe_b64decode((token + padding).encode('ascii')).decode('utf-8')
    except Exception:
        return token
