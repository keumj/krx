from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from pipeline_krx.db import init_krx_project_db, krx_prices_sqlite_path


DEFAULT_SHARED_DB_ROOT = Path(os.getenv("KEUMJ_KRX_DB_DIR", "data/krx_shared_db"))
DEFAULT_SQLITE_NAME = str(os.getenv("KEUMJ_KRX_DB_SQLITE_NAME", "krx_shared_prices.sqlite")).strip() or "krx_shared_prices.sqlite"
NEWS_ANALYSIS_STATUS_PENDING = "pending"
NEWS_ANALYSIS_STATUS_PROCESSING = "processing"
NEWS_ANALYSIS_STATUS_DONE = "done"
NEWS_ANALYSIS_STATUS_FAILED = "failed"
VALID_NEWS_ANALYSIS_STATUSES = {
    NEWS_ANALYSIS_STATUS_PENDING,
    NEWS_ANALYSIS_STATUS_PROCESSING,
    NEWS_ANALYSIS_STATUS_DONE,
    NEWS_ANALYSIS_STATUS_FAILED,
}
ALLOWED_NEWS_ANALYSIS_TRANSITIONS: dict[str, set[str]] = {
    NEWS_ANALYSIS_STATUS_PENDING: {NEWS_ANALYSIS_STATUS_PROCESSING, NEWS_ANALYSIS_STATUS_FAILED},
    NEWS_ANALYSIS_STATUS_PROCESSING: {
        NEWS_ANALYSIS_STATUS_PENDING,
        NEWS_ANALYSIS_STATUS_DONE,
        NEWS_ANALYSIS_STATUS_FAILED,
    },
    NEWS_ANALYSIS_STATUS_DONE: {NEWS_ANALYSIS_STATUS_PROCESSING},
    NEWS_ANALYSIS_STATUS_FAILED: {NEWS_ANALYSIS_STATUS_PENDING, NEWS_ANALYSIS_STATUS_PROCESSING},
}


@dataclass(frozen=True)
class NewsArticleRow:
    id: int
    ticker: str
    publish_date: str
    title: str
    link: str
    source: str
    sentiment_score: float | None
    analysis_status: str


def _normalize_symbol(symbol: object) -> str:
    text = re.sub(r"[^A-Z0-9._-]+", "_", str(symbol or "").strip().upper())
    return text.zfill(6) if text.isdigit() else text


def _resolve_shared_root(shared_db_root: Path | str | None = None) -> Path:
    if shared_db_root is not None:
        return Path(shared_db_root)
    return Path(os.getenv("KEUMJ_KRX_DB_DIR", str(DEFAULT_SHARED_DB_ROOT)))


def shared_prices_csv_dir(shared_db_root: Path | str | None = None) -> Path:
    return _resolve_shared_root(shared_db_root) / "prices"


def shared_prices_sqlite_path(shared_db_root: Path | str | None = None) -> Path:
    explicit = str(os.getenv("KEUMJ_KRX_DB_SQLITE_PATH", "")).strip()
    if explicit:
        return Path(explicit)
    return krx_prices_sqlite_path(_resolve_shared_root(shared_db_root))


def _target_path(shared_db_root: Path | str | None = None, db_path: Path | str | None = None) -> Path:
    return Path(db_path) if db_path is not None else shared_prices_sqlite_path(shared_db_root)


def _ensure_db(target: Path) -> None:
    init_krx_project_db(db_path=target)


def _query(target: Path, query: str, params: list[object]) -> pd.DataFrame:
    with sqlite3.connect(target) as conn:
        return pd.read_sql_query(query, conn, params=params)


