from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import BinaryIO


def configure_launcher_logger(log_path: Path, *, max_bytes: int, backups: int) -> logging.Logger:
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"infinite_canvas_launcher:{log_path.resolve()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def rotate_child_log(log_path: Path, *, max_bytes: int, backups: int) -> None:
    log_path = Path(log_path)
    if not log_path.exists() or log_path.stat().st_size <= max_bytes:
        return
    oldest = log_path.with_name(f"{log_path.name}.{backups}")
    oldest.unlink(missing_ok=True)
    for index in range(backups - 1, 0, -1):
        source = log_path.with_name(f"{log_path.name}.{index}")
        target = log_path.with_name(f"{log_path.name}.{index + 1}")
        if source.exists():
            source.replace(target)
    log_path.replace(log_path.with_name(f"{log_path.name}.1"))


def open_child_log(log_path: Path, *, max_bytes: int, backups: int) -> BinaryIO:
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    rotate_child_log(log_path, max_bytes=max_bytes, backups=backups)
    return log_path.open("ab", buffering=0)
