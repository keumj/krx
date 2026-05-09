from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pandas as pd

from pipeline_krx.db import init_krx_project_db

from .shared_krx_prices_sql import shared_prices_sqlite_path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync fundamentals snapshot CSV into the shared KRX SQLite DB.")
    parser.add_argument(
        "--csv-path",
        default="data/shared_fundamentals_snapshot.csv",
        help="CSV path containing symbol/ticker and ROE/PER/PBR columns",
    )
    parser.add_argument("--db-path", default="", help="Optional shared SQLite path override")
    parser.add_argument("--as-of-date", default="", help="Fallback as_of_date if CSV does not include one")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    csv_path = Path(args.csv_path)
    db_path = Path(args.db_path) if str(args.db_path).strip() else shared_prices_sqlite_path()
    frame = pd.read_csv(csv_path)
    cols = {str(col).strip().lower(): col for col in frame.columns}
    symbol_col = cols.get("symbol") or cols.get("ticker")
    if symbol_col is None:
        raise ValueError("fundamentals CSV must include a symbol or ticker column")
    as_of_col = cols.get("as_of_date") or cols.get("date")
    default_as_of = str(args.as_of_date).strip() or pd.Timestamp.today().normalize().strftime("%Y-%m-%d")

    rows: list[tuple[object, ...]] = []
    for _, row in frame.iterrows():
        symbol = str(row.get(symbol_col) or "").strip().upper()
        if not symbol:
            continue
        as_of_date = str(row.get(as_of_col) if as_of_col else default_as_of).strip() or default_as_of
        rows.append(
            (
                symbol.zfill(6) if symbol.isdigit() else symbol,
                as_of_date,
                row.get(cols.get("market")) if cols.get("market") else None,
                row.get(cols.get("per")) if cols.get("per") else None,
                row.get(cols.get("pbr")) if cols.get("pbr") else None,
                row.get(cols.get("roe")) if cols.get("roe") else None,
                row.get(cols.get("eps")) if cols.get("eps") else None,
                row.get(cols.get("bps")) if cols.get("bps") else None,
                row.get(cols.get("dividend_yield")) if cols.get("dividend_yield") else None,
                row.get(cols.get("shares_outstanding")) if cols.get("shares_outstanding") else None,
                row.get(cols.get("market_cap")) if cols.get("market_cap") else None,
                f"csv:{csv_path.as_posix()}",
            )
        )

    init_krx_project_db(db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT INTO fundamentals_snapshot(
                symbol, as_of_date, market, per, pbr, roe, eps, bps, dividend_yield,
                shares_outstanding, market_cap, source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                as_of_date=excluded.as_of_date,
                market=COALESCE(excluded.market, fundamentals_snapshot.market),
                per=excluded.per,
                pbr=excluded.pbr,
                roe=excluded.roe,
                eps=excluded.eps,
                bps=excluded.bps,
                dividend_yield=excluded.dividend_yield,
                shares_outstanding=excluded.shares_outstanding,
                market_cap=excluded.market_cap,
                source=excluded.source,
                updated_at=CURRENT_TIMESTAMP
            """,
            rows,
        )
        conn.commit()
        changed = conn.total_changes - before
    print(f"Fundamentals sync complete: rows_changed={changed}, csv={csv_path}, db={db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