def _normalize_symbols(symbols: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        clean = _normalize_symbol(symbol)
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def _metric_pivot(
    symbols: list[str],
    *,
    value_col: str,
    start_date: str | pd.Timestamp,
    end_date: str | pd.Timestamp | None = None,
    shared_db_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> tuple[pd.DataFrame | None, str | None]:
    normalized = _normalize_symbols(symbols)
    if not normalized:
        return None, None

    target = _target_path(shared_db_root, db_path)
    if not target.exists():
        return None, None
    _ensure_db(target)

    start_text = pd.Timestamp(start_date).normalize().strftime("%Y-%m-%d")
    end_text = pd.Timestamp(end_date).normalize().strftime("%Y-%m-%d") if end_date is not None else None
    placeholders = ",".join("?" for _ in normalized)
    query = f"SELECT date, symbol, {value_col} FROM prices WHERE symbol IN ({placeholders}) AND date >= ?"
    params: list[object] = [*normalized, start_text]
    if end_text is not None:
        query += " AND date <= ?"
        params.append(end_text)
    query += f" AND {value_col} IS NOT NULL ORDER BY date, symbol"

    try:
        raw = _query(target, query, params)
    except Exception:
        return None, None
    if raw.empty:
        return None, None

    raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
    raw[value_col] = pd.to_numeric(raw[value_col], errors="coerce")
    raw = raw.dropna(subset=["date", "symbol", value_col])
    if raw.empty:
        return None, None

    pivot = raw.pivot_table(index="date", columns="symbol", values=value_col, aggfunc="last").sort_index()
    cols = [symbol for symbol in normalized if symbol in pivot.columns]
    if not cols:
        return None, None
    pivot = pivot[cols].dropna(how="all")
    return (pivot, f"sqlite:{target.as_posix()}") if not pivot.empty else (None, None)


def load_shared_ohlcv_for_symbol(
    symbol: str,
    *,
    start_date: str | pd.Timestamp | None = None,
    end_date: str | pd.Timestamp | None = None,
    shared_db_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> tuple[pd.DataFrame | None, str | None]:
    symbol_clean = _normalize_symbol(symbol)
    if not symbol_clean:
        return None, None
    target = _target_path(shared_db_root, db_path)
    if not target.exists():
        return None, None
    _ensure_db(target)

    query = "SELECT date, open, high, low, close, volume FROM prices WHERE symbol = ?"
    params: list[object] = [symbol_clean]
    if start_date is not None:
        query += " AND date >= ?"
        params.append(pd.Timestamp(start_date).normalize().strftime("%Y-%m-%d"))
    if end_date is not None:
        query += " AND date <= ?"
        params.append(pd.Timestamp(end_date).normalize().strftime("%Y-%m-%d"))
    query += " ORDER BY date"

    try:
        raw = _query(target, query, params)
    except Exception:
        return None, None
    if raw.empty:
        return None, None

    raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
    for col in ["open", "high", "low", "close", "volume"]:
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    raw = raw.dropna(subset=["date", "open", "high", "low", "close"]).sort_values("date")
    if raw.empty:
        return None, None
    out = raw.set_index(raw["date"].dt.normalize())[["open", "high", "low", "close", "volume"]]
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out, f"sqlite:{target.as_posix()}"


def load_shared_close_prices_for_symbols(
    symbols: list[str],
    *,
    start_date: str | pd.Timestamp,
    end_date: str | pd.Timestamp | None = None,
    shared_db_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> tuple[pd.DataFrame | None, str | None]:
    return _metric_pivot(symbols, value_col="close", start_date=start_date, end_date=end_date, shared_db_root=shared_db_root, db_path=db_path)


def load_shared_market_caps_for_symbols(
    symbols: list[str],
    *,
    start_date: str | pd.Timestamp,
    end_date: str | pd.Timestamp | None = None,
    shared_db_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> tuple[pd.DataFrame | None, str | None]:
    return _metric_pivot(symbols, value_col="market_cap", start_date=start_date, end_date=end_date, shared_db_root=shared_db_root, db_path=db_path)


def load_shared_adjusted_close_prices_for_symbols(
    symbols: list[str],
    *,
    start_date: str | pd.Timestamp,
    end_date: str | pd.Timestamp | None = None,
    shared_db_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> tuple[pd.DataFrame | None, str | None]:
    return _metric_pivot(symbols, value_col="adj_close", start_date=start_date, end_date=end_date, shared_db_root=shared_db_root, db_path=db_path)


def load_shared_quarterly_fundamentals_for_symbols(
    symbols: list[str],
    *,
    limit_per_symbol: int | None = 4,
    shared_db_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> tuple[pd.DataFrame | None, str | None]:
    normalized = _normalize_symbols(symbols)
    if not normalized:
        return None, None

    target = _target_path(shared_db_root, db_path)
    if not target.exists():
        return None, None
    _ensure_db(target)

    placeholders = ",".join("?" for _ in normalized)
    query = (
        "SELECT symbol, fiscal_date, filing_date, period_type, revenue, operating_income, "
        "net_income, total_assets, total_liabilities, stockholders_equity, operating_cash_flow, "
        "free_cash_flow, capex, diluted_eps, source "
        f"FROM fundamentals_quarterly WHERE symbol IN ({placeholders}) "
        "ORDER BY symbol, fiscal_date DESC"
    )
    try:
        frame = _query(target, query, list(normalized))
    except Exception:
        return None, None
    if frame.empty:
        return None, None

    for col in [
        "revenue",
        "operating_income",
        "net_income",
        "total_assets",
        "total_liabilities",
        "stockholders_equity",
        "operating_cash_flow",
        "free_cash_flow",
        "capex",
        "diluted_eps",
    ]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    if limit_per_symbol is not None and int(limit_per_symbol) > 0:
        frame = frame.groupby("symbol", as_index=False, group_keys=False).head(int(limit_per_symbol)).reset_index(drop=True)
    return frame, f"sqlite:{target.as_posix()}"


def _normalize_news_analysis_status(value: str) -> str:
    status = str(value or "").strip().lower()
    if status not in VALID_NEWS_ANALYSIS_STATUSES:
        raise ValueError(
            "Invalid news analysis status "
            f"'{value}'. Choose from: {', '.join(sorted(VALID_NEWS_ANALYSIS_STATUSES))}"
        )
    return status


def claim_pending_news_articles_for_analysis(
    limit: int,
    *,
    ticker: str | None = None,
    shared_db_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> list[NewsArticleRow]:
    if int(limit) <= 0:
        raise ValueError("limit must be positive")

    target = _target_path(shared_db_root, db_path)
    if not target.exists() or not target.is_file():
        return []

    with sqlite3.connect(target) as conn:
        _ensure_db(target)
        conn.execute("BEGIN IMMEDIATE")
        query = (
            "SELECT id, symbol, publish_date, title, link, source, sentiment_score, analysis_status "
            "FROM news_articles "
            "WHERE analysis_status = ?"
        )
        params: list[object] = [NEWS_ANALYSIS_STATUS_PENDING]
        ticker_clean = _normalize_symbol(ticker) if ticker else ""
        if ticker_clean:
            query += " AND symbol = ?"
            params.append(ticker_clean)
        query += " ORDER BY publish_date ASC, id ASC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(query, params).fetchall()
        if not rows:
            conn.commit()
            return []
        ids = [int(row[0]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE news_articles SET analysis_status = ? WHERE id IN ({placeholders})",
            [NEWS_ANALYSIS_STATUS_PROCESSING, *ids],
        )
        conn.commit()
    return [
        NewsArticleRow(
            id=int(row[0]),
            ticker=str(row[1]),
            publish_date=str(row[2]),
            title=str(row[3]),
            link=str(row[4]),
            source=str(row[5]),
            sentiment_score=float(row[6]) if row[6] is not None else None,
            analysis_status=NEWS_ANALYSIS_STATUS_PROCESSING,
        )
        for row in rows
    ]


def update_news_article_analysis_status(
    article_id: int,
    new_status: str,
    *,
    expected_current_status: str | None = None,
    shared_db_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> bool:
    status_to = _normalize_news_analysis_status(new_status)
    expected_from = _normalize_news_analysis_status(expected_current_status) if expected_current_status is not None else None

    target = _target_path(shared_db_root, db_path)
    if not target.exists() or not target.is_file():
        return False

    with sqlite3.connect(target) as conn:
        _ensure_db(target)
        row = conn.execute("SELECT analysis_status FROM news_articles WHERE id = ?", (int(article_id),)).fetchone()
        if row is None:
            return False
        current_status = _normalize_news_analysis_status(str(row[0]))
        if expected_from is not None and current_status != expected_from:
            return False
        if status_to != current_status and status_to not in ALLOWED_NEWS_ANALYSIS_TRANSITIONS.get(current_status, set()):
            raise ValueError(f"Invalid news analysis status transition: {current_status} -> {status_to}")
        before = conn.total_changes
        conn.execute("UPDATE news_articles SET analysis_status = ? WHERE id = ?", (status_to, int(article_id)))
        conn.commit()
        return conn.total_changes > before


def mark_news_article_analysis_done(
    article_id: int,
    *,
    shared_db_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> bool:
    return update_news_article_analysis_status(
        article_id,
        NEWS_ANALYSIS_STATUS_DONE,
        expected_current_status=NEWS_ANALYSIS_STATUS_PROCESSING,
        shared_db_root=shared_db_root,
        db_path=db_path,
    )


def mark_news_article_analysis_failed(
    article_id: int,
    *,
    shared_db_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> bool:
    return update_news_article_analysis_status(
        article_id,
        NEWS_ANALYSIS_STATUS_FAILED,
        expected_current_status=NEWS_ANALYSIS_STATUS_PROCESSING,
        shared_db_root=shared_db_root,
        db_path=db_path,
    )


def main() -> int:
    result = init_krx_project_db(db_path=shared_prices_sqlite_path())
    print(f"Initialized KRX shared SQLite DB: {result.db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
