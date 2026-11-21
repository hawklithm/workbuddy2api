"""Trae authentication and device diagnostics, independent of host applications."""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

_logger: logging.Logger | None = None
_log_directory: Path | None = None


def configure_log_directory(directory: Path | None) -> None:
    global _log_directory
    _log_directory = Path(directory) if directory is not None else None


def get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    logger = logging.getLogger('trae2api.debug')
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not logger.handlers:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter('[trae2api] %(message)s'))
        logger.addHandler(stream)
        try:
            directory = _log_directory or Path.cwd() / 'logs'
            directory.mkdir(parents=True, exist_ok=True)
            output = logging.FileHandler(directory / f"debug-{time.strftime('%Y%m%d')}.log", encoding='utf-8')
            output.setFormatter(logging.Formatter('%(message)s'))
            logger.addHandler(output)
        except OSError:
            pass
    _logger = logger
    return logger
