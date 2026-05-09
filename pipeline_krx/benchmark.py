from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

try:
    from pykrx import stock as pykrx_stock
except Exception:  # pragma: no cover - optional dependency
    pykrx_stock = None

from .db import (
    init_krx_project_db,
    load_latest_krx_benchmark_snapshot,
    upsert_index_constituent_history,
    upsert_krx_benchmark_snapshot,
)


DEFAULT_SOURCE_MODE = "pykrx_index"
DEFAULT_CONSTITUENT_COUNT = 200
DEFAULT_SOURCE_CSV = Path("data/krx_kospi200_manual.csv")
DEFAULT_EXPORT_CSV = Path("data/krx_kospi200_latest.csv")
DEFAULT_BENCHMARK_CODE = "KOSPI200"
DEFAULT_BENCHMARK_NAME = "KOSPI 200"
DEFAULT_INDEX_TICKER = "KOSPI200"
KOSPI200_INDEX_CODE = "1028"


@dataclass(frozen=True)
class KRXBenchmarkSyncResult:
    benchmark_code: str
    as_of_date: str
    constituent_count: int
    sqlite_path: Path
    export_csv_path: Path | None
    source_mode: str


@dataclass(frozen=True)
class KRXIndexHistorySyncResult:
    index_code: str
    start_date: str
    end_date: str
    requested_days: int
    stored_days: int
    stored_rows: int
    sqlite_path: Path
    source_mode: str


def _log(message: str) -> None:
    print(f"[krx-benchmark] {message}", flush=True)


def _normalize_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    if text.isdigit():
        return text.zfill(6)
    return text


def _normalize_date_text(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return pd.Timestamp(text).normalize().strftime("%Y-%m-%d")
    except Exception:
        return None


def _load_as_of_date(conn: sqlite3.Connection, as_of_date: str | None) -> str:
    explicit = _normalize_date_text(as_of_date)
    if explicit:
        return explicit
    row = conn.execute("SELECT MAX(date) FROM prices").fetchone()
    latest = _normalize_date_text(row[0] if row else None)
    if latest is None:
        raise RuntimeError("prices table is empty, so no benchmark as-of date can be derived")
    return latest


def _load_top_kospi_proxy(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    constituent_count: int,
) -> pd.DataFrame:
    frame = pd.read_sql_query(
        """
        WITH latest_price_dates AS (
            SELECT
                p.symbol,
                MAX(p.date) AS latest_date
            FROM prices AS p
            INNER JOIN securities AS s
                ON s.symbol = p.symbol
            WHERE s.market = 'KOSPI'
              AND s.is_active = 1
              AND p.date <= ?
            GROUP BY p.symbol
        )
        SELECT
            s.symbol,
            s.market,
            s.name_kr,
            s.sector,
            s.industry,
            p.date AS reference_date,
            p.market_cap
        FROM latest_price_dates AS l
        INNER JOIN prices AS p
            ON p.symbol = l.symbol
           AND p.date = l.latest_date
        INNER JOIN securities AS s
            ON s.symbol = l.symbol
        WHERE p.market_cap IS NOT NULL
          AND p.market_cap > 0
        ORDER BY p.market_cap DESC, s.symbol ASC
        LIMIT ?
        """,
        conn,
        params=[as_of_date, int(constituent_count)],
    )
    if frame.empty:
        raise RuntimeError("No active KOSPI securities with market caps were found for benchmark sync")
    frame["member_order"] = range(1, len(frame.index) + 1)
    frame["source"] = "top200_proxy"
    return frame


def _load_manual_constituents(path: Path) -> pd.DataFrame:
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"KOSPI200 manual CSV not found: {path}")

    frame = pd.read_csv(path)
    if frame.empty:
        raise RuntimeError(f"KOSPI200 manual CSV is empty: {path}")

    cols = {str(col).strip().lower(): col for col in frame.columns}
    symbol_col = cols.get("symbol")
    if symbol_col is None:
        raise RuntimeError(f"KOSPI200 manual CSV must include Symbol column: {path}")

    normalized = pd.DataFrame()
    normalized["symbol"] = frame[symbol_col].map(_normalize_symbol)
    normalized = normalized[normalized["symbol"] != ""].drop_duplicates(subset=["symbol"], keep="first")
    if normalized.empty:
        raise RuntimeError(f"KOSPI200 manual CSV has no usable symbols: {path}")

    order_col = cols.get("memberorder") or cols.get("member_order")
    notes_col = cols.get("notes")
    fallback_order = pd.Series(range(1, len(normalized.index) + 1), index=normalized.index, dtype="float64")
    normalized["member_order"] = (
        pd.to_numeric(frame.loc[normalized.index, order_col], errors="coerce")
        if order_col is not None
        else fallback_order.copy()
    )
    normalized["member_order"] = normalized["member_order"].fillna(fallback_order).astype(int)
    normalized["notes"] = (
        frame.loc[normalized.index, notes_col].astype(str).str.strip().replace({"nan": "", "None": ""})
        if notes_col is not None
        else ""
    )
    normalized["source"] = "manual_csv"
    return normalized.sort_values(["member_order", "symbol"]).reset_index(drop=True)


