from __future__ import annotations

import sqlite3
from pathlib import Path


DB_PATH = Path("data/krx_shared_db/krx_shared_prices.sqlite")


def _fetchone(conn: sqlite3.Connection, query: str) -> tuple[object, ...]:
    row = conn.execute(query).fetchone()
    return tuple(row or ())


def _fetchall(conn: sqlite3.Connection, query: str) -> list[tuple[object, ...]]:
    return [tuple(row) for row in conn.execute(query).fetchall()]


def main() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        latest_date = _fetchone(conn, "SELECT MAX(date) FROM prices")[0]
        print(f"db={DB_PATH}")
        print(f"latest_price_date={latest_date}")
        print(
            "latest_rows_missing_key_fields=",
            _fetchone(
                conn,
                """
                SELECT
                    SUM(market_cap IS NULL),
                    SUM(shares_outstanding IS NULL),
                    SUM(adj_close IS NULL),
                    SUM(dividend_yield IS NULL),
                    COUNT(*)
                FROM prices
                WHERE date = (SELECT MAX(date) FROM prices)
                """,
            ),
        )
        print(
            "latest_rows_calcable_market_cap_from_shares=",
            _fetchone(
                conn,
                """
                SELECT COUNT(*)
                FROM prices
                WHERE date = (SELECT MAX(date) FROM prices)
                  AND market_cap IS NULL
                  AND close IS NOT NULL
                  AND shares_outstanding IS NOT NULL
                """,
            )[0],
        )
        print(
            "symbols_latest_close_newer_than_cap_or_shares=",
            _fetchone(
                conn,
                """
                SELECT COUNT(*)
                FROM (
                    SELECT
                        symbol,
                        MAX(date) AS latest_close,
                        MAX(CASE WHEN market_cap IS NOT NULL THEN date END) AS latest_cap,
                        MAX(CASE WHEN shares_outstanding IS NOT NULL THEN date END) AS latest_shares
                    FROM prices
                    GROUP BY symbol
                )
                WHERE latest_close > COALESCE(latest_cap, '0000-00-00')
                   OR latest_close > COALESCE(latest_shares, '0000-00-00')
                """,
            )[0],
        )
        print(
            "symbols_stale_but_have_some_shares=",
            _fetchone(
                conn,
                """
                SELECT COUNT(*)
                FROM (
                    SELECT
                        symbol,
                        MAX(date) AS latest_close,
                        MAX(CASE WHEN market_cap IS NOT NULL THEN date END) AS latest_cap,
                        MAX(CASE WHEN shares_outstanding IS NOT NULL THEN date END) AS latest_shares
                    FROM prices
                    GROUP BY symbol
                )
                WHERE (
                    latest_close > COALESCE(latest_cap, '0000-00-00')
                    OR latest_close > COALESCE(latest_shares, '0000-00-00')
                )
                  AND latest_shares IS NOT NULL
                """,
            )[0],
        )
        print(
            "market_cap_mismatch_gt_1pct=",
            _fetchone(
                conn,
                """
                SELECT COUNT(*)
                FROM prices
                WHERE market_cap IS NOT NULL
                  AND shares_outstanding IS NOT NULL
                  AND close IS NOT NULL
                  AND close <> 0
                  AND ABS(market_cap - close * shares_outstanding) > ABS(close * shares_outstanding) * 0.01
                """,
            )[0],
        )
        print(
            "latest_market_cap_mismatch_gt_1pct=",
            _fetchone(
                conn,
                """
                SELECT COUNT(*)
                FROM prices
                WHERE date = (SELECT MAX(date) FROM prices)
                  AND market_cap IS NOT NULL
                  AND shares_outstanding IS NOT NULL
                  AND close IS NOT NULL
                  AND close <> 0
                  AND ABS(market_cap - close * shares_outstanding) > ABS(close * shares_outstanding) * 0.01
                """,
            )[0],
        )
        print(
            "fundamentals_eps_missing_but_calcable=",
            _fetchone(
                conn,
                """
                SELECT COUNT(*)
                FROM fundamentals_quarterly
                WHERE diluted_eps IS NULL
                  AND net_income IS NOT NULL
                  AND shares_outstanding IS NOT NULL
                  AND shares_outstanding > 0
                """,
            )[0],
        )
        print(
            "fundamentals_dte_calcable=",
            _fetchone(
                conn,
                """
                SELECT COUNT(*)
                FROM fundamentals_quarterly
                WHERE total_liabilities IS NOT NULL
                  AND COALESCE(stockholders_equity, total_assets - total_liabilities) IS NOT NULL
                  AND COALESCE(stockholders_equity, total_assets - total_liabilities) <> 0
                """,
            )[0],
        )
        print(
            "fundamentals_snapshot_counts=",
            _fetchone(
                conn,
                """
                SELECT
                    COUNT(*),
                    SUM(per IS NOT NULL),
                    SUM(pbr IS NOT NULL),
                    SUM(eps IS NOT NULL),
                    SUM(dividend_yield IS NOT NULL)
                FROM fundamentals_snapshot
                """,
            ),
        )
        print(
            "latest_missing_market_cap_sample=",
            _fetchall(
                conn,
                """
                SELECT p.symbol, s.name_kr, s.market, p.close, p.market_cap, p.shares_outstanding
                FROM prices AS p
                LEFT JOIN securities AS s
                    ON s.symbol = p.symbol
                WHERE p.date = (SELECT MAX(date) FROM prices)
                  AND (p.market_cap IS NULL OR p.shares_outstanding IS NULL)
                ORDER BY p.symbol
                LIMIT 20
                """,
            ),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
