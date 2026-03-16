from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


APP_DIR = Path.home() / "AppData" / "Local" / "memact"


def configure_logging(level: int = logging.INFO) -> None:
    log_dir = APP_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "memact.log"

    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == log_path:
            return

    handler = RotatingFileHandler(
        log_path,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)
    root.setLevel(level)
    root.addHandler(handler)
    logging.captureWarnings(True)
