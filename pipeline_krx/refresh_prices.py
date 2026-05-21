from __future__ import annotations

import argparse
import contextlib
import io
import os
import signal
import sqlite3
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pandas as pd

from pipeline_common.security import configure_ssl

from .db import (
    init_krx_project_db,
    sync_krx_security_snapshot,
    upsert_krx_fundamentals_snapshot,
    upsert_krx_prices,
    upsert_krx_securities,
)

try:
    import FinanceDataReader as fdr
except Exception:  # pragma: no cover - optional dependency
    fdr = None

DEFAULT_START_DATE = "2019-12-31"
DEFAULT_COMPONENTS_CSV = Path("data/krx_components_full.csv")
DEFAULT_CLOSE_CSV = Path("data/krx_close_prices.csv")
DEFAULT_MARKET_CAP_CSV = Path("data/krx_market_caps.csv")
DEFAULT_SHARES_CSV = Path("data/krx_shares.csv")
DEFAULT_PROGRESS_BATCH_SIZE = 200
DEFAULT_INCREMENTAL_OVERLAP_DAYS = 7
_CANCEL_REQUESTED = False


@dataclass(frozen=True)
class KRXPriceRefreshResult:
    symbol_count: int
    stored_price_rows: int
    close_csv_rows: int
    market_cap_csv_rows: int
    shares_csv_rows: int
    sqlite_path: Path
    close_csv_path: Path
    market_cap_csv_path: Path
    shares_csv_path: Path


def _log(message: str) -> None:
    print(f"[refresh-krx-prices] {message}", flush=True)


def _batch_progress_log(
    *,
    batch_start_index: int,
    batch_end_index: int,
    total_symbols: int,
    stored_symbols: int | None = None,
    empty_symbols: int | None = None,
    rows_seen: int | None = None,
    note: str | None = None,
) -> None:
    parts = [f"{batch_start_index}-{batch_end_index}/{total_symbols}"]
    if note:
        parts.append(str(note))
    if stored_symbols is not None:
        parts.append(f"stored_symbols_batch={int(stored_symbols)}")
    if empty_symbols is not None:
        parts.append(f"empty_symbols_batch={int(empty_symbols)}")
    if rows_seen is not None:
        parts.append(f"rows_batch={int(rows_seen)}")
    _log(" ".join(parts))


def _handle_sigint(_signum: int, _frame: object) -> None:
    global _CANCEL_REQUESTED
    _CANCEL_REQUESTED = True
    raise KeyboardInterrupt


def _raise_if_cancelled() -> None:
    if _CANCEL_REQUESTED:
        raise KeyboardInterrupt


def _interruptible_sleep(seconds: float) -> None:
    remaining = max(float(seconds), 0.0)
    while remaining > 0:
        _raise_if_cancelled()
        chunk = min(remaining, 0.2)
        time.sleep(chunk)
        remaining -= chunk
    _raise_if_cancelled()


def _normalize_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    if text.isdigit():
        return text.zfill(6)
    return text


def _normalize_number(value: object) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text.lower() in {"nan", "none", "null", "n/a", "-"}:
        return None
    try:
        return float(text)
    except Exception:
        return None


def _column_name(frame: pd.DataFrame, *candidates: str) -> str | None:
    cols = {str(col).strip().lower(): col for col in frame.columns}
    for candidate in candidates:
        found = cols.get(candidate.strip().lower())
        if found is not None:
            return str(found)
    return None


