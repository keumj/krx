from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def _query(conn: sqlite3.Connection, query: str) -> tuple[object, ...] | None:
    try:
        return conn.execute(query).fetchone()
    except sqlite3.Error as exc:
        print(f"query_error={type(exc).__name__}: {exc}")
        return None


def _print_stock(conn: sqlite3.Connection) -> None:
    row = _query(conn, "SELECT MAX(date), COUNT(*), COUNT(DISTINCT symbol) FROM prices")
    if row:
        print(f"prices: max_date={row[0] or '-'} rows={row[1] or 0} symbols={row[2] or 0}")
    row = _query(conn, "SELECT MAX(date), COUNT(*) FROM prices WHERE market_cap IS NOT NULL")
    if row:
        print(f"market_caps_in_prices: max_date={row[0] or '-'} rows={row[1] or 0}")


def _print_quarterly(conn: sqlite3.Connection) -> None:
    row = _query(conn, "SELECT MAX(fiscal_date), COUNT(*), COUNT(DISTINCT symbol) FROM fundamentals_quarterly")
    if row:
        print(f"fundamentals_quarterly: max_fiscal_date={row[0] or '-'} rows={row[1] or 0} symbols={row[2] or 0}")


def _print_news(conn: sqlite3.Connection) -> None:
    row = _query(conn, "SELECT MAX(publish_date), COUNT(*), COUNT(DISTINCT symbol) FROM news_articles")
    if row:
        print(f"news_articles: max_publish_date={row[0] or '-'} rows={row[1] or 0} symbols={row[2] or 0}")


def _print_macro() -> int:
    db_path = Path.cwd() / "data" / "macro_prices.sqlite"
    print(f"macro_sqlite={db_path}")
    print(f"sqlite_exists={db_path.exists()} size_bytes={db_path.stat().st_size if db_path.exists() else 0}")
    if not db_path.exists():
        return 1
    with sqlite3.connect(db_path) as conn:
        row = _query(conn, "SELECT MIN(date), MAX(date), COUNT(*), COUNT(DISTINCT series_id) FROM macro_series")
        if row:
            print(f"macro_series: min_date={row[0] or '-'} max_date={row[1] or '-'} rows={row[2] or 0} series={row[3] or 0}")
        row = _query(conn, "SELECT series_id, max_date, row_count, source FROM macro_metadata ORDER BY series_id")
        if row:
            print(f"sample_metadata: series_id={row[0]} max_date={row[1] or '-'} rows={row[2] or 0} source={row[3] or '-'}")
    return 0


def main() -> int:
    job = str(sys.argv[1] if len(sys.argv) > 1 else "all").strip().lower()
    if job == "macro":
        return _print_macro()
    db_path = Path.cwd() / "data" / "krx_shared_db" / "krx_shared_prices.sqlite"
    print(f"shared_sqlite={db_path}")
    print(f"sqlite_exists={db_path.exists()} size_bytes={db_path.stat().st_size if db_path.exists() else 0}")
    if not db_path.exists():
        return 1

    with sqlite3.connect(db_path) as conn:
        if job in {"stock", "all"}:
            _print_stock(conn)
        if job in {"quarterly", "all"}:
            _print_quarterly(conn)
        if job in {"news", "all"}:
            _print_news(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
