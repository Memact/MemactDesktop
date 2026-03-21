from __future__ import annotations

import json
import shutil
from pathlib import Path


APP_DIR = Path.home() / "AppData" / "Local" / "memact"
LEGACY_APP_DIR = Path.home() / "AppData" / "Local" / "Memact"
SETTINGS_PATH = APP_DIR / "settings.json"


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        legacy_path = LEGACY_APP_DIR / "settings.json"
        if legacy_path.exists():
            APP_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_path, SETTINGS_PATH)
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(settings: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(settings, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_excluded_apps() -> set[str]:
    """Return lowercase exe names to exclude from capture."""
    settings = load_settings()
    raw = settings.get("excluded_apps") or []
    if not isinstance(raw, list):
        return set()
    return {str(item).strip().lower() for item in raw if str(item).strip()}


def save_excluded_apps(apps: set[str]) -> None:
    settings = load_settings()
    settings["excluded_apps"] = sorted(str(app).strip().lower() for app in apps if str(app).strip())
    save_settings(settings)
