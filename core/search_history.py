from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from core.settings import APP_DIR


HISTORY_PATH = APP_DIR / "search_history.json"
MAX_ENTRIES = 200


def _load_raw() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        payload = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    cleaned: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        query = str(item.get("query", "")).strip()
        ts = str(item.get("timestamp", "")).strip()
        if not query or not ts:
            continue
        cleaned.append({"query": query, "timestamp": ts})
    return cleaned


def load_history(limit: int | None = None) -> list[dict]:
    items = _load_raw()
    if limit is None:
        return items
    return items[: max(0, limit)]


def save_history(items: list[dict]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(items, indent=2), encoding="utf-8")


def add_history(query: str) -> None:
    text = str(query or "").strip()
    if not text:
        return
    items = _load_raw()
    key = text.casefold()
    items = [item for item in items if str(item.get("query", "")).casefold() != key]
    items.insert(0, {"query": text, "timestamp": datetime.now().isoformat(timespec="seconds")})
    save_history(items[:MAX_ENTRIES])


def clear_history() -> None:
    save_history([])


def remove_history(query: str) -> None:
    text = str(query or "").strip()
    if not text:
        return
    key = text.casefold()
    items = _load_raw()
    items = [item for item in items if str(item.get("query", "")).casefold() != key]
    save_history(items)