def _load_pykrx_index_constituents(*, as_of_date: str) -> pd.DataFrame:
    if pykrx_stock is None:
        raise RuntimeError("pykrx is not installed. Install it with `pip install pykrx` to auto-sync KOSPI200.")

    query_date = str(as_of_date or "").replace("-", "")
    portfolio_raw = None
    try:
        portfolio_raw = pykrx_stock.get_index_portfolio_deposit_file(KOSPI200_INDEX_CODE, query_date)
    except TypeError:
        portfolio_raw = pykrx_stock.get_index_portfolio_deposit_file(KOSPI200_INDEX_CODE)

    if isinstance(portfolio_raw, pd.DataFrame):
        if "티커" in portfolio_raw.columns:
            symbols = portfolio_raw["티커"].tolist()
        elif "symbol" in portfolio_raw.columns:
            symbols = portfolio_raw["symbol"].tolist()
        else:
            symbols = portfolio_raw.index.tolist()
    elif isinstance(portfolio_raw, pd.Series):
        symbols = portfolio_raw.tolist()
    else:
        symbols = list(portfolio_raw or [])

    normalized_symbols = [_normalize_symbol(value) for value in symbols if _normalize_symbol(value)]
    if not normalized_symbols:
        raise RuntimeError("pykrx returned an empty KOSPI200 constituent list")

    frame = pd.DataFrame({"symbol": normalized_symbols})
    frame = frame.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)
    frame["member_order"] = range(1, len(frame.index) + 1)
    frame["source"] = "pykrx_index"
    return frame


def _date_range(start_date: str, end_date: str) -> list[str]:
    start_ts = pd.Timestamp(start_date).normalize()
    end_ts = pd.Timestamp(end_date).normalize()
    if start_ts > end_ts:
        raise ValueError("start_date must be on or before end_date")
    return [ts.strftime("%Y-%m-%d") for ts in pd.date_range(start_ts, end_ts, freq="D")]


def _latest_stored_index_history_date(conn: sqlite3.Connection, index_code: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(as_of_date) FROM index_constituent_history WHERE index_code = ?",
        (index_code,),
    ).fetchone()
    return _normalize_date_text(row[0] if row else None)


