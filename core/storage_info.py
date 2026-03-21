from __future__ import annotations

import sqlite3
from pathlib import Path


APP_DIR = Path.home() / "AppData" / "Local" / "memact"


def get_storage_info() -> dict:
    """Return storage breakdown details for the local Memact store."""
    info = {
        "db_bytes": 0,
        "chroma_bytes": 0,
        "logs_bytes": 0,
        "total_bytes": 0,
        "db_path": str(APP_DIR / "memact.db"),
        "chroma_path": str(APP_DIR / "chroma"),
        "event_count": 0,
    }
    try:
        db_path = APP_DIR / "memact.db"
        if db_path.exists():
            info["db_bytes"] = db_path.stat().st_size
        chroma_path = APP_DIR / "chroma"
        if chroma_path.exists():
            info["chroma_bytes"] = sum(
                file_path.stat().st_size
                for file_path in chroma_path.rglob("*")
                if file_path.is_file()
            )
        logs_path = APP_DIR / "logs"
        if logs_path.exists():
            info["logs_bytes"] = sum(
                file_path.stat().st_size
                for file_path in logs_path.rglob("*")
                if file_path.is_file()
            )
        info["total_bytes"] = (
            info["db_bytes"] + info["chroma_bytes"] + info["logs_bytes"]
        )
        if db_path.exists():
            connection = sqlite3.connect(db_path)
            try:
                row = connection.execute("SELECT COUNT(*) AS c FROM events").fetchone()
            finally:
                connection.close()
            info["event_count"] = int(row[0]) if row else 0
    except Exception:
        pass
    return info


def format_bytes(b: int) -> str:
    """Return a human-readable byte count."""
    if b >= 1_000_000_000:
        return f"{b / 1_000_000_000:.1f} GB"
    if b >= 1_000_000:
        return f"{b / 1_000_000:.1f} MB"
    if b >= 1_000:
        return f"{b / 1_000:.1f} KB"
    return f"{b} B"
