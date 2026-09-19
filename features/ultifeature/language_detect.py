"""
utils/language_detect.py
─────────────────────────
Jarvis v2.0 — Language Detector (PRD Section 4.4)

Detects Tamil, Telugu, Hindi, and other supported languages.
Works at the semantic router level — no user config needed.

Uses langdetect library if available, else Unicode block analysis fallback.
"""

from __future__ import annotations


SUPPORTED_LANGUAGES = {
    "ta": "Tamil",
    "te": "Telugu",
    "hi": "Hindi",
    "en": "English",
    "ml": "Malayalam",
    "kn": "Kannada",
}

# Tamil unicode range
TAMIL_RANGE = (0x0B80, 0x0BFF)
TELUGU_RANGE = (0x0C00, 0x0C7F)
HINDI_RANGE = (0x0900, 0x097F)
MALAYALAM_RANGE = (0x0D00, 0x0D7F)
KANNADA_RANGE = (0x0C80, 0x0CFF)


class LanguageDetector:
    def __init__(self):
        self._use_langdetect = False
        self._try_load()

    def _try_load(self):
        try:
            from langdetect import detect as ld_detect
            self._ld_detect = ld_detect
            self._use_langdetect = True
            print("[ Lang ] langdetect ready")
        except ImportError:
            print("[ Lang ] langdetect not installed — using Unicode fallback")

    def detect(self, text: str) -> str:
        """
        Detect language of text.
        Returns ISO 639-1 code: 'en', 'ta', 'te', 'hi', etc.
        """
        if not text or len(text.strip()) < 3:
            return "en"

        # Unicode range check first (handles script-based detection perfectly)
        unicode_result = self._unicode_detect(text)
        if unicode_result != "en":
            return unicode_result

        # For Latin-script inputs, try langdetect
        if self._use_langdetect:
            try:
                lang = self._ld_detect(text)
                # Map to our supported list
                return lang if lang in SUPPORTED_LANGUAGES else "en"
            except Exception:
                pass

        return "en"

    def _unicode_detect(self, text: str) -> str:
        """Detect by Unicode block analysis."""
        scores = {"ta": 0, "te": 0, "hi": 0, "ml": 0, "kn": 0}

        for char in text:
            cp = ord(char)
            if TAMIL_RANGE[0] <= cp <= TAMIL_RANGE[1]: scores["ta"] += 1
            elif TELUGU_RANGE[0] <= cp <= TELUGU_RANGE[1]: scores["te"] += 1
            elif HINDI_RANGE[0] <= cp <= HINDI_RANGE[1]: scores["hi"] += 1
            elif MALAYALAM_RANGE[0] <= cp <= MALAYALAM_RANGE[1]: scores["ml"] += 1
            elif KANNADA_RANGE[0] <= cp <= KANNADA_RANGE[1]: scores["kn"] += 1

        max_lang = max(scores, key=scores.get)
        max_score = scores[max_lang]

        if max_score > 0:
            return max_lang
        return "en"

    def get_language_name(self, code: str) -> str:
        return SUPPORTED_LANGUAGES.get(code, "Unknown")