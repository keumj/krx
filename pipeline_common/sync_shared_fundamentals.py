from __future__ import annotations

import argparse
from pathlib import Path

from .shared_sp500_prices_sql import shared_prices_sqlite_path, sync_shared_fundamentals_snapshot_csv


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync fundamentals snapshot CSV into the shared S&P 500 SQLite DB.")
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
    changed = sync_shared_fundamentals_snapshot_csv(
        csv_path,
        db_path=db_path,
        default_as_of_date=str(args.as_of_date).strip() or None,
        source=f"csv:{csv_path.as_posix()}",
    )
    print(f"Fundamentals sync complete: rows_changed={changed}, csv={csv_path}, db={db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
