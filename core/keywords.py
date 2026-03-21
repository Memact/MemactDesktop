from __future__ import annotations

try:
    import yake  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    yake = None


def extract_keyphrases(text: str, max_phrases: int = 12) -> list[str]:
    """Extract top keyphrases from text using YAKE.

    Returns an empty list when the input is empty, YAKE is unavailable,
    or extraction fails for any reason.
    """
    cleaned = str(text or "").strip()
    if not cleaned or max_phrases <= 0 or yake is None:
        return []
    try:
        extractor = yake.KeywordExtractor(
            lan="en",
            n=3,
            top=max_phrases,
            dedupLim=0.9,
        )
        raw_keywords = extractor.extract_keywords(cleaned)
    except Exception:
        return []

    phrases: list[str] = []
    seen: set[str] = set()
    for item in raw_keywords:
        if isinstance(item, tuple):
            phrase = str(item[0]).strip()
        else:
            phrase = str(item).strip()
        key = phrase.casefold()
        if not phrase or key in seen:
            continue
        phrases.append(phrase)
        seen.add(key)
        if len(phrases) >= max_phrases:
            break
    return phrases


def keyphrases_to_text(phrases: list[str]) -> str:
    """Join keyphrases into a single searchable string."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for phrase in phrases or []:
        value = str(phrase).strip()
        key = value.casefold()
        if not value or key in seen:
            continue
        cleaned.append(value)
        seen.add(key)
    return " ".join(cleaned)
