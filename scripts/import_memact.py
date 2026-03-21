from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


APP_DIR = Path.home() / "AppData" / "Local" / "memact"
DB_PATH = APP_DIR / "memact.db"
CHROMA_PATH = APP_DIR / "chroma"


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if not args:
        print("Usage: python scripts/import_memact.py <export-folder>")
        return 1

    export_dir = Path(args[0]).expanduser().resolve()
    manifest_path = export_dir / "export_manifest.json"
    if not manifest_path.exists():
        print(f"Export manifest not found at {manifest_path}")
        return 1

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        print(
            "Found export created at "
            f"{manifest.get('exported_at', 'unknown time')} "
            f"from {manifest.get('memact_version', 'unknown version')}."
        )
    except Exception:
        print("Warning: export manifest could not be parsed cleanly, continuing anyway.")

    if DB_PATH.exists() or CHROMA_PATH.exists():
        print("Warning: destination already has Memact data. Import will overwrite matching files.")

    APP_DIR.mkdir(parents=True, exist_ok=True)

    source_db = export_dir / "memact.db"
    if source_db.exists():
        try:
            shutil.copy2(source_db, DB_PATH)
            print(f"Imported database to {DB_PATH}")
        except Exception as error:
            print(f"Warning: could not import database: {error}")
    else:
        print("Warning: no memact.db found in the export folder.")

    source_chroma = export_dir / "chroma"
    if source_chroma.exists():
        try:
            shutil.copytree(source_chroma, CHROMA_PATH, dirs_exist_ok=True)
            print(f"Imported Chroma directory to {CHROMA_PATH}")
        except Exception as error:
            print(f"Warning: could not import Chroma directory: {error}")
    else:
        print("Warning: no chroma directory found in the export folder.")

    print(f"Import complete from {export_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
