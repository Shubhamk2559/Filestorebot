"""utils/lbpaths.py - the two folders of Local Backup / Recover (created on demand, with a safe fallback).

  tmp_dir()      BK_TMP_DIR   (default /app/backup_tmp)   the ONE part being built; emptied on start-up and after every part
  restore_dir()  RESTORE_DIR  (default /restore)          zip parts to recover from (placed by you or uploaded on the web page)
"""
import logging
import os

import config

log = logging.getLogger(__name__)
_cache: dict = {}


def _ensure(preferred: str, fallback: str) -> str:
    for p in (preferred, fallback):
        try:
            os.makedirs(p, exist_ok=True)
            probe = os.path.join(p, ".w")
            with open(probe, "w") as f:
                f.write("1")
            os.remove(probe)
            return p
        except OSError:
            log.warning("Folder %s is not writable.", p)
    return preferred


def tmp_dir() -> str:
    if "tmp" not in _cache:
        _cache["tmp"] = _ensure(config.BK_TMP_DIR, os.path.join(os.getcwd(), "backup_tmp"))
    return _cache["tmp"]


def restore_dir() -> str:
    if "restore" not in _cache:
        _cache["restore"] = _ensure(config.RESTORE_DIR, os.path.join(os.getcwd(), "restore"))
    return _cache["restore"]
