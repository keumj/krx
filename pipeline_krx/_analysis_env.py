from __future__ import annotations

import os
from pathlib import Path
from types import ModuleType
from typing import Any, Callable


def configure_krx_analysis_environment() -> None:
    root = Path(__file__).resolve().parents[1]
    db_path = root / "data" / "krx_shared_db" / "krx_shared_prices.sqlite"
    components_path = root / "data" / "krx_components_full.csv"
    close_prices_path = root / "data" / "krx_close_prices.csv"
    prices_dir = root / "data" / "krx_shared_db" / "prices"
    os.environ.setdefault("KEUMJ_KRX_DB_SQLITE_PATH", str(db_path))
    os.environ.setdefault("KEUMJ_KRX_DB_DIR", str(db_path.parent))
    os.environ.setdefault("KRX_COMPONENTS_CSV_PATH", str(components_path))
    os.environ.setdefault("KRX_METRICS_CSV_PATH", str(close_prices_path))
    os.environ.setdefault("KRX_PRICES_PANEL_DIR", str(prices_dir))
    try:
        from .db import init_krx_project_db

        init_krx_project_db(db_path=db_path)
    except Exception:
        pass


def krx_text(value: str) -> str:
    return (
        str(value)
        .replace("US large-cap", "KRX")
    )


def install_krx_html_text_replacements(module: ModuleType) -> None:
    for name, value in list(vars(module).items()):
        if not callable(value) or getattr(value, "_krx_text_wrapped", False):
            continue
        if not (name.startswith("_") or name.startswith("render") or name.startswith("launch")):
            continue
        setattr(module, name, _wrap_text_function(value))


def _wrap_text_function(func: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = func(*args, **kwargs)
        if isinstance(result, str):
            return krx_text(result)
        return result

    wrapped.__name__ = getattr(func, "__name__", "wrapped")
    wrapped.__doc__ = getattr(func, "__doc__", None)
    wrapped._krx_text_wrapped = True  # type: ignore[attr-defined]
    return wrapped
