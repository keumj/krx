from __future__ import annotations

import argparse
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .security import configure_ssl, security_hint

try:
    import FinanceDataReader as fdr
except Exception:  # pragma: no cover - optional dependency
    fdr = None

try:
    import yfinance as yf
except Exception:  # pragma: no cover - optional dependency
    yf = None
else:
    try:
        from yfinance import set_tz_cache_location

        _YF_CACHE_DIR = Path("data/.yfinance_cache")
        _YF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        set_tz_cache_location(str(_YF_CACHE_DIR))
    except Exception:
        pass


DEFAULT_START_DATE = "2015-12-31"
DEFAULT_DATA_DIR = Path("data")
DEFAULT_SHARED_DB_ROOT = DEFAULT_DATA_DIR / "sp500_shared_db"
DEFAULT_COMPONENTS_CSV = DEFAULT_DATA_DIR / "sp500_components_full.csv"
DEFAULT_METRICS_CSV = DEFAULT_DATA_DIR / "sp500_all_metrics_prices.csv"
DEFAULT_SHARES_CSV = DEFAULT_DATA_DIR / "sp500_shares.csv"
DEFAULT_MARKET_CAP_CSV = DEFAULT_DATA_DIR / "sp500_market_caps.csv"
DEFAULT_SQLITE_NAME = "sp500_shared_prices.sqlite"
DEFAULT_MARKET_CAP_BACKFILL_DAYS = 30
_CANCEL_REQUESTED = False


@dataclass(frozen=True)
class RefreshResult:
    symbol_count: int
    fresh_symbol_count: int
    stale_symbol_count: int
    market_cap_fresh_symbol_count: int
    metrics_rows: int
    market_cap_rows: int
    sqlite_added_rows: int
    sqlite_market_cap_updated_rows: int
    sqlite_old_max_date: str | None
    sqlite_new_max_date: str | None
    metrics_csv_path: Path
    shares_csv_path: Path
    market_cap_csv_path: Path
    sqlite_path: Path


def _log(message: str) -> None:
    print(f"[refresh-sp500] {message}", flush=True)


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
    return str(value or "").strip().upper()


def _provider_symbol(symbol: str) -> str:
    return _normalize_symbol(symbol).replace(".", "-")


def _read_symbol_list(symbols_csv: Path, sqlite_path: Path) -> list[str]:
    if symbols_csv.exists() and symbols_csv.is_file():
        raw = pd.read_csv(symbols_csv)
        cols = {str(c).strip().lower(): c for c in raw.columns}
        sym_col = cols.get("symbol") or raw.columns[0]
        symbols = [_normalize_symbol(v) for v in raw[sym_col].tolist()]
        out = [s for s in symbols if s]
        if out:
            return sorted(dict.fromkeys(out))

    if sqlite_path.exists() and sqlite_path.is_file():
        with sqlite3.connect(sqlite_path) as conn:
            rows = conn.execute("SELECT DISTINCT symbol FROM prices ORDER BY symbol").fetchall()
        out = [_normalize_symbol(row[0]) for row in rows]
        out = [s for s in out if s]
        if out:
            return out

    raise FileNotFoundError(
        f"Could not determine symbol universe from {symbols_csv} or existing SQLite {sqlite_path}"
    )


def _read_wide_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or not path.is_file():
        return pd.DataFrame()
    raw = pd.read_csv(path)
    if raw.empty:
        return pd.DataFrame()
    cols = {str(c).strip().lower(): c for c in raw.columns}
    date_col = cols.get("date") or cols.get("datetime") or raw.columns[0]
    raw[date_col] = pd.to_datetime(raw[date_col], errors="coerce")
    raw = raw.dropna(subset=[date_col]).set_index(date_col).sort_index()
    raw.index = raw.index.normalize()
    for col in raw.columns:
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    return raw


