"""Logger persisten untuk jalur live bot.

Semua modul jalur live memakai get_logger() ini (bukan print) supaya:
- Log bertahan di file walau SSH session / process mati (VPS-friendly)
- Ada rotasi otomatis (5 MB x 5 file) supaya disk tidak penuh
- Tetap terlihat di stdout saat dijalankan manual

Pemakaian:
    from src.utils.logger import get_logger
    log = get_logger("engine")
    log.info("... %s", var)

Jalur backtest & tests tetap memakai print (tidak perlu file log).
"""

import logging
import os
from logging.handlers import RotatingFileHandler

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "bot.log")
LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5


def _ensure_base_logger() -> logging.Logger:
    """Pasang handler ke logger base 'hlbot' sekali saja (idempotent)."""
    base = logging.getLogger("hlbot")
    if getattr(base, "_handlers_attached", False):
        return base

    os.makedirs(LOG_DIR, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    base.addHandler(file_handler)
    base.addHandler(stream_handler)
    base.setLevel(logging.INFO)
    base.propagate = False  # hindari duplikasi lewat root logger
    base._handlers_attached = True
    return base


def get_logger(name: str = "") -> logging.Logger:
    """Ambil child logger dari base 'hlbot'. name contoh: 'engine', 'exec'."""
    _ensure_base_logger()
    if name:
        return logging.getLogger(f"hlbot.{name}")
    return logging.getLogger("hlbot")