def _read_components_csv(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if frame.empty:
        raise RuntimeError(f"KRX components CSV is empty: {path}")
    cols = {str(col).strip().lower(): col for col in frame.columns}
    symbol_col = cols.get("symbol")
    if symbol_col is None:
        raise RuntimeError(f"KRX components CSV has no Symbol column: {path}")
    out = frame.copy()
    out["Symbol"] = out[symbol_col].map(_normalize_symbol)
    shares_col = cols.get("sharesoutstanding") or cols.get("shares_outstanding")
    out["SharesOutstanding"] = out[shares_col].map(_normalize_number) if shares_col is not None else None
    market_cap_col = cols.get("marketcap") or cols.get("market_cap") or cols.get("marcap")
    out["MarketCap"] = out[market_cap_col].map(_normalize_number) if market_cap_col is not None else None
    market_col = cols.get("market")
    out["Market"] = out[market_col].astype(str).str.strip().str.upper() if market_col is not None else "UNKNOWN"
    return out


def _load_current_listing_from_fdr() -> pd.DataFrame:
    if fdr is None:
        return pd.DataFrame()
    try:
        raw = fdr.StockListing("KRX")
    except Exception as exc:
        _log(f"current listing source unavailable: fdr.StockListing('KRX') error={type(exc).__name__}: {exc}")
        return pd.DataFrame()
    if raw is None or raw.empty:
        return pd.DataFrame()

    cols = {str(col).strip().lower(): col for col in raw.columns}
    code_col = cols.get("code") or cols.get("symbol")
    if code_col is None:
        return pd.DataFrame()

    out = pd.DataFrame()
    out["Symbol"] = raw[code_col].map(_normalize_symbol)
    out["Market"] = raw[cols["market"]].astype(str).str.strip().str.upper() if cols.get("market") is not None else "UNKNOWN"
    if cols.get("name") is not None:
        out["NameKR"] = raw[cols["name"]].astype(str).str.strip()
    if cols.get("isu_cd") is not None:
        out["ISIN"] = raw[cols["isu_cd"]].astype(str).str.strip()
    stocks_col = cols.get("stocks") or cols.get("sharesoutstanding") or cols.get("shares_outstanding")
    marcap_col = cols.get("marcap") or cols.get("marketcap") or cols.get("market_cap")
    out["SharesOutstanding"] = raw[stocks_col].map(_normalize_number) if stocks_col is not None else None
    out["MarketCap"] = raw[marcap_col].map(_normalize_number) if marcap_col is not None else None
    out["ReferenceSource"] = "fdr:StockListing:KRX"
    out = out[out["Symbol"].astype(str).str.strip() != ""]
    out = out.drop_duplicates(subset=["Symbol"], keep="last").reset_index(drop=True)
    return out


def _load_components_from_sqlite(db_path: Path) -> pd.DataFrame:
    if not db_path.exists():
        return pd.DataFrame()
    with sqlite3.connect(db_path) as conn:
        frame = pd.read_sql_query(
            """
            SELECT
                s.symbol AS Symbol,
                s.market AS Market,
                s.name_kr AS NameKR,
                s.name_en AS NameEN,
                s.sector AS Sector,
                s.industry AS Industry,
                s.listing_date AS ListingDate,
                s.reference_source AS ReferenceSource,
                (
                    SELECT p.shares_outstanding
                    FROM prices AS p
                    WHERE p.symbol = s.symbol
                      AND p.shares_outstanding IS NOT NULL
                    ORDER BY p.date DESC
                    LIMIT 1
                ) AS SharesOutstanding,
                (
                    SELECT p.market_cap
                    FROM prices AS p
                    WHERE p.symbol = s.symbol
                      AND p.market_cap IS NOT NULL
                    ORDER BY p.date DESC
                    LIMIT 1
                ) AS MarketCap
            FROM securities AS s
            WHERE COALESCE(s.is_active, 1) = 1
              AND s.symbol IS NOT NULL
            ORDER BY
                CASE s.market WHEN 'KOSPI' THEN 0 WHEN 'KOSDAQ' THEN 1 WHEN 'KONEX' THEN 2 ELSE 3 END,
                s.symbol
            """,
            conn,
        )
    if frame.empty:
        return frame
    frame["Symbol"] = frame["Symbol"].map(_normalize_symbol)
    frame["Market"] = frame["Market"].astype(str).str.strip().str.upper().replace({"": "UNKNOWN", "NONE": "UNKNOWN", "NAN": "UNKNOWN"})
    frame["SharesOutstanding"] = frame["SharesOutstanding"].map(_normalize_number)
    if "MarketCap" in frame.columns:
        frame["MarketCap"] = frame["MarketCap"].map(_normalize_number)
    return frame


def _load_components(path: Path, *, db_path: Path | None = None, prefer_current_listing: bool = True) -> tuple[pd.DataFrame, str]:
    if prefer_current_listing:
        listing = _load_current_listing_from_fdr()
        if not listing.empty:
            return listing, "fdr:StockListing:KRX"

    if db_path is not None:
        from_db = _load_components_from_sqlite(db_path)
        if not from_db.empty:
            _log(f"components source: sqlite:{db_path}:securities rows={len(from_db.index)}")
            return from_db, f"sqlite:{db_path}:securities"

    candidates = [path]
    for fallback in [Path("data/krx_components.csv"), Path("data/krx_kospi200_latest.csv")]:
        if fallback not in candidates:
            candidates.append(fallback)
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            _log(f"components source: csv:{candidate}")
            return _read_components_csv(candidate), f"csv:{candidate}"

    raise FileNotFoundError(
        f"KRX components are missing from shared SQLite and no bootstrap CSV was found: {path}. "
        "Run `python -m pipeline_krx.components --db-path <shared_db>` to seed securities."
    )


def _standardize_price_frame(
    raw: pd.DataFrame,
    *,
    symbol: str,
    shares_outstanding: float | None,
    dividend_per_share: float | None = None,
    dividend_yield: float | None = None,
) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()

    open_col = _column_name(raw, "open", "시가")
    high_col = _column_name(raw, "high", "고가")
    low_col = _column_name(raw, "low", "저가")
    close_col = _column_name(raw, "close", "종가")
    volume_col = _column_name(raw, "volume", "거래량")
    value_col = _column_name(raw, "amount", "tradingvalue", "trade value", "거래대금")
    market_cap_col = _column_name(raw, "marcap", "marketcap", "market_cap", "시가총액")
    if None in {open_col, high_col, low_col, close_col}:
        return pd.DataFrame()

    out = pd.DataFrame(index=pd.to_datetime(raw.index, errors="coerce").normalize())
    out["symbol"] = _normalize_symbol(symbol)
    out["date"] = out.index.strftime("%Y-%m-%d")
    out["open"] = pd.to_numeric(raw[open_col], errors="coerce")
    out["high"] = pd.to_numeric(raw[high_col], errors="coerce")
    out["low"] = pd.to_numeric(raw[low_col], errors="coerce")
    out["close"] = pd.to_numeric(raw[close_col], errors="coerce")
    out["volume"] = pd.to_numeric(raw[volume_col], errors="coerce").fillna(0.0) if volume_col else 0.0
    out["trading_value"] = pd.to_numeric(raw[value_col], errors="coerce") if value_col else None
    market_cap = pd.to_numeric(raw[market_cap_col], errors="coerce") if market_cap_col else None
    out["market_cap"] = market_cap if market_cap is not None else (out["close"] * float(shares_outstanding) if shares_outstanding is not None else None)
    if shares_outstanding is not None:
        out["shares_outstanding"] = float(shares_outstanding)
    elif market_cap is not None:
        out["shares_outstanding"] = out["market_cap"] / out["close"].replace(0.0, pd.NA)
    else:
        out["shares_outstanding"] = None
    out["foreign_ownership_pct"] = None
    out["adj_close"] = out["close"]
    out["dividends"] = float(dividend_per_share) if dividend_per_share is not None else 0.0
    out["dividend_yield"] = float(dividend_yield) if dividend_yield is not None else None
    out["stock_splits"] = 0.0
    out["currency"] = "KRW"
    out["source"] = "fdr"
    out = out.dropna(subset=["date", "open", "high", "low", "close"])
    out = out[~out.index.isna()].sort_index()
    return out.reset_index(drop=True)


def _load_latest_krx_fundamental_metrics(symbols: list[str], *, end_date: str | None) -> tuple[dict[str, dict[str, float | None]], str | None]:
    if not symbols:
        return {}, None
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from pykrx import stock as pykrx_stock
    except Exception as exc:  # pragma: no cover - optional dependency
        _log(f"fundamental snapshot unavailable: pykrx import failed ({type(exc).__name__})")
        return {}, None

    query_end = pd.Timestamp(end_date).normalize() if end_date else pd.Timestamp.today().normalize()
    wanted = {_normalize_symbol(symbol) for symbol in symbols if _normalize_symbol(symbol)}
    if not wanted:
        return {}, None

    for query_date in pd.date_range(end=query_end, periods=10, freq="D")[::-1]:
        date_text = query_date.strftime("%Y%m%d")
        frames: list[pd.DataFrame] = []
        for market in ("KOSPI", "KOSDAQ", "KONEX"):
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    frame = pykrx_stock.get_market_fundamental(date_text, market=market)
            except Exception:
                continue
            if frame is not None and not frame.empty:
                frames.append(frame)
        if not frames:
            continue

        combined = pd.concat(frames, axis=0)
        combined.index = combined.index.map(_normalize_symbol)
        out: dict[str, dict[str, float | None]] = {}
        for symbol in wanted.intersection(set(combined.index)):
            row = combined.loc[symbol]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            metrics = {
                "per": _normalize_number(row.get("PER")),
                "pbr": _normalize_number(row.get("PBR")),
                "eps": _normalize_number(row.get("EPS")),
                "bps": _normalize_number(row.get("BPS")),
                "dividend_per_share": _normalize_number(row.get("DPS")),
                "dividend_yield": _normalize_number(row.get("DIV")),
            }
            if any(value is not None for value in metrics.values()):
                out[symbol] = {
                    key: value
                    for key, value in metrics.items()
                }
        if out:
            source = f"pykrx:market_fundamental:{date_text}"
            _log(f"fundamental metrics source: {source} symbols={len(out)}")
            return out, date_text
    return {}, None


def _latest_price_dates(db_path: Path) -> dict[str, str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT symbol, MAX(date)
            FROM prices
            GROUP BY symbol
            """
        ).fetchall()
    return {
        _normalize_symbol(symbol): str(latest_date)
        for symbol, latest_date in rows
        if str(symbol or "").strip() and str(latest_date or "").strip()
    }


def _incremental_start_date(
    *,
    configured_start_date: str,
    latest_date: str | None,
    overlap_days: int,
) -> str:
    configured = pd.Timestamp(configured_start_date).normalize()
    if not latest_date:
        return configured.strftime("%Y-%m-%d")
    try:
        latest = pd.Timestamp(latest_date).normalize()
    except Exception:
        return configured.strftime("%Y-%m-%d")
    incremental = latest - timedelta(days=max(int(overlap_days), 0))
    return max(configured, incremental).strftime("%Y-%m-%d")


def _export_wide_csvs_from_db(
    *,
    db_path: Path,
    close_csv_path: Path,
    market_cap_csv_path: Path,
    shares_csv_path: Path,
) -> tuple[int, int, int]:
    with sqlite3.connect(db_path) as conn:
        frame = pd.read_sql_query(
            """
            SELECT symbol, date, close, market_cap, shares_outstanding
            FROM prices
            ORDER BY date, symbol
            """,
            conn,
        )
    if frame.empty:
        return 0, 0, 0

    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date", "symbol"])
    outputs = [
        (close_csv_path, frame.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()),
        (
            market_cap_csv_path,
            frame.pivot_table(index="date", columns="symbol", values="market_cap", aggfunc="last").sort_index(),
        ),
        (
            shares_csv_path,
            frame.pivot_table(index="date", columns="symbol", values="shares_outstanding", aggfunc="last").sort_index(),
        ),
    ]
    row_counts: list[int] = []
    for path, wide_frame in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        export = wide_frame.reset_index().rename(columns={"date": "Date"})
        export["Date"] = pd.to_datetime(export["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
        export.to_csv(path, index=False, encoding="utf-8")
        row_counts.append(len(wide_frame.index))
    return row_counts[0], row_counts[1], row_counts[2]


def refresh_krx_prices(
    *,
    components_csv: Path = DEFAULT_COMPONENTS_CSV,
    db_path: Path | str | None = None,
    start_date: str = DEFAULT_START_DATE,
    end_date: str | None = None,
    incremental_overlap_days: int = DEFAULT_INCREMENTAL_OVERLAP_DAYS,
    pause_seconds: float = 0.05,
    close_csv_path: Path = DEFAULT_CLOSE_CSV,
    market_cap_csv_path: Path = DEFAULT_MARKET_CAP_CSV,
    shares_csv_path: Path = DEFAULT_SHARES_CSV,
    insecure_ssl: bool = False,
    ca_bundle: str | None = None,
    refresh_fundamentals: bool = False,
) -> KRXPriceRefreshResult:
    if fdr is None:
        raise RuntimeError("FinanceDataReader is not installed")

    configure_ssl(insecure_ssl=insecure_ssl, ca_bundle=ca_bundle)
    sqlite_result = init_krx_project_db(db_path=Path(db_path) if db_path is not None else None)
    components, components_source = _load_components(components_csv, db_path=sqlite_result.db_path)
    _log(f"components source: {components_source} rows={len(components.index)}")
    securities_frame = components.rename(
        columns={
            "Symbol": "symbol",
            "Market": "market",
            "NameKR": "name_kr",
            "NameEN": "name_en",
            "ISIN": "isin",
            "Sector": "sector",
            "Industry": "industry",
            "ListingDate": "listing_date",
            "ReferenceSource": "reference_source",
        }
    )
    if components_source == "fdr:StockListing:KRX":
        sync_krx_security_snapshot(
            securities_frame,
            deactivate_all_missing=True,
            db_path=sqlite_result.db_path,
        )
    else:
        upsert_krx_securities(securities_frame, db_path=sqlite_result.db_path)

    price_frames: list[pd.DataFrame] = []
    latest_dates = _latest_price_dates(sqlite_result.db_path)
    component_symbols = [_normalize_symbol(value) for value in components["Symbol"].tolist()]
    fundamental_metrics: dict[str, dict[str, float | None]] = {}
    fundamental_date = None
    if refresh_fundamentals:
        fundamental_metrics, fundamental_date = _load_latest_krx_fundamental_metrics(component_symbols, end_date=end_date)
    else:
        _log("fundamental snapshot skipped; use --refresh-fundamentals to request pykrx PER/PBR/DPS/DIV.")
    if fundamental_metrics:
        snapshot_rows = [
            {
                "symbol": symbol,
                "as_of_date": pd.Timestamp(fundamental_date).strftime("%Y-%m-%d") if fundamental_date else pd.Timestamp.today().normalize().strftime("%Y-%m-%d"),
                "per": metrics.get("per"),
                "pbr": metrics.get("pbr"),
                "eps": metrics.get("eps"),
                "bps": metrics.get("bps"),
                "dividend_yield": metrics.get("dividend_yield"),
                "source": f"pykrx:market_fundamental:{fundamental_date or 'latest'}",
            }
            for symbol, metrics in fundamental_metrics.items()
        ]
        changed_snapshot_rows = upsert_krx_fundamentals_snapshot(pd.DataFrame(snapshot_rows), db_path=sqlite_result.db_path)
        _log(f"fundamentals_snapshot changed_rows={changed_snapshot_rows}")
    total = len(components.index)
    batch_size = DEFAULT_PROGRESS_BATCH_SIZE
    batch_start_index = 1
    batch_stored_symbols = 0
    batch_empty_symbols = 0
    batch_rows_seen = 0
    for index, row in enumerate(components.itertuples(index=False), start=1):
        _raise_if_cancelled()
        if index == batch_start_index:
            _batch_progress_log(
                batch_start_index=batch_start_index,
                batch_end_index=min(batch_start_index + batch_size - 1, total),
                total_symbols=total,
                note="batch_started",
            )
        symbol = _normalize_symbol(getattr(row, "Symbol"))
        shares_outstanding = _normalize_number(getattr(row, "SharesOutstanding", None))
        fundamental_metric = fundamental_metrics.get(symbol, {})
        symbol_start_date = _incremental_start_date(
            configured_start_date=start_date,
            latest_date=latest_dates.get(symbol),
            overlap_days=incremental_overlap_days,
        )
        raw = fdr.DataReader(symbol, symbol_start_date, end_date) if end_date else fdr.DataReader(symbol, symbol_start_date)
        standardized = _standardize_price_frame(
            raw,
            symbol=symbol,
            shares_outstanding=shares_outstanding,
            dividend_per_share=_normalize_number(fundamental_metric.get("dividend_per_share")),
            dividend_yield=_normalize_number(fundamental_metric.get("dividend_yield")),
        )
        if not standardized.empty:
            price_frames.append(standardized)
            batch_stored_symbols += 1
            batch_rows_seen += len(standardized)
        else:
            batch_empty_symbols += 1
        if index % batch_size == 0 or index == total:
            _batch_progress_log(
                batch_start_index=batch_start_index,
                batch_end_index=index,
                total_symbols=total,
                stored_symbols=batch_stored_symbols,
                empty_symbols=batch_empty_symbols,
                rows_seen=batch_rows_seen,
                note="batch_done",
            )
            batch_start_index = index + 1
            batch_stored_symbols = 0
            batch_empty_symbols = 0
            batch_rows_seen = 0
        if index < total and pause_seconds > 0:
            _interruptible_sleep(pause_seconds)

    combined = pd.concat(price_frames, axis=0, ignore_index=True) if price_frames else pd.DataFrame()
    stored_price_rows = upsert_krx_prices(combined, db_path=sqlite_result.db_path) if not combined.empty else 0
    close_csv_rows, market_cap_csv_rows, shares_csv_rows = _export_wide_csvs_from_db(
        db_path=sqlite_result.db_path,
        close_csv_path=close_csv_path,
        market_cap_csv_path=market_cap_csv_path,
        shares_csv_path=shares_csv_path,
    )

    return KRXPriceRefreshResult(
        symbol_count=total,
        stored_price_rows=stored_price_rows,
        close_csv_rows=close_csv_rows,
        market_cap_csv_rows=market_cap_csv_rows,
        shares_csv_rows=shares_csv_rows,
        sqlite_path=sqlite_result.db_path,
        close_csv_path=close_csv_path,
        market_cap_csv_path=market_cap_csv_path,
        shares_csv_path=shares_csv_path,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh KRX prices and market caps into the separate KRX project DB.")
    parser.add_argument("--components-csv", default=str(DEFAULT_COMPONENTS_CSV), help="KRX components CSV path")
    parser.add_argument("--db-path", default="", help="Optional SQLite DB path override")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE, help="Historical start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", default="", help="Optional end date (YYYY-MM-DD)")
    parser.add_argument(
        "--incremental-overlap-days",
        type=int,
        default=DEFAULT_INCREMENTAL_OVERLAP_DAYS,
        help="Days to overlap from each symbol's latest stored DB date",
    )
    parser.add_argument("--pause-seconds", type=float, default=0.05, help="Delay between symbol fetches")
    parser.add_argument("--close-csv", default=str(DEFAULT_CLOSE_CSV), help="Wide close-price CSV output path")
    parser.add_argument("--market-cap-csv", default=str(DEFAULT_MARKET_CAP_CSV), help="Wide market-cap CSV output path")
    parser.add_argument("--shares-csv", default=str(DEFAULT_SHARES_CSV), help="Wide shares CSV output path")
    parser.add_argument("--ca-bundle", default="", help="CA bundle path")
    parser.add_argument("--insecure-ssl", action="store_true", help="Disable TLS verification for temporary testing")
    parser.add_argument(
        "--refresh-fundamentals",
        action="store_true",
        default=str(os.getenv("KRX_REFRESH_FUNDAMENTALS", "")).strip().lower() in {"1", "true", "yes", "y"},
        help="Also refresh pykrx market fundamental snapshot. Disabled by default because pykrx often prints noisy upstream errors.",
    )
    return parser.parse_args()


def main() -> int:
    global _CANCEL_REQUESTED
    args = _parse_args()
    _CANCEL_REQUESTED = False
    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        result = refresh_krx_prices(
            components_csv=Path(args.components_csv),
            db_path=Path(args.db_path) if str(args.db_path).strip() else None,
            start_date=str(args.start_date).strip() or DEFAULT_START_DATE,
            end_date=str(args.end_date).strip() or None,
            incremental_overlap_days=int(args.incremental_overlap_days),
            pause_seconds=float(args.pause_seconds),
            close_csv_path=Path(args.close_csv),
            market_cap_csv_path=Path(args.market_cap_csv),
            shares_csv_path=Path(args.shares_csv),
            insecure_ssl=bool(args.insecure_ssl),
            ca_bundle=str(args.ca_bundle).strip() or None,
            refresh_fundamentals=bool(args.refresh_fundamentals),
        )
    except KeyboardInterrupt:
        _log("Cancelled by user (Ctrl+C).")
        return 130
    finally:
        signal.signal(signal.SIGINT, previous_handler)
    print(
        "refreshed_krx_prices",
        f"symbols={result.symbol_count}",
        f"stored_price_rows={result.stored_price_rows}",
        f"sqlite_path={result.sqlite_path}",
        f"close_csv={result.close_csv_path}",
        f"market_cap_csv={result.market_cap_csv_path}",
        f"shares_csv={result.shares_csv_path}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