def sync_kospi200_constituent_history(
    *,
    db_path: Path | str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    source_mode: str = "pykrx_index",
    skip_empty: bool = True,
) -> KRXIndexHistorySyncResult:
    if source_mode != "pykrx_index":
        raise ValueError("daily constituent history currently supports source_mode='pykrx_index' only")

    sqlite_result = init_krx_project_db(db_path=Path(db_path) if db_path is not None else None)
    with sqlite3.connect(sqlite_result.db_path) as conn:
        final_end = _load_as_of_date(conn, end_date)
        explicit_start = _normalize_date_text(start_date)
        if explicit_start is not None:
            final_start = explicit_start
        else:
            latest_stored = _latest_stored_index_history_date(conn, DEFAULT_BENCHMARK_CODE)
            if latest_stored is not None:
                final_start = (pd.Timestamp(latest_stored) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                final_start = final_end

    dates = _date_range(final_start, final_end)
    stored_rows = 0
    stored_days = 0
    for date_text in dates:
        try:
            frame = _load_pykrx_index_constituents(as_of_date=date_text)
        except Exception as exc:
            if skip_empty:
                _log(f"Skipped {date_text}: {type(exc).__name__}: {exc}")
                continue
            raise
        if frame.empty:
            if skip_empty:
                _log(f"Skipped {date_text}: empty constituent list")
                continue
            raise RuntimeError(f"empty constituent list for {date_text}")
        changed = upsert_index_constituent_history(
            frame,
            index_code=DEFAULT_BENCHMARK_CODE,
            as_of_date=date_text,
            source=source_mode,
            db_path=sqlite_result.db_path,
        )
        stored_rows += int(changed)
        stored_days += 1
        _log(f"Stored constituent list for {date_text}: rows={len(frame.index)}, changes={changed}")

    return KRXIndexHistorySyncResult(
        index_code=DEFAULT_BENCHMARK_CODE,
        start_date=final_start,
        end_date=final_end,
        requested_days=len(dates),
        stored_days=stored_days,
        stored_rows=stored_rows,
        sqlite_path=sqlite_result.db_path,
        source_mode=source_mode,
    )


def _attach_latest_market_caps(
    conn: sqlite3.Connection,
    frame: pd.DataFrame,
    *,
    as_of_date: str,
) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()

    symbols = [_normalize_symbol(value) for value in frame["symbol"].tolist() if _normalize_symbol(value)]
    if not symbols:
        return frame.copy()

    placeholders = ",".join("?" for _ in symbols)
    price_frame = pd.read_sql_query(
        f"""
        WITH latest_price_dates AS (
            SELECT symbol, MAX(date) AS latest_date
            FROM prices
            WHERE symbol IN ({placeholders})
              AND date <= ?
            GROUP BY symbol
        )
        SELECT
            p.symbol,
            p.date AS reference_date,
            p.market_cap,
            s.market,
            s.name_kr,
            s.sector,
            s.industry
        FROM latest_price_dates AS l
        INNER JOIN prices AS p
            ON p.symbol = l.symbol
           AND p.date = l.latest_date
        LEFT JOIN securities AS s
            ON s.symbol = p.symbol
        """,
        conn,
        params=[*symbols, as_of_date],
    )
    return frame.merge(price_frame, on="symbol", how="left")


def _apply_proxy_weights(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()

    weighted = frame.copy()
    weighted["market_cap"] = pd.to_numeric(weighted["market_cap"], errors="coerce")
    positive_caps = weighted["market_cap"].dropna()
    positive_caps = positive_caps[positive_caps > 0]

    if positive_caps.empty:
        weighted["benchmark_weight"] = 1.0 / float(len(weighted.index))
        notes = weighted.get("notes", pd.Series([""] * len(weighted.index), index=weighted.index)).astype(str)
        weighted["notes"] = notes.mask(notes == "", "equal_weight_fallback")
        return weighted

    proxy_cap = float(positive_caps.median())
    weighted["market_cap_proxy"] = weighted["market_cap"].where(weighted["market_cap"] > 0, proxy_cap)
    total_proxy_cap = float(weighted["market_cap_proxy"].sum())
    if total_proxy_cap <= 0:
        raise RuntimeError("Unable to calculate benchmark weights from market caps")
    weighted["benchmark_weight"] = weighted["market_cap_proxy"] / total_proxy_cap

    if "notes" not in weighted.columns:
        weighted["notes"] = ""
    missing_mask = weighted["market_cap"].isna() | (weighted["market_cap"] <= 0)
    if missing_mask.any():
        existing_notes = weighted.loc[missing_mask, "notes"].fillna("").astype(str).str.strip()
        weighted.loc[missing_mask, "notes"] = existing_notes.map(
            lambda value: "market_cap_imputed" if not value else f"{value}; market_cap_imputed"
        )
    return weighted


def sync_kospi200_benchmark(
    *,
    db_path: Path | str | None = None,
    as_of_date: str | None = None,
    source_mode: str = DEFAULT_SOURCE_MODE,
    source_csv: Path = DEFAULT_SOURCE_CSV,
    export_csv_path: Path | None = DEFAULT_EXPORT_CSV,
    constituent_count: int = DEFAULT_CONSTITUENT_COUNT,
) -> KRXBenchmarkSyncResult:
    sqlite_result = init_krx_project_db(db_path=Path(db_path) if db_path is not None else None)
    with sqlite3.connect(sqlite_result.db_path) as conn:
        snapshot_date = _load_as_of_date(conn, as_of_date)
        if source_mode == "pykrx_index":
            base = _load_pykrx_index_constituents(as_of_date=snapshot_date)
            benchmark_frame = _attach_latest_market_caps(conn, base, as_of_date=snapshot_date)
        elif source_mode == "manual_csv":
            base = _load_manual_constituents(source_csv)
            benchmark_frame = _attach_latest_market_caps(conn, base, as_of_date=snapshot_date)
        elif source_mode == "top200_proxy":
            benchmark_frame = _load_top_kospi_proxy(
                conn,
                as_of_date=snapshot_date,
                constituent_count=int(constituent_count),
            )
        else:
            raise ValueError(f"Unsupported source_mode: {source_mode}")

    weighted = _apply_proxy_weights(benchmark_frame)
    stored_rows = upsert_krx_benchmark_snapshot(
        weighted,
        benchmark_code=DEFAULT_BENCHMARK_CODE,
        benchmark_name=DEFAULT_BENCHMARK_NAME,
        as_of_date=snapshot_date,
        weighting_method="market_cap_proxy",
        index_ticker=DEFAULT_INDEX_TICKER,
        source=source_mode,
        db_path=sqlite_result.db_path,
    )
    _log(
        f"Stored {stored_rows} rows for {DEFAULT_BENCHMARK_CODE} "
        f"(source_mode={source_mode}, as_of_date={snapshot_date})"
    )

    latest = load_latest_krx_benchmark_snapshot(
        DEFAULT_BENCHMARK_CODE,
        db_path=sqlite_result.db_path,
    )
    if export_csv_path is not None:
        export_csv_path.parent.mkdir(parents=True, exist_ok=True)
        latest.to_csv(export_csv_path, index=False, encoding="utf-8")
        _log(f"Exported latest snapshot CSV: {export_csv_path}")

    return KRXBenchmarkSyncResult(
        benchmark_code=DEFAULT_BENCHMARK_CODE,
        as_of_date=snapshot_date,
        constituent_count=len(latest.index),
        sqlite_path=sqlite_result.db_path,
        export_csv_path=export_csv_path,
        source_mode=source_mode,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and store the KOSPI200 benchmark snapshot.")
    parser.add_argument(
        "--mode",
        default="snapshot",
        choices=["snapshot", "history"],
        help="snapshot stores one weighted benchmark; history accumulates daily constituent lists only",
    )
    parser.add_argument("--db-path", default="", help="Optional SQLite DB path override")
    parser.add_argument("--as-of-date", default="", help="Benchmark as-of date (YYYY-MM-DD)")
    parser.add_argument("--start-date", default="", help="History mode start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", default="", help="History mode end date (YYYY-MM-DD)")
    parser.add_argument(
        "--source-mode",
        default=DEFAULT_SOURCE_MODE,
        choices=["pykrx_index", "top200_proxy", "manual_csv"],
        help="How to construct the benchmark constituents",
    )
    parser.add_argument("--source-csv", default=str(DEFAULT_SOURCE_CSV), help="Manual KOSPI200 CSV path")
    parser.add_argument("--export-csv", default=str(DEFAULT_EXPORT_CSV), help="Latest benchmark CSV output path")
    parser.add_argument(
        "--constituent-count",
        type=int,
        default=DEFAULT_CONSTITUENT_COUNT,
        help="Number of KOSPI securities to include in proxy mode",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if str(args.mode).strip().lower() == "history":
        result = sync_kospi200_constituent_history(
            db_path=Path(args.db_path) if str(args.db_path).strip() else None,
            start_date=str(args.start_date).strip() or None,
            end_date=str(args.end_date).strip() or str(args.as_of_date).strip() or None,
            source_mode=str(args.source_mode).strip() or DEFAULT_SOURCE_MODE,
        )
        _log(
            f"Completed constituent history sync: code={result.index_code}, "
            f"range={result.start_date}..{result.end_date}, "
            f"requested_days={result.requested_days}, stored_days={result.stored_days}, "
            f"stored_rows={result.stored_rows}"
        )
        return 0

    result = sync_kospi200_benchmark(
        db_path=Path(args.db_path) if str(args.db_path).strip() else None,
        as_of_date=str(args.as_of_date).strip() or None,
        source_mode=str(args.source_mode).strip() or DEFAULT_SOURCE_MODE,
        source_csv=Path(args.source_csv),
        export_csv_path=Path(args.export_csv) if str(args.export_csv).strip() else None,
        constituent_count=max(int(args.constituent_count), 1),
    )
    _log(
        f"Completed benchmark sync: code={result.benchmark_code}, as_of_date={result.as_of_date}, "
        f"constituents={result.constituent_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