def _ensure_ohlcv_frame(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    if frame is None or frame.empty:
        return None

    raw = frame.copy()
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [str(c[0]) for c in raw.columns]

    cols = {str(c).strip().lower(): c for c in raw.columns}
    open_col = cols.get("open")
    high_col = cols.get("high")
    low_col = cols.get("low")
    close_col = cols.get("close")
    adj_close_col = cols.get("adj close") or cols.get("adjclose") or cols.get("adj_close")
    volume_col = cols.get("volume")
    dividends_col = cols.get("dividends") or cols.get("dividend")
    stock_splits_col = cols.get("stock splits") or cols.get("stocksplits") or cols.get("stock_splits") or cols.get("split")

    if open_col is None or high_col is None or low_col is None or close_col is None:
        return None

    out = pd.DataFrame(index=pd.to_datetime(raw.index, errors="coerce").normalize())
    out["open"] = pd.to_numeric(raw[open_col], errors="coerce")
    out["high"] = pd.to_numeric(raw[high_col], errors="coerce")
    out["low"] = pd.to_numeric(raw[low_col], errors="coerce")
    out["close"] = pd.to_numeric(raw[close_col], errors="coerce")
    if adj_close_col is not None:
        out["adj_close"] = pd.to_numeric(raw[adj_close_col], errors="coerce")
    else:
        out["adj_close"] = out["close"]
    if volume_col is not None:
        out["volume"] = pd.to_numeric(raw[volume_col], errors="coerce").fillna(0.0)
    else:
        out["volume"] = 0.0
    if dividends_col is not None:
        out["dividends"] = pd.to_numeric(raw[dividends_col], errors="coerce").fillna(0.0)
    else:
        out["dividends"] = 0.0
    if stock_splits_col is not None:
        out["stock_splits"] = pd.to_numeric(raw[stock_splits_col], errors="coerce").fillna(0.0)
    else:
        out["stock_splits"] = 0.0

    out = out.dropna(subset=["open", "high", "low", "close", "adj_close"])
    out = out[~out.index.isna()]
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out if not out.empty else None


def _ensure_share_series(series: pd.Series | None) -> pd.Series | None:
    if series is None or len(series) == 0:
        return None
    raw = pd.Series(series).copy()
    raw.index = pd.to_datetime(raw.index, errors="coerce")
    if getattr(raw.index, "tz", None) is not None:
        raw.index = raw.index.tz_localize(None)
    raw.index = raw.index.normalize()
    raw = pd.to_numeric(raw, errors="coerce")
    raw = raw[~raw.index.isna()].dropna()
    if raw.empty:
        return None
    raw = raw.groupby(raw.index).last().sort_index()
    return raw.astype(float)


def _pick_numeric_field(payload: object, *keys: str) -> float | None:
    if payload is None:
        return None

    getter = getattr(payload, "get", None)
    for key in keys:
        raw = None
        if callable(getter):
            try:
                raw = getter(key)
            except Exception:
                raw = None
        else:
            try:
                raw = payload[key]  # type: ignore[index]
            except Exception:
                raw = None

        try:
            value = float(raw)
        except Exception:
            continue
        if pd.notna(value):
            return value
    return None


def _single_point_share_series(shares: float, *, end_date: str) -> pd.Series:
    try:
        anchor = pd.Timestamp(end_date).normalize() - pd.Timedelta(days=1)
    except Exception:
        anchor = pd.Timestamp.today().normalize()
    return pd.Series([float(shares)], index=[anchor])


def _extract_from_yfinance_download(raw: pd.DataFrame, local_symbol: str) -> pd.DataFrame | None:
    if raw is None or raw.empty:
        return None

    provider_symbol = _provider_symbol(local_symbol)

    if isinstance(raw.columns, pd.MultiIndex):
        level0 = {str(v).strip().lower() for v in raw.columns.get_level_values(0)}
        level1 = {str(v).strip() for v in raw.columns.get_level_values(1)}

        sub: pd.DataFrame | None = None
        if provider_symbol in level0:
            sub = raw[provider_symbol]
        elif provider_symbol in level1:
            sub = raw.xs(provider_symbol, axis=1, level=1)

        return _ensure_ohlcv_frame(sub)

    return _ensure_ohlcv_frame(raw)


def _download_yfinance_chunk(
    symbols: list[str],
    *,
    start_date: str,
    end_date: str,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    if yf is None:
        raise RuntimeError("yfinance is not installed")

    provider_symbols = [_provider_symbol(symbol) for symbol in symbols]
    raw = yf.download(
        provider_symbols,
        start=start_date,
        end=end_date,
        progress=False,
        auto_adjust=False,
        actions=True,
        threads=False,
    )

    out: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for symbol in symbols:
        frame = _extract_from_yfinance_download(raw, symbol)
        if frame is None or frame.empty:
            missing.append(symbol)
            continue
        out[symbol] = frame
    return out, missing


def _download_fdr_symbol(symbol: str, *, start_date: str) -> pd.DataFrame | None:
    if fdr is None:
        return None

    provider_symbol = _provider_symbol(symbol)
    try:
        raw = fdr.DataReader(provider_symbol, start_date)
    except Exception:
        return None
    return _ensure_ohlcv_frame(raw)


def _download_yfinance_shares_full(
    symbol: str,
    *,
    start_date: str,
    end_date: str,
) -> pd.Series | None:
    if yf is None:
        return None
    provider_symbol = _provider_symbol(symbol)
    try:
        ticker = yf.Ticker(provider_symbol)
    except Exception:
        return None

    raw = None
    try:
        raw = ticker.get_shares_full(start=start_date, end=end_date)
    except Exception:
        raw = None

    full_series = _ensure_share_series(raw)
    if full_series is not None and not full_series.empty:
        return full_series

    latest_shares = _pick_numeric_field(getattr(ticker, "fast_info", None), "shares", "shares_outstanding")
    info_payload = None
    if latest_shares is None:
        try:
            get_info = getattr(ticker, "get_info", None)
            if callable(get_info):
                info_payload = get_info()
            else:
                info_payload = getattr(ticker, "info", None)
        except Exception:
            info_payload = None
        latest_shares = _pick_numeric_field(
            info_payload,
            "sharesOutstanding",
            "impliedSharesOutstanding",
            "shares",
        )

    if latest_shares is None:
        if info_payload is None:
            try:
                get_info = getattr(ticker, "get_info", None)
                if callable(get_info):
                    info_payload = get_info()
                else:
                    info_payload = getattr(ticker, "info", None)
            except Exception:
                info_payload = None
        market_cap = _pick_numeric_field(info_payload, "marketCap")
        last_price = _pick_numeric_field(info_payload, "regularMarketPrice", "currentPrice", "previousClose")
        if market_cap is not None and last_price not in (None, 0):
            latest_shares = float(market_cap) / float(last_price)

    if latest_shares is None or not pd.notna(latest_shares) or float(latest_shares) <= 0:
        return None

    return _single_point_share_series(float(latest_shares), end_date=end_date)


def _download_shares_histories(
    symbols: list[str],
    *,
    start_date: str,
    end_date: str,
    provider: str,
    chunk_size: int,
    pause_seconds: float,
) -> tuple[dict[str, pd.Series], list[str]]:
    if provider not in {"auto", "yfinance"}:
        _log(f"Skipping market cap share-history fetch for provider={provider}")
        return {}, list(symbols)

    fresh: dict[str, pd.Series] = {}
    missing: list[str] = []
    safe_chunk_size = max(1, int(chunk_size))
    total_chunks = max((len(symbols) + safe_chunk_size - 1) // safe_chunk_size, 1)
    for i in range(0, len(symbols), safe_chunk_size):
        _raise_if_cancelled()
        chunk = symbols[i : i + safe_chunk_size]
        chunk_no = (i // safe_chunk_size) + 1
        _log(
            f"Downloading shares chunk {chunk_no}/{total_chunks} "
            f"({len(chunk)} symbols, first={chunk[0]}, last={chunk[-1]})"
        )
        chunk_fresh = 0
        chunk_missing = 0
        for symbol in chunk:
            _raise_if_cancelled()
            series = _download_yfinance_shares_full(
                symbol,
                start_date=start_date,
                end_date=end_date,
            )
            if series is None or series.empty:
                missing.append(symbol)
                chunk_missing += 1
            else:
                fresh[symbol] = series
                chunk_fresh += 1
            if pause_seconds > 0:
                _interruptible_sleep(pause_seconds)
        _log(
            f"Finished shares chunk {chunk_no}/{total_chunks}: "
            f"downloaded={chunk_fresh}, missing={chunk_missing}, cumulative_downloaded={len(fresh)}"
        )

    _log(
        f"Shares history stage complete: requested={len(symbols)}, fresh={len(fresh)}, missing={len(missing)}"
    )
    return fresh, missing


def _download_symbol_frames(
    symbols: list[str],
    *,
    start_date: str,
    end_date: str,
    chunk_size: int,
    provider: str,
    pause_seconds: float,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    fresh: dict[str, pd.DataFrame] = {}
    missing_total: list[str] = []

    use_yf = provider in {"auto", "yfinance"}
    use_fdr = provider in {"auto", "fdr"}

    if use_yf:
        total_chunks = max((len(symbols) + chunk_size - 1) // chunk_size, 1)
        for i in range(0, len(symbols), chunk_size):
            _raise_if_cancelled()
            chunk = symbols[i : i + chunk_size]
            chunk_no = (i // chunk_size) + 1
            _log(
                f"Downloading yfinance chunk {chunk_no}/{total_chunks} "
                f"({len(chunk)} symbols, first={chunk[0]}, last={chunk[-1]})"
            )
            try:
                frames, missing = _download_yfinance_chunk(
                    chunk,
                    start_date=start_date,
                    end_date=end_date,
                )
            except Exception as exc:
                _log(f"yfinance chunk {chunk_no}/{total_chunks} failed: {type(exc).__name__}: {exc}")
                frames = {}
                missing = list(chunk)
            fresh.update(frames)
            missing_total.extend(missing)
            _log(
                f"Finished chunk {chunk_no}/{total_chunks}: "
                f"downloaded={len(frames)}, missing={len(missing)}, cumulative_downloaded={len(fresh)}"
            )
            if pause_seconds > 0:
                _interruptible_sleep(pause_seconds)
    else:
        missing_total = list(symbols)

    retry_missing = [s for s in missing_total if s not in fresh]
    if retry_missing:
        _log(f"Retrying {len(retry_missing)} missing symbols one by one")
    still_missing: list[str] = []
    for symbol in retry_missing:
        _raise_if_cancelled()
        frame = None
        if use_yf and symbol not in fresh:
            _log(f"Retrying via yfinance: {symbol}")
            try:
                retry_frames, retry_missing_single = _download_yfinance_chunk(
                    [symbol],
                    start_date=start_date,
                    end_date=end_date,
                )
                if symbol in retry_frames:
                    frame = retry_frames[symbol]
                elif retry_missing_single:
                    frame = None
            except Exception as exc:
                _log(f"yfinance retry failed for {symbol}: {type(exc).__name__}: {exc}")
                frame = None
        if frame is None and use_fdr:
            _log(f"Trying FDR fallback: {symbol}")
            frame = _download_fdr_symbol(symbol, start_date=start_date)
        if frame is None or frame.empty:
            still_missing.append(symbol)
            _log(f"No fresh data for {symbol}")
            continue
        fresh[symbol] = frame
        _log(f"Downloaded fresh data for {symbol}")
        if pause_seconds > 0:
            _interruptible_sleep(pause_seconds)

    _log(
        f"Download stage complete: requested={len(symbols)}, fresh={len(fresh)}, "
        f"still_missing={len(still_missing)}"
    )
    return fresh, still_missing


def _build_output_frames(symbol_frames: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    close_parts: list[pd.Series] = []
    metrics_parts: list[pd.DataFrame] = []

    for symbol in sorted(symbol_frames):
        frame = symbol_frames[symbol].copy()
        if frame.empty:
            continue

        close_parts.append(frame["adj_close"].rename(symbol))
        metrics_parts.append(
            pd.DataFrame(
                {
                    f"{symbol}_AdjClose": frame["adj_close"],
                    f"{symbol}_Close": frame["close"],
                    f"{symbol}_High": frame["high"],
                    f"{symbol}_Low": frame["low"],
                    f"{symbol}_Open": frame["open"],
                    f"{symbol}_Volume": frame["volume"],
                    f"{symbol}_Dividends": frame.get("dividends", pd.Series(0.0, index=frame.index)),
                    f"{symbol}_StockSplits": frame.get("stock_splits", pd.Series(0.0, index=frame.index)),
                }
            )
        )

    if not close_parts:
        return pd.DataFrame(), pd.DataFrame()

    close_df = pd.concat(close_parts, axis=1).sort_index()
    metrics_df = pd.concat(metrics_parts, axis=1).sort_index()
    close_df.index = pd.to_datetime(close_df.index, errors="coerce").normalize()
    metrics_df.index = pd.to_datetime(metrics_df.index, errors="coerce").normalize()
    return close_df, metrics_df


def _build_market_cap_frame(metrics_df: pd.DataFrame, shares_histories: dict[str, pd.Series]) -> pd.DataFrame:
    if metrics_df.empty or not shares_histories:
        return pd.DataFrame()

    metrics = metrics_df.copy()
    metrics.index = pd.to_datetime(metrics.index, errors="coerce").normalize()
    metrics = metrics[~metrics.index.isna()].sort_index()
    parts: list[pd.Series] = []

    for symbol in sorted(shares_histories):
        close_col = f"{symbol}_Close"
        if close_col not in metrics.columns:
            continue
        close_series = pd.to_numeric(metrics[close_col], errors="coerce").dropna()
        if close_series.empty:
            continue
        shares_series = _ensure_share_series(shares_histories.get(symbol))
        if shares_series is None or shares_series.empty:
            continue
        aligned_shares = shares_series.reindex(close_series.index).ffill()
        market_cap = (close_series * aligned_shares).dropna()
        if market_cap.empty:
            continue
        parts.append(market_cap.rename(symbol))

    if not parts:
        return pd.DataFrame()

    out = pd.concat(parts, axis=1).sort_index()
    out.index = pd.to_datetime(out.index, errors="coerce").normalize()
    out = out[~out.index.isna()]
    out = out[~out.index.duplicated(keep="last")]
    return out.dropna(how="all")


def _share_histories_to_wide(shares_histories: dict[str, pd.Series]) -> pd.DataFrame:
    if not shares_histories:
        return pd.DataFrame()

    parts: list[pd.Series] = []
    for symbol in sorted(shares_histories):
        series = _ensure_share_series(shares_histories.get(symbol))
        if series is None or series.empty:
            continue
        parts.append(series.rename(symbol))
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, axis=1).sort_index()
    out.index = pd.to_datetime(out.index, errors="coerce").normalize()
    out = out[~out.index.isna()]
    out = out[~out.index.duplicated(keep="last")]
    return out.dropna(how="all")


def _wide_non_null_max_date(frame: pd.DataFrame) -> str | None:
    if frame.empty:
        return None
    temp = frame.copy()
    temp.index = pd.to_datetime(temp.index, errors="coerce").normalize()
    temp = temp[~temp.index.isna()]
    temp = temp.dropna(how="all")
    if temp.empty:
        return None
    return pd.Timestamp(temp.index.max()).strftime("%Y-%m-%d")


def _build_market_cap_frame_from_latest_shares(metrics_df: pd.DataFrame, shares_df: pd.DataFrame) -> pd.DataFrame:
    if metrics_df.empty or shares_df.empty:
        return pd.DataFrame()

    metrics = metrics_df.copy()
    metrics.index = pd.to_datetime(metrics.index, errors="coerce").normalize()
    metrics = metrics[~metrics.index.isna()].sort_index()
    shares = shares_df.copy()
    shares.index = pd.to_datetime(shares.index, errors="coerce").normalize()
    shares = shares[~shares.index.isna()].sort_index()

    parts: list[pd.Series] = []
    for col in metrics.columns:
        name = str(col)
        if not name.endswith("_Close"):
            continue
        symbol = name[: -len("_Close")]
        if symbol not in shares.columns:
            continue
        latest_shares_series = pd.to_numeric(shares[symbol], errors="coerce").dropna()
        if latest_shares_series.empty:
            continue
        latest_shares = float(latest_shares_series.iloc[-1])
        if latest_shares <= 0:
            continue
        close_series = pd.to_numeric(metrics[name], errors="coerce").dropna()
        if close_series.empty:
            continue
        market_cap_series = (close_series * latest_shares).dropna()
        if market_cap_series.empty:
            continue
        parts.append(market_cap_series.rename(symbol))

    if not parts:
        return pd.DataFrame()

    out = pd.concat(parts, axis=1).sort_index()
    out.index = pd.to_datetime(out.index, errors="coerce").normalize()
    out = out[~out.index.isna()]
    out = out[~out.index.duplicated(keep="last")]
    return out.dropna(how="all")


def _infer_share_histories_from_existing_market_caps(
    metrics_df: pd.DataFrame,
    market_cap_df: pd.DataFrame,
    symbols: list[str],
) -> dict[str, pd.Series]:
    if metrics_df.empty or market_cap_df.empty or not symbols:
        return {}

    metrics = metrics_df.copy()
    metrics.index = pd.to_datetime(metrics.index, errors="coerce").normalize()
    metrics = metrics[~metrics.index.isna()].sort_index()

    market_caps = market_cap_df.copy()
    market_caps.index = pd.to_datetime(market_caps.index, errors="coerce").normalize()
    market_caps = market_caps[~market_caps.index.isna()].sort_index()

    out: dict[str, pd.Series] = {}
    for symbol in symbols:
        close_col = f"{symbol}_Close"
        if close_col not in metrics.columns or symbol not in market_caps.columns:
            continue

        close_series = pd.to_numeric(metrics[close_col], errors="coerce")
        market_cap_series = pd.to_numeric(market_caps[symbol], errors="coerce")
        joined = pd.concat(
            [close_series.rename("close"), market_cap_series.rename("market_cap")],
            axis=1,
        ).dropna()
        if joined.empty:
            continue

        joined = joined[(joined["close"] > 0) & (joined["market_cap"] > 0)]
        if joined.empty:
            continue

        shares_series = (joined["market_cap"] / joined["close"]).replace([float("inf"), float("-inf")], pd.NA).dropna()
        shares_series = shares_series[shares_series > 0]
        if shares_series.empty:
            continue

        out[symbol] = shares_series.groupby(shares_series.index).last().sort_index().astype(float)

    return out


def _overlay_fresh_on_existing(fresh: pd.DataFrame, existing: pd.DataFrame) -> pd.DataFrame:
    if fresh.empty:
        return existing.copy()
    if existing.empty:
        return fresh.copy()
    out = fresh.combine_first(existing)
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out


def _write_wide_csv_atomic(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    out = frame.copy()
    out.index = pd.to_datetime(out.index, errors="coerce").normalize()
    out.index.name = "Date"
    _log(f"Writing CSV: {path}")
    out.reset_index().to_csv(tmp, index=False, encoding="utf-8")
    tmp.replace(path)


def _ensure_prices_table_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prices (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL DEFAULT 0,
            adj_close REAL,
            dividends REAL NOT NULL DEFAULT 0,
            stock_splits REAL NOT NULL DEFAULT 0,
            market_cap REAL,
            PRIMARY KEY (symbol, date)
        )
        """
    )
    existing_cols = {str(row[1]).strip().lower() for row in conn.execute("PRAGMA table_info(prices)").fetchall()}
    if "adj_close" not in existing_cols:
        conn.execute("ALTER TABLE prices ADD COLUMN adj_close REAL")
    if "dividends" not in existing_cols:
        conn.execute("ALTER TABLE prices ADD COLUMN dividends REAL NOT NULL DEFAULT 0")
    if "stock_splits" not in existing_cols:
        conn.execute("ALTER TABLE prices ADD COLUMN stock_splits REAL NOT NULL DEFAULT 0")
    if "market_cap" not in existing_cols:
        conn.execute("ALTER TABLE prices ADD COLUMN market_cap REAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            publish_date DATETIME NOT NULL,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            source TEXT NOT NULL,
            sentiment_score REAL,
            analysis_status TEXT NOT NULL DEFAULT 'pending'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fundamentals_snapshot (
            symbol TEXT PRIMARY KEY,
            as_of_date TEXT NOT NULL,
            roe REAL,
            per REAL,
            pbr REAL,
            source TEXT NOT NULL DEFAULT 'unknown',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fundamentals_quarterly (
            symbol TEXT NOT NULL,
            fiscal_date TEXT NOT NULL,
            filing_date TEXT,
            period_type TEXT NOT NULL DEFAULT 'quarterly',
            net_income REAL,
            diluted_eps REAL,
            stockholders_equity REAL,
            total_assets REAL,
            total_debt REAL,
            current_assets REAL,
            current_liabilities REAL,
            operating_cash_flow REAL,
            free_cash_flow REAL,
            source TEXT NOT NULL DEFAULT 'unknown',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (symbol, fiscal_date)
        )
        """
    )
    fundamentals_cols = {
        str(row[1]).strip().lower() for row in conn.execute("PRAGMA table_info(fundamentals_snapshot)").fetchall()
    }
    if "source" not in fundamentals_cols:
        conn.execute("ALTER TABLE fundamentals_snapshot ADD COLUMN source TEXT NOT NULL DEFAULT 'unknown'")
    if "updated_at" not in fundamentals_cols:
        conn.execute(
            "ALTER TABLE fundamentals_snapshot ADD COLUMN updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        )
    quarterly_cols = {
        str(row[1]).strip().lower() for row in conn.execute("PRAGMA table_info(fundamentals_quarterly)").fetchall()
    }
    if "source" not in quarterly_cols:
        conn.execute("ALTER TABLE fundamentals_quarterly ADD COLUMN source TEXT NOT NULL DEFAULT 'unknown'")
    if "updated_at" not in quarterly_cols:
        conn.execute(
            "ALTER TABLE fundamentals_quarterly ADD COLUMN updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        )
    news_cols = {str(row[1]).strip().lower() for row in conn.execute("PRAGMA table_info(news_articles)").fetchall()}
    if "analysis_status" not in news_cols:
        conn.execute("ALTER TABLE news_articles ADD COLUMN analysis_status TEXT NOT NULL DEFAULT 'pending'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_articles_ticker_publish_date ON news_articles(ticker, publish_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_articles_publish_date ON news_articles(publish_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_articles_ticker_publish_day ON news_articles(ticker, date(publish_date))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_articles_analysis_status_publish_date ON news_articles(analysis_status, publish_date)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fundamentals_snapshot_as_of_date ON fundamentals_snapshot(as_of_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fundamentals_quarterly_symbol_fiscal_date ON fundamentals_quarterly(symbol, fiscal_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fundamentals_quarterly_fiscal_date ON fundamentals_quarterly(fiscal_date)"
    )
    conn.execute(
        """
        CREATE VIEW IF NOT EXISTS news_articles_price_context AS
        SELECT
            n.id,
            n.ticker,
            n.publish_date,
            date(n.publish_date) AS publish_day,
            n.title,
            n.link,
            n.source,
            n.sentiment_score,
            n.analysis_status,
            p.date AS price_date,
            p.open,
            p.high,
            p.low,
            p.close,
            p.adj_close,
            p.volume,
            p.market_cap
        FROM news_articles AS n
        LEFT JOIN prices AS p
            ON p.symbol = n.ticker
           AND p.date = date(n.publish_date)
        """
    )
    conn.execute(
        """
        CREATE VIEW IF NOT EXISTS news_articles_market_context AS
        SELECT
            n.id,
            n.ticker,
            n.publish_date,
            date(n.publish_date) AS publish_day,
            n.title,
            n.link,
            n.source,
            n.sentiment_score,
            n.analysis_status,
            p.date AS reference_price_date,
            CASE
                WHEN p.date = date(n.publish_date) THEN 1
                ELSE 0
            END AS matched_on_publish_day,
            p.open,
            p.high,
            p.low,
            p.close,
            p.adj_close,
            p.volume,
            p.market_cap
        FROM news_articles AS n
        LEFT JOIN prices AS p
            ON p.symbol = n.ticker
           AND p.date = (
                SELECT MAX(p2.date)
                FROM prices AS p2
                WHERE p2.symbol = n.ticker
                  AND p2.date <= date(n.publish_date)
           )
        """
    )


def _sqlite_max_date(db_path: Path) -> str | None:
    if not db_path.exists() or not db_path.is_file():
        return None
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='prices'"
        ).fetchone()
        if row is None:
            return None
        value = conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    return str(value) if value else None


def _sqlite_market_cap_value_count(db_path: Path) -> int:
    if not db_path.exists() or not db_path.is_file():
        return 0
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='prices'"
        ).fetchone()
        if row is None:
            return 0
        cols = {str(item[1]).strip().lower() for item in conn.execute("PRAGMA table_info(prices)").fetchall()}
        if "market_cap" not in cols:
            return 0
        value = conn.execute("SELECT COUNT(*) FROM prices WHERE market_cap IS NOT NULL").fetchone()[0]
    return int(value or 0)


def _sqlite_market_cap_max_date(db_path: Path) -> str | None:
    if not db_path.exists() or not db_path.is_file():
        return None
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='prices'"
        ).fetchone()
        if row is None:
            return None
        cols = {str(item[1]).strip().lower() for item in conn.execute("PRAGMA table_info(prices)").fetchall()}
        if "market_cap" not in cols:
            return None
        value = conn.execute("SELECT MAX(date) FROM prices WHERE market_cap IS NOT NULL").fetchone()[0]
    return str(value) if value else None


def _incremental_fetch_start_date(default_start_date: str, sqlite_max_date: str | None) -> str:
    default_start = pd.Timestamp(default_start_date).normalize()
    if not sqlite_max_date:
        return default_start.strftime("%Y-%m-%d")

    try:
        next_business_day = pd.Timestamp(sqlite_max_date).normalize() + pd.offsets.BDay(1)
    except Exception:
        return default_start.strftime("%Y-%m-%d")

    fetch_start = max(default_start, next_business_day)
    return pd.Timestamp(fetch_start).strftime("%Y-%m-%d")


def _market_cap_fetch_start_date(default_start_date: str, market_cap_max_date: str | None, lookback_days: int) -> str:
    default_start = pd.Timestamp(default_start_date).normalize()
    if not market_cap_max_date:
        return default_start.strftime("%Y-%m-%d")

    try:
        max_cap_date = pd.Timestamp(market_cap_max_date).normalize()
        lookback_start = max_cap_date - pd.Timedelta(days=max(0, int(lookback_days)))
    except Exception:
        return default_start.strftime("%Y-%m-%d")

    fetch_start = max(default_start, lookback_start)
    return pd.Timestamp(fetch_start).strftime("%Y-%m-%d")


def _has_fetch_window(start_date: str, end_date_exclusive: str) -> bool:
    try:
        return pd.Timestamp(start_date).normalize() < pd.Timestamp(end_date_exclusive).normalize()
    except Exception:
        return True


def _metrics_wide_to_long(metrics_df: pd.DataFrame, *, only_after: str | None = None) -> pd.DataFrame:
    if metrics_df.empty:
        return pd.DataFrame(
            columns=["symbol", "date", "open", "high", "low", "close", "volume", "adj_close", "dividends", "stock_splits"]
        )

    frame = metrics_df.copy()
    frame.index = pd.to_datetime(frame.index, errors="coerce").normalize()
    frame = frame[~frame.index.isna()].sort_index()
    if only_after:
        cutoff = pd.Timestamp(only_after).normalize()
        frame = frame[frame.index > cutoff]
    if frame.empty:
        return pd.DataFrame(
            columns=["symbol", "date", "open", "high", "low", "close", "volume", "adj_close", "dividends", "stock_splits"]
        )

    symbol_cols: dict[str, dict[str, str]] = {}
    for col in frame.columns:
        name = str(col)
        if "_" not in name:
            continue
        symbol, metric = name.rsplit("_", 1)
        metric_key = metric.strip().lower()
        symbol_cols.setdefault(symbol, {})[metric_key] = name

    parts: list[pd.DataFrame] = []
    for symbol, cols in symbol_cols.items():
        if not {"open", "high", "low", "close"}.issubset(cols):
            continue

        part = pd.DataFrame(index=frame.index)
        part["symbol"] = symbol
        part["date"] = frame.index.strftime("%Y-%m-%d")
        part["open"] = pd.to_numeric(frame[cols["open"]], errors="coerce")
        part["high"] = pd.to_numeric(frame[cols["high"]], errors="coerce")
        part["low"] = pd.to_numeric(frame[cols["low"]], errors="coerce")
        part["close"] = pd.to_numeric(frame[cols["close"]], errors="coerce")
        if "adjclose" in cols:
            part["adj_close"] = pd.to_numeric(frame[cols["adjclose"]], errors="coerce")
        else:
            part["adj_close"] = part["close"]
        if "volume" in cols:
            part["volume"] = pd.to_numeric(frame[cols["volume"]], errors="coerce").fillna(0.0)
        else:
            part["volume"] = 0.0
        if "dividends" in cols:
            part["dividends"] = pd.to_numeric(frame[cols["dividends"]], errors="coerce").fillna(0.0)
        else:
            part["dividends"] = 0.0
        if "stocksplits" in cols:
            part["stock_splits"] = pd.to_numeric(frame[cols["stocksplits"]], errors="coerce").fillna(0.0)
        else:
            part["stock_splits"] = 0.0
        part = part.dropna(subset=["open", "high", "low", "close"])
        if not part.empty:
            parts.append(part)

    if not parts:
        return pd.DataFrame(
            columns=["symbol", "date", "open", "high", "low", "close", "volume", "adj_close", "dividends", "stock_splits"]
        )

    out = pd.concat(parts, axis=0, ignore_index=True)
    out = out.drop_duplicates(subset=["symbol", "date"], keep="last")
    return out


def append_metrics_to_shared_sqlite(
    metrics_df: pd.DataFrame,
    *,
    db_path: Path,
    only_after: str | None = None,
) -> int:
    long_df = _metrics_wide_to_long(metrics_df, only_after=only_after)
    if long_df.empty:
        _log("No SQLite rows to add from metrics CSV")
        return 0

    db_path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(
        long_df[["symbol", "date", "open", "high", "low", "close", "volume", "adj_close", "dividends", "stock_splits"]]
        .itertuples(index=False, name=None)
    )
    _log(f"Syncing up to {len(rows)} rows into SQLite without overwriting existing keys: {db_path}")

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _ensure_prices_table_schema(conn)
        before_insert_changes = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO prices
            (symbol, date, open, high, low, close, volume, adj_close, dividends, stock_splits)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        inserted_rows = conn.total_changes - before_insert_changes
        enrichment_rows = list(
            long_df[["adj_close", "dividends", "stock_splits", "symbol", "date"]].itertuples(index=False, name=None)
        )
        conn.executemany(
            """
            UPDATE prices
            SET adj_close = ?, dividends = ?, stock_splits = ?
            WHERE symbol = ? AND date = ?
            """,
            enrichment_rows,
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prices_symbol_date ON prices(symbol, date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date)")
        conn.commit()
    return int(inserted_rows)


def _market_cap_wide_to_long(market_cap_df: pd.DataFrame, *, only_after: str | None = None) -> pd.DataFrame:
    if market_cap_df.empty:
        return pd.DataFrame(columns=["symbol", "date", "market_cap"])

    frame = market_cap_df.copy()
    frame.index = pd.to_datetime(frame.index, errors="coerce").normalize()
    frame = frame[~frame.index.isna()].sort_index()
    if only_after:
        cutoff = pd.Timestamp(only_after).normalize()
        frame = frame[frame.index > cutoff]
    if frame.empty:
        return pd.DataFrame(columns=["symbol", "date", "market_cap"])

    parts: list[pd.DataFrame] = []
    for symbol in frame.columns:
        series = pd.to_numeric(frame[symbol], errors="coerce").dropna()
        if series.empty:
            continue
        parts.append(
            pd.DataFrame(
                {
                    "symbol": str(symbol).strip().upper(),
                    "date": series.index.strftime("%Y-%m-%d"),
                    "market_cap": series.astype(float).values,
                }
            )
        )
    if not parts:
        return pd.DataFrame(columns=["symbol", "date", "market_cap"])
    out = pd.concat(parts, axis=0, ignore_index=True)
    return out.drop_duplicates(subset=["symbol", "date"], keep="last")


def append_market_caps_to_shared_sqlite(
    market_cap_df: pd.DataFrame,
    *,
    db_path: Path,
    only_after: str | None = None,
) -> int:
    long_df = _market_cap_wide_to_long(market_cap_df, only_after=only_after)
    if long_df.empty:
        _log("No SQLite market cap rows to update")
        return 0

    db_path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(long_df[["market_cap", "symbol", "date"]].itertuples(index=False, name=None))
    _log(f"Updating up to {len(rows)} market cap values in SQLite: {db_path}")

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _ensure_prices_table_schema(conn)
        before_changes = conn.total_changes
        conn.executemany(
            """
            UPDATE prices
            SET market_cap = ?
            WHERE symbol = ? AND date = ?
            """,
            rows,
        )
        conn.commit()
        updated_rows = conn.total_changes - before_changes
    return int(updated_rows)


def refresh_sp500_shared_prices(
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    shared_db_root: Path = DEFAULT_SHARED_DB_ROOT,
    symbols_csv: Path = DEFAULT_COMPONENTS_CSV,
    start_date: str = DEFAULT_START_DATE,
    chunk_size: int = 50,
    provider: str = "auto",
    pause_seconds: float = 0.0,
) -> RefreshResult:
    metrics_csv_path = data_dir / DEFAULT_METRICS_CSV.name
    shares_csv_path = data_dir / DEFAULT_SHARES_CSV.name
    market_cap_csv_path = data_dir / DEFAULT_MARKET_CAP_CSV.name
    sqlite_path = shared_db_root / DEFAULT_SQLITE_NAME

    _log(f"Loading symbol universe from {symbols_csv}")
    symbols = _read_symbol_list(symbols_csv, sqlite_path)
    end_date = (pd.Timestamp.today().normalize() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    old_max_date = _sqlite_max_date(sqlite_path)
    existing_market_cap_count = _sqlite_market_cap_value_count(sqlite_path)
    price_fetch_start_date = _incremental_fetch_start_date(start_date, old_max_date)
    _log(
        f"Starting refresh: symbols={len(symbols)}, start_date={start_date}, "
        f"price_fetch_start={price_fetch_start_date}, "
        f"end_date_exclusive={end_date}, provider={provider}, chunk_size={chunk_size}"
    )
    _log(f"Existing SQLite max date: {old_max_date}")
    _log(f"Existing SQLite market_cap non-null rows: {existing_market_cap_count}")
    if old_max_date and price_fetch_start_date != start_date:
        _log(f"Using incremental price download from {price_fetch_start_date} based on SQLite max date")

    _log("Reading existing local CSV snapshots")
    existing_metrics_df = _read_wide_csv(metrics_csv_path)
    existing_shares_df = _read_wide_csv(shares_csv_path)
    existing_market_cap_df = _read_wide_csv(market_cap_csv_path)

    if _has_fetch_window(price_fetch_start_date, end_date):
        fresh_frames, missing_symbols = _download_symbol_frames(
            symbols,
            start_date=price_fetch_start_date,
            end_date=end_date,
            chunk_size=max(1, int(chunk_size)),
            provider=provider,
            pause_seconds=max(0.0, float(pause_seconds)),
        )
    else:
        fresh_frames = {}
        missing_symbols = list(symbols)
        _log(
            f"Skipping price download: incremental start {price_fetch_start_date} "
            f"is not before end_date_exclusive {end_date}"
        )
    _, fresh_metrics_df = _build_output_frames(fresh_frames)
    if fresh_metrics_df.empty and existing_metrics_df.empty:
        raise RuntimeError("No fresh S&P 500 price data was downloaded from the configured providers")
    if fresh_metrics_df.empty:
        _log("No new price rows downloaded; keeping existing local snapshots")
    else:
        _log(
            f"Built fresh frames: metrics_rows={len(fresh_metrics_df)}, "
            f"fresh_symbols={len(fresh_frames)}, stale_symbols={len(missing_symbols)}"
        )

    final_metrics_df = _overlay_fresh_on_existing(fresh_metrics_df, existing_metrics_df)

    if final_metrics_df.empty:
        raise RuntimeError("Refresh produced empty output after merging with existing CSV files")

    _write_wide_csv_atomic(final_metrics_df, metrics_csv_path)

    sqlite_added_rows = append_metrics_to_shared_sqlite(
        final_metrics_df,
        db_path=sqlite_path,
        only_after=None,
    )
    inferred_shares_histories = _infer_share_histories_from_existing_market_caps(
        final_metrics_df,
        existing_market_cap_df,
        symbols,
    )
    inferred_shares_df = _share_histories_to_wide(inferred_shares_histories)
    base_shares_df = _overlay_fresh_on_existing(inferred_shares_df, existing_shares_df)
    shares_max_date = _wide_non_null_max_date(base_shares_df)
    shares_fetch_start_date = _incremental_fetch_start_date(start_date, shares_max_date)
    if shares_max_date:
        _log(f"Using incremental shares download from {shares_fetch_start_date} based on shares max date {shares_max_date}")

    if _has_fetch_window(shares_fetch_start_date, end_date):
        shares_histories, missing_market_cap_symbols = _download_shares_histories(
            symbols,
            start_date=shares_fetch_start_date,
            end_date=end_date,
            provider=provider,
            chunk_size=max(1, int(chunk_size)),
            pause_seconds=max(0.0, float(pause_seconds)),
        )
    else:
        shares_histories = {}
        missing_market_cap_symbols = list(symbols)
        _log(
            f"Skipping market cap share-history download: start {shares_fetch_start_date} "
            f"is not before end_date_exclusive {end_date}"
        )

    fetched_shares_df = _share_histories_to_wide(shares_histories)
    final_shares_df = _overlay_fresh_on_existing(fetched_shares_df, base_shares_df)
    if not final_shares_df.empty:
        _write_wide_csv_atomic(final_shares_df, shares_csv_path)
    else:
        _log("No shares CSV changes to write")

    if missing_market_cap_symbols:
        covered = [s for s in missing_market_cap_symbols if s in final_shares_df.columns] if not final_shares_df.empty else []
        if covered:
            _log(f"Recovered shares from local snapshots for {len(covered)} symbols")
            missing_market_cap_symbols = [s for s in missing_market_cap_symbols if s not in covered]
    if missing_market_cap_symbols:
        _log(f"Market cap share histories still missing for {len(missing_market_cap_symbols)} symbols")
    fresh_market_cap_df = _build_market_cap_frame_from_latest_shares(final_metrics_df, final_shares_df)
    final_market_cap_df = _overlay_fresh_on_existing(fresh_market_cap_df, existing_market_cap_df)
    if not final_market_cap_df.empty:
        _write_wide_csv_atomic(final_market_cap_df, market_cap_csv_path)
    else:
        _log("No market cap CSV changes to write")

    sqlite_market_cap_updated_rows = append_market_caps_to_shared_sqlite(
        final_market_cap_df,
        db_path=sqlite_path,
        only_after=None,
    )
    new_max_date = _sqlite_max_date(sqlite_path)
    _log(
        f"SQLite append complete: added_rows={sqlite_added_rows}, "
        f"market_cap_updates={sqlite_market_cap_updated_rows}, new_max_date={new_max_date}"
    )

    return RefreshResult(
        symbol_count=len(symbols),
        fresh_symbol_count=len(fresh_frames),
        stale_symbol_count=len(missing_symbols),
        market_cap_fresh_symbol_count=len(shares_histories),
        metrics_rows=len(final_metrics_df),
        market_cap_rows=len(final_market_cap_df),
        sqlite_added_rows=sqlite_added_rows,
        sqlite_market_cap_updated_rows=sqlite_market_cap_updated_rows,
        sqlite_old_max_date=old_max_date,
        sqlite_new_max_date=new_max_date,
        metrics_csv_path=metrics_csv_path,
        shares_csv_path=shares_csv_path,
        market_cap_csv_path=market_cap_csv_path,
        sqlite_path=sqlite_path,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh wide S&P 500 CSV files and append only new dates into the shared SQLite DB."
    )
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="Data directory containing output CSV files")
    parser.add_argument("--shared-db-root", default=str(DEFAULT_SHARED_DB_ROOT), help="Shared DB root directory")
    parser.add_argument("--symbols-csv", default=str(DEFAULT_COMPONENTS_CSV), help="Universe CSV with Symbol column")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE, help="Download start date (YYYY-MM-DD)")
    parser.add_argument("--chunk-size", type=int, default=50, help="Ticker count per yfinance batch")
    parser.add_argument(
        "--provider",
        choices=["auto", "yfinance", "fdr"],
        default="auto",
        help="Price provider strategy",
    )
    parser.add_argument("--pause-seconds", type=float, default=0.0, help="Sleep between provider calls")
    parser.add_argument("--insecure-ssl", action="store_true", help="Disable TLS verification for temporary testing")
    parser.add_argument("--ca-bundle", default="", help="Custom CA bundle path")
    return parser.parse_args()


def main() -> int:
    global _CANCEL_REQUESTED
    args = _parse_args()
    _CANCEL_REQUESTED = False
    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        configure_ssl(insecure_ssl=bool(args.insecure_ssl), ca_bundle=str(args.ca_bundle).strip() or None)
        result = refresh_sp500_shared_prices(
            data_dir=Path(args.data_dir),
            shared_db_root=Path(args.shared_db_root),
            symbols_csv=Path(args.symbols_csv),
            start_date=str(args.start_date).strip() or DEFAULT_START_DATE,
            chunk_size=int(args.chunk_size),
            provider=str(args.provider).strip().lower(),
            pause_seconds=float(args.pause_seconds),
        )
    except KeyboardInterrupt:
        _log("Cancelled by user (Ctrl+C).")
        return 130
    except Exception as exc:
        hint = security_hint(exc, output_dir=Path(args.data_dir))
        if hint:
            print(hint, file=sys.stderr)
        _log(
            "SUMMARY "
            f"status=error error_type={type(exc).__name__} "
            f"data_dir={Path(args.data_dir)}"
        )
        print(f"Refresh failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    print(
        "Refreshed S&P 500 shared prices "
        f"(symbols={result.symbol_count}, fresh={result.fresh_symbol_count}, "
        f"sqlite_added={result.sqlite_added_rows}, market_cap_updates={result.sqlite_market_cap_updated_rows}, "
        f"sqlite_max={result.sqlite_new_max_date})"
    )
    print(
        f"Outputs: {result.metrics_csv_path}, {result.shares_csv_path}, "
        f"{result.market_cap_csv_path}, db={result.sqlite_path}"
    )
    _log(
        "SUMMARY "
        f"status=ok symbols={result.symbol_count} fresh={result.fresh_symbol_count} "
        f"sqlite_added={result.sqlite_added_rows} market_cap_updates={result.sqlite_market_cap_updated_rows} "
        f"sqlite_max={result.sqlite_new_max_date}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
