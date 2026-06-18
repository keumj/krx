from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline_krx.refresh_prices import _load_current_listing_from_fdr, _load_latest_naver_dividend_metrics, _normalize_symbol
from pipeline_krx.db import sync_krx_total_return_indices


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh latest KRX dividend DPS/yield from Naver into shared SQLite.")
    parser.add_argument("--db-path", default="data/krx_shared_db/krx_shared_prices.sqlite", help="Shared SQLite DB path.")
    parser.add_argument("--as-of-date", default="", help="Price date to update. Defaults to latest date in prices.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    db_path = Path(args.db_path)
    listing = _load_current_listing_from_fdr()
    symbols = [_normalize_symbol(value) for value in listing.get("Symbol", pd.Series(dtype=object)).tolist()]
    if not symbols and db_path.exists():
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT symbol
                FROM securities
                WHERE COALESCE(is_active, 1) = 1
                UNION
                SELECT DISTINCT symbol
                FROM prices
                WHERE date = (SELECT MAX(date) FROM prices)
                """
            ).fetchall()
        symbols = [_normalize_symbol(row[0]) for row in rows]
    metrics, metric_date = _load_latest_naver_dividend_metrics(symbols)
    if not metrics:
        print(f"refreshed_krx_dividends changed_snapshot_rows=0 changed_price_rows=0 db_path={db_path}")
        return 0

    with sqlite3.connect(db_path, timeout=60) as conn:
        snapshot_rows = [
            (
                symbol,
                metric_date,
                values.get("dividend_yield"),
                f"naver:marcap:{metric_date}",
            )
            for symbol, values in metrics.items()
            if values.get("dividend_yield") is not None
        ]
        before_snapshot = conn.total_changes
        conn.executemany(
            """
            INSERT INTO fundamentals_snapshot(symbol, as_of_date, dividend_yield, source)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                as_of_date=excluded.as_of_date,
                dividend_yield=excluded.dividend_yield,
                source=excluded.source,
                updated_at=CURRENT_TIMESTAMP
            """,
            snapshot_rows,
        )
        changed_snapshot_rows = int(conn.total_changes - before_snapshot)

        as_of_date = str(args.as_of_date).strip()
        if not as_of_date:
            row = conn.execute("SELECT MAX(date) FROM prices").fetchone()
            as_of_date = str(row[0]) if row and row[0] else metric_date
        before = conn.total_changes
        rows = [
            (
                values.get("dividend_per_share"),
                values.get("dividend_yield"),
                symbol,
                as_of_date,
            )
            for symbol, values in metrics.items()
            if values.get("dividend_per_share") is not None
        ]
        conn.executemany(
            """
            UPDATE prices
            SET
                dividends = ?,
                dividend_yield = ?
            WHERE symbol = ?
              AND date = ?
            """,
            rows,
        )
        conn.commit()
        changed_price_rows = int(conn.total_changes - before)

    print(
        "refreshed_krx_dividends",
        f"symbols={len(metrics)}",
        f"changed_snapshot_rows={changed_snapshot_rows}",
        f"changed_price_rows={changed_price_rows}",
        f"total_return_rows={sync_krx_total_return_indices(db_path=db_path, symbols=list(metrics.keys()))}",
        f"as_of_date={as_of_date}",
        f"db_path={db_path}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
