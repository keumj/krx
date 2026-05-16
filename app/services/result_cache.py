from __future__ import annotations

import os
import pickle
import threading
from pathlib import Path
from typing import TypeVar


T = TypeVar("T")

_CACHE_DIR = Path(os.getenv("APP_RESULT_CACHE_DIR", "data/app_state"))
_LOCK = threading.RLock()


def load_pickle(name: str, default: T) -> T:
    path = _CACHE_DIR / name
    try:
        with path.open("rb") as fh:
            return pickle.load(fh)
    except Exception:
        return default


def save_pickle(name: str, payload: object) -> bool:
    path = _CACHE_DIR / name
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tmp_path.open("wb") as fh:
                pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp_path, path)
        return True
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False
