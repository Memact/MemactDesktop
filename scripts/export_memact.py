from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
APP_DIR = Path.home() / "AppData" / "Local" / "memact"
DB_PATH = APP_DIR / "memact.db"
CHROMA_PATH = APP_DIR / "chroma"
DESKTOP_DIR = Path.home() / "Desktop"


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for file_path in path.rglob("*"):
        if file_path.is_file():
            try:
                total += file_path.stat().st_size
            except Exception:
                continue
    return total


def _event_count(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    try:
        connection = sqlite3.connect(db_path)
        try:
            row = connection.execute("SELECT COUNT(*) FROM events").fetchone()
        finally:
            connection.close()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _current_version() -> str:
    version_path = ROOT_DIR / "VERSION"
    try:
        return version_path.read_text(encoding="utf-8").strip() or "v0.4"
    except Exception:
        return "v0.4"


def main() -> int:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    export_dir = DESKTOP_DIR / f"memact_export_{timestamp}"
    export_dir.mkdir(parents=True, exist_ok=True)
    print(f"Creating export at {export_dir}")

    db_export_path = export_dir / "memact.db"
    chroma_export_path = export_dir / "chroma"

    if DB_PATH.exists():
        try:
            shutil.copy2(DB_PATH, db_export_path)
            print(f"Copied database to {db_export_path}")
        except Exception as error:
            print(f"Warning: could not copy database: {error}")
    else:
        print("Warning: memact.db was not found; continuing without it.")

    if CHROMA_PATH.exists():
        try:
            shutil.copytree(CHROMA_PATH, chroma_export_path, dirs_exist_ok=True)
            print(f"Copied Chroma directory to {chroma_export_path}")
        except Exception as error:
            print(f"Warning: could not copy Chroma directory: {error}")
    else:
        print("Warning: chroma directory was not found; continuing without it.")

    manifest = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "memact_version": _current_version(),
        "event_count": _event_count(db_export_path),
        "db_size_bytes": db_export_path.stat().st_size if db_export_path.exists() else 0,
        "chroma_size_bytes": _directory_size(chroma_export_path),
    }
    manifest_path = export_dir / "export_manifest.json"
    try:
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"Wrote manifest to {manifest_path}")
    except Exception as error:
        print(f"Warning: could not write manifest: {error}")

    print(f"Export complete: {export_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
