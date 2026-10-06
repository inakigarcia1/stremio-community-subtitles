# List of languages for subtitles
# Format: (language_code, language_name) - Using ISO 639-2 codes
LANGUAGES = [
    ('eng', 'English'),
    ('pol', 'Polski'),
    ('spa', 'Español'),
    ('fra', 'Français'),
    ('deu', 'Deutsch'),
    ('ita', 'Italiano'),
    ('por', 'Português'),
    ('pob', 'Português (Brasil)'),
    ('rus', 'Русский'),
    ('jpn', '日本語'),
    ('zho', '中文'),  # ISO 639-2/T for Chinese
    ('kor', '한국어'),
    ('ara', 'العربية'),
    ('hin', 'हिन्दी'),
    ('tur', 'Türkçe'),
    ('nld', 'Nederlands'),
    ('swe', 'Svenska'),
    ('nor', 'Norsk'),
    ('dan', 'Dansk'),
    ('fin', 'Suomi'),
    ('ces', 'Čeština'),
    ('slk', 'Slovenčina'),
    ('hun', 'Magyar'),
    ('ron', 'Română'),
    ('bul', 'Български'),
    ('ell', 'Ελληνικά'),
    ('heb', 'עברית'),
    ('tha', 'ไทย'),
    ('vie', 'Tiếng Việt'),
    ('ind', 'Bahasa Indonesia'),
    ('msa', 'Bahasa Melayu'),    # ISO 639-2/T for Malay
    ('ukr', 'Українська'),
    ('srp', 'Српски'),
    ('hrv', 'Hrvatski'),
    ('slv', 'Slovenščina'),
    ('est', 'Eesti'),
    ('lav', 'Latviešu'),
    ('lit', 'Lietuvių'),
    ('fas', 'فارسی'),  # ISO 639-2/B for Persian (Farsi)
    ('pus', 'پښتو'),  # Pashto
    ('urd', 'اردو'),
    ('ben', 'বাংলা'),
    ('mya', 'မြန်မာ'), # ISO 639-2/T for Burmese
    ('cat', 'Català'),
    ('eus', 'Euskara'),
    ('epo', 'Esperanto'),
    ('mkd', 'Македонски'),
    ('tel', 'తెలుగు'), 
    ('sqi', 'Shqip'),
    ('sin', 'සිංහල')  # Sinhala (Sri Lanka)
]

# Dictionary for quick lookups
LANGUAGE_DICT = dict(LANGUAGES)


def get_language_name(code):
    """Get language name from language code."""
    return LANGUAGE_DICT.get(code, code)


# OpenSubtitles and the Stremio addon use these for Spanish besides ISO 639-2 `spa`.
_SPANISH_CODES = {
    'spa',
    'spl',
    'es',
    'esp',
    'sp',
    'es-es',
    'es-mx',
    'es-419',
    'es-ar',
}


def is_spanish_language(code):
    """True for spa, spl, and codes this app already treats as Spanish."""
    if not code:
        return False
    normalized = str(code).strip().lower().replace('_', '-')
    if normalized in _SPANISH_CODES or normalized.startswith('es-'):
        return True
    if normalized in LANGUAGE_DICT and normalized == 'spa':
        return True
    try:
        from iso639 import Lang
        lang = Lang(normalized)
        return lang.pt3 == 'spa' or lang.pt1 == 'es'
    except Exception:
        return False
