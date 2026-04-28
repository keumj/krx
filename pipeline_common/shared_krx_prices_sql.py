from __future__ import annotations

import os
from pathlib import Path

from .shared_sp500_prices_sql import *  # noqa: F401,F403


def _krx_db_path(db_path: Path | str | None = None, shared_db_root: Path | str | None = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    explicit = str(os.getenv("KEUMJ_KRX_DB_SQLITE_PATH", "")).strip()
    if explicit:
        return Path(explicit)
    root = Path(shared_db_root) if shared_db_root is not None else Path(os.getenv("KEUMJ_KRX_DB_DIR", "data/krx_shared_db"))
    return root / "krx_shared_prices.sqlite"


def shared_prices_sqlite_path(shared_db_root: Path | str | None = None) -> Path:
    return _krx_db_path(shared_db_root=shared_db_root)


def load_shared_close_prices_for_symbols(symbols, *, start_date, end_date=None, shared_db_root=None, db_path=None):
    from .shared_sp500_prices_sql import load_shared_close_prices_for_symbols as _load

    return _load(symbols, start_date=start_date, end_date=end_date, shared_db_root=shared_db_root, db_path=_krx_db_path(db_path, shared_db_root))


def load_shared_adjusted_close_prices_for_symbols(symbols, *, start_date, end_date=None, shared_db_root=None, db_path=None):
    from .shared_sp500_prices_sql import load_shared_adjusted_close_prices_for_symbols as _load

    return _load(symbols, start_date=start_date, end_date=end_date, shared_db_root=shared_db_root, db_path=_krx_db_path(db_path, shared_db_root))


def load_shared_dividends_for_symbols(symbols, *, start_date, end_date=None, shared_db_root=None, db_path=None):
    from .shared_sp500_prices_sql import load_shared_dividends_for_symbols as _load

    return _load(symbols, start_date=start_date, end_date=end_date, shared_db_root=shared_db_root, db_path=_krx_db_path(db_path, shared_db_root))


def load_shared_stock_splits_for_symbols(symbols, *, start_date, end_date=None, shared_db_root=None, db_path=None):
    from .shared_sp500_prices_sql import load_shared_stock_splits_for_symbols as _load

    return _load(symbols, start_date=start_date, end_date=end_date, shared_db_root=shared_db_root, db_path=_krx_db_path(db_path, shared_db_root))


def load_shared_market_caps_for_symbols(symbols, *, start_date, end_date=None, shared_db_root=None, db_path=None):
    from .shared_sp500_prices_sql import load_shared_market_caps_for_symbols as _load

    return _load(symbols, start_date=start_date, end_date=end_date, shared_db_root=shared_db_root, db_path=_krx_db_path(db_path, shared_db_root))
