from __future__ import annotations

import logging
from typing import Optional
from pathlib import Path
from logging.handlers import RotatingFileHandler
import os

try:
	from rich.logging import RichHandler
	_HAS_RICH = True
except Exception:  # pragma: no cover
	RichHandler = None  # type: ignore
	_HAS_RICH = False

LOG_DIR = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming") / "FileMover2" / "logs"
LOG_FILE = LOG_DIR / "filemover2.log"


def get_log_file() -> Path:
	LOG_DIR.mkdir(parents=True, exist_ok=True)
	return LOG_FILE


def setup_logging(level: int = logging.INFO) -> None:
	logger = logging.getLogger()
	if logger.handlers:
		# Already configured
		return
	logger.setLevel(level)
	# Console handler
	if _HAS_RICH and RichHandler is not None:
		handler = RichHandler(rich_tracebacks=True, show_time=True, show_level=True, show_path=False)
	else:
		handler = logging.StreamHandler()
	formatter = logging.Formatter(fmt="%(asctime)s %(levelname)s %(name)s: %(message)s")
	if hasattr(handler, "setFormatter"):
		handler.setFormatter(formatter)
	logger.addHandler(handler)
	# Rotating file handler
	LOG_DIR.mkdir(parents=True, exist_ok=True)
	file_handler = RotatingFileHandler(get_log_file(), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
	file_handler.setFormatter(logging.Formatter(fmt="%(asctime)s %(levelname)s %(name)s: %(message)s"))
	logger.addHandler(file_handler)
