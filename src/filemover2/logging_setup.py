from __future__ import annotations

import logging
from typing import Optional

try:
	from rich.logging import RichHandler
	_HAS_RICH = True
except Exception:  # pragma: no cover
	RichHandler = None  # type: ignore
	_HAS_RICH = False


def setup_logging(level: int = logging.INFO) -> None:
	logger = logging.getLogger()
	if logger.handlers:
		# Already configured
		return
	logger.setLevel(level)
	if _HAS_RICH and RichHandler is not None:
		handler = RichHandler(rich_tracebacks=True, show_time=True, show_level=True, show_path=False)
	else:
		handler = logging.StreamHandler()
	formatter = logging.Formatter(fmt="%(asctime)s %(levelname)s %(name)s: %(message)s")
	if hasattr(handler, "setFormatter"):
		handler.setFormatter(formatter)
	logger.addHandler(handler)
