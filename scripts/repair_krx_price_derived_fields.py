from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline_krx.db import repair_krx_price_derived_fields


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill KRX price market cap, shares outstanding, and EPS fields from known DB values."
    )
    parser.add_argument(
        "--db-path",
        default="data/krx_shared_db/krx_shared_prices.sqlite",
        help="Shared KRX SQLite DB path.",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help="Optional symbol to repair. Repeat for multiple symbols.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    symbols = [str(symbol).strip() for symbol in args.symbol if str(symbol).strip()]
    changed = repair_krx_price_derived_fields(
        symbols=symbols or None,
        db_path=Path(args.db_path),
    )
    print(f"repaired_krx_price_derived_fields changed_rows={changed} db_path={args.db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
