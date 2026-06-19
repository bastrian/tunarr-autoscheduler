from __future__ import annotations

from typing import Any

ENGLISH_ALIASES = {"en", "eng", "english"}
LANGUAGE_ALIASES = {
    "deu": "de",
    "ger": "de",
    "german": "de",
    "jpn": "ja",
    "japanese": "ja",
}


def normalize_language(value: object) -> str | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    raw = raw.replace("_", "-")
    primary = raw.split("-", 1)[0]
    if primary in ENGLISH_ALIASES or raw in ENGLISH_ALIASES:
        return "en"
    if primary in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[primary]
    if raw in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[raw]
    if len(primary) == 2:
        return primary
    if len(primary) == 3:
        return primary
    return raw


def extract_language_metadata(media_streams: object) -> dict[str, list[str]]:
    audio: set[str] = set()
    subtitles: set[str] = set()
    if not isinstance(media_streams, list):
        return {"audio_languages": [], "subtitle_languages": []}
    for stream in media_streams:
        if not isinstance(stream, dict):
            continue
        stream_type = str(stream.get("Type") or "").strip().lower()
        language = normalize_language(
            stream.get("Language")
            or stream.get("LanguageCode")
            or stream.get("DisplayLanguage")
        )
        if language is None:
            continue
        if stream_type == "audio":
            audio.add(language)
        elif stream_type == "subtitle":
            subtitles.add(language)
    return {
        "audio_languages": sorted(audio),
        "subtitle_languages": sorted(subtitles),
    }


def has_english_audio(metadata: dict[str, Any]) -> bool:
    return "en" in _language_set(metadata.get("audio_languages"))


def has_english_subtitles(metadata: dict[str, Any]) -> bool:
    return "en" in _language_set(metadata.get("subtitle_languages"))


def _language_set(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        normalized
        for item in value
        if (normalized := normalize_language(item)) is not None
    }
