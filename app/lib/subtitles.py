import zipfile
import os
import tempfile
import chardet
from pysubs2 import SSAFile, load, FormatAutodetectionError
import re
import logging
import aiofiles
from ..lib.ass_to_vtt import convert_ass_file_to_vtt_string, convert_ass_string_to_vtt_string, AssParsingError, VttConversionError

logger = logging.getLogger(__name__)

# UTF-8 misread as Latin-1 (common with Spanish subtitles from providers).
_MOJIBAKE_MARKERS = (
    'Ã¡', 'Ã©', 'Ã­', 'Ã³', 'Ãº', 'Ã±', 'Ã¼',
    'Â¿', 'Â¡', 'Ã‰', 'Ã“', 'Ãš', 'Ã‘',
)


def fix_mojibake_text(text: str) -> str:
    """Repair text that was UTF-8 decoded as Latin-1."""
    if not text or not any(marker in text for marker in _MOJIBAKE_MARKERS):
        return text
    try:
        repaired = text.encode('latin-1').decode('utf-8')
        if any(marker in text for marker in _MOJIBAKE_MARKERS):
            return repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return text


def normalize_vtt_content(vtt_content: str) -> str:
    return fix_mojibake_text(vtt_content)


def prepare_vtt_text(vtt_content: str) -> str:
    """Normalize cue text and ensure WEBVTT header (no BOM — Stremio mis-handles BOM)."""
    text = normalize_vtt_content(vtt_content).lstrip('\ufeff')
    if not text.strip().upper().startswith('WEBVTT'):
        text = f'WEBVTT\n\n{text}'
    return text


_VTT_HTTP_ENCODINGS = {
    'utf-8': ('utf-8', 'UTF-8'),
    'utf8': ('utf-8', 'UTF-8'),
    'cp1252': ('cp1252', 'windows-1252'),
    'windows-1252': ('cp1252', 'windows-1252'),
    'iso-8859-1': ('iso-8859-1', 'ISO-8859-1'),
    'latin1': ('iso-8859-1', 'ISO-8859-1'),
}


def encode_vtt_for_http(vtt_content: str) -> tuple[bytes, str]:
    """
    Encode VTT for HTTP response.

    Default cp1252: Stremio mis-detects UTF-8 Western European text as ISO-8859-1 and
    re-encodes it, causing mojibake (Â¿, Ã¡). Single-byte cp1252 survives that pipeline.
    Set VTT_RESPONSE_ENCODING=utf-8 if you need UTF-8 downloads for other tools.
    """
    text = prepare_vtt_text(vtt_content)
    raw = os.environ.get('VTT_RESPONSE_ENCODING', 'cp1252').strip().lower()
    python_enc, http_charset = _VTT_HTTP_ENCODINGS.get(raw, ('utf-8', 'UTF-8'))
    try:
        return text.encode(python_enc), f'text/vtt; charset={http_charset}'
    except UnicodeEncodeError:
        logger.warning(f"Cannot encode VTT as {python_enc}, falling back to UTF-8")
        return text.encode('utf-8'), 'text/vtt; charset=UTF-8'


def decode_subtitle_bytes(data: bytes) -> str:
    encoding = detect_encoding(data)
    return normalize_vtt_content(data.decode(encoding))


def detect_encoding(raw_data):
    """Detect the encoding of subtitle data."""
    if not raw_data:
        return 'utf-8'

    if raw_data.startswith(b'\xef\xbb\xbf'):
        return 'utf-8-sig'

    # Prefer UTF-8 when bytes are valid — chardet often mislabels Spanish UTF-8 as ISO-8859-1.
    try:
        raw_data.decode('utf-8')
        return 'utf-8'
    except UnicodeDecodeError:
        pass

    result = chardet.detect(raw_data)
    encoding = result['encoding']
    confidence = result['confidence']
    
    logger.info(f"Detected encoding: {encoding} with confidence: {confidence}")
    
    # If confidence is low, try common encodings
    if confidence < 0.8:
        common_encodings = [
            'utf-8', 'utf-8-sig',
            'cp1250',       # Polish, Czech, Hungarian
            'cp1251',       # Russian, Bulgarian, Serbian
            'cp1252',       # Western European
            'cp1253',       # Greek
            'cp1254',       # Turkish
            'cp1255',       # Hebrew
            'cp1256',       # Arabic
            'cp874',        # Thai
            'latin1',       # ISO Western
            'iso-8859-1',   # Western European
            'iso-8859-2',   # Central European
            'euc-kr',       # Korean
            'shift_jis',    # Japanese
            'gb2312',       # Chinese Simplified
            'big5',         # Chinese Traditional
        ]
        for enc in common_encodings:
            try:
                raw_data.decode(enc)
                logger.info(f"Successfully decoded with {enc}")
                return enc
            except UnicodeDecodeError:
                continue
    
    return encoding or 'utf-8'


async def convert_to_vtt(file_data, file_extension, encoding=None, fps=None):
    """
    Convert subtitle file to VTT format.
    
    Args:
        file_data: Raw binary data of the subtitle file
        file_extension: File extension (e.g. '.srt', '.ass')
        encoding: Optional encoding to use (None for auto-detection)
        fps: Optional frames per second for frame-based formats
    
    Returns:
        String containing WebVTT content
    """

    if encoding is None:
        encoding = detect_encoding(file_data)
    
    logger.info(f"Converting subtitle with encoding: {encoding}, FPS: {fps}")
    
    # Create a temporary file to work with
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=file_extension)
    temp_file_path = temp_file.name
    
    async with aiofiles.open(temp_file_path, 'wb') as f:
        await f.write(file_data)
    
    try:
        if file_extension.lower() in ('ass', 'ssa'):
            # Use our custom ASS/SSA to VTT converter
            try:
                vtt_content = convert_ass_file_to_vtt_string(temp_file_path, input_encoding=encoding)
                return normalize_vtt_content(vtt_content)
            except (AssParsingError, VttConversionError) as e:
                logger.error(f"Error converting ASS/SSA to VTT: {e}")
                # Fall back to pysubs2 if our converter fails
                subs = load(temp_file_path, encoding=encoding)
                return normalize_vtt_content(subs.to_string('vtt'))
        else:
            # For SRT, SUB, etc. use pysubs2
            from pysubs2.exceptions import UnknownFPSError
            try:
                subs = load(temp_file_path, encoding=encoding, fps=fps)
            except FormatAutodetectionError:
                subs = load(temp_file_path, encoding=encoding, fps=fps, format_=file_extension)
            except UnicodeDecodeError:
                # Re-detect encoding and try again
                logger.warning(f"Failed to decode with {encoding}, re-detecting encoding")
                detected_encoding = detect_encoding(file_data)
                if detected_encoding != encoding:
                    logger.info(f"Re-detected encoding: {detected_encoding}")
                    subs = load(temp_file_path, encoding=detected_encoding, fps=fps)
                else:
                    raise
            except UnknownFPSError as e:
                logger.warning(f"MicroDVD file without FPS, using default 23.976: {e}")
                subs = load(temp_file_path, encoding=encoding, fps=23.976)
            return normalize_vtt_content(subs.to_string('vtt'))
    except Exception as e:
        logger.error(f"Error converting subtitle file: {e}")
        raise
    finally:
        if os.path.exists(temp_file_path):
            os.unlink(temp_file_path)
