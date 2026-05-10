from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pandas as pd
import requests

try:
    from fredapi import Fred
except Exception:  # pragma: no cover - optional dependency
    Fred = None

try:
    import yfinance as yf
except Exception:  # pragma: no cover - optional dependency
    yf = None

try:
    from pykrx import stock as pykrx_stock
except Exception:  # pragma: no cover - optional dependency
    pykrx_stock = None

from .macro_data_store import (
    ALL_SPECS,
    DAILY_MACRO_SPECS,
    FRED_SPECS,
    GLOBAL_COMPARISON_SPECS,
    KOREA_MACRO_SPECS,
    MacroSeriesSpec,
    ensure_macro_schema,
    macro_db_path,
    normalize_series,
    read_local_series,
)


@dataclass(frozen=True)
class MacroRefreshResult:
    db_path: Path
    min_date: str
    max_date: str
    inserted_or_replaced_rows: int
    total_rows: int
    series_count: int
    size_bytes: int


def _log(message: str) -> None:
    print(f"[refresh-krx-macro] {message}", flush=True)


def _safe_error_message(exc: Exception, *secrets: str) -> str:
    text = str(exc)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _start_date(years: int) -> pd.Timestamp:
    return (pd.Timestamp.today().normalize() - pd.DateOffset(years=int(years))).normalize()


def _fetch_fred_series(fred: Fred | None, spec: MacroSeriesSpec, start: pd.Timestamp) -> tuple[pd.Series | None, str | None]:
    if not spec.fred_id:
        return None, None
    fred_key = str(os.getenv("FRED_API_KEY", "")).strip()
    if fred_key:
        try:
            response = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id": spec.fred_id,
                    "api_key": fred_key,
                    "file_type": "json",
                    "observation_start": start.strftime("%Y-%m-%d"),
                },
                timeout=30,
            )
            response.raise_for_status()
            observations = response.json().get("observations", [])
            rows = []
            for obs in observations:
                date = pd.to_datetime(obs.get("date"), errors="coerce")
                value = pd.to_numeric(obs.get("value"), errors="coerce")
                if pd.isna(date) or pd.isna(value):
                    continue
                rows.append((pd.Timestamp(date).normalize(), float(value)))
            if rows:
                series = pd.Series([value for _, value in rows], index=[date for date, _ in rows], name=spec.series_id)
                series = normalize_series(series, series_id=spec.series_id, start_date=start)
                if not series.empty:
                    return series, f"fred:{spec.fred_id}"
        except Exception as exc:
            _log(f"FRED REST failed for {spec.series_id} ({spec.fred_id}): {type(exc).__name__}: {_safe_error_message(exc, fred_key)}")
            return None, None
    if fred is None:
        return None, None
    try:
        raw = fred.get_series(spec.fred_id, observation_start=start.strftime("%Y-%m-%d"))
    except Exception as exc:
        _log(f"FRED failed for {spec.series_id} ({spec.fred_id}): {type(exc).__name__}: {_safe_error_message(exc, fred_key)}")
        return None, None
    series = normalize_series(pd.Series(raw), series_id=spec.series_id, start_date=start)
    if series.empty:
        return None, None
    return series, f"fred:{spec.fred_id}"


def _ecos_time(value: pd.Timestamp, cycle: str) -> str:
    cycle = cycle.upper()
    if cycle == "D":
        return value.strftime("%Y%m%d")
    if cycle == "M":
        return value.strftime("%Y%m")
    if cycle == "Q":
        quarter = ((int(value.month) - 1) // 3) + 1
        return f"{value.year}Q{quarter}"
    if cycle == "A":
        return value.strftime("%Y")
    return value.strftime("%Y%m%d")


def _parse_ecos_date(value: object, cycle: str) -> pd.Timestamp | None:
    text = str(value or "").strip()
    if not text:
        return None
    cycle = cycle.upper()
    try:
        if cycle == "D":
            return pd.to_datetime(text, format="%Y%m%d", errors="coerce")
        if cycle == "M":
            return pd.to_datetime(text + "01", format="%Y%m%d", errors="coerce")
        if cycle == "Q":
            if "Q" in text.upper():
                year_text, q_text = text.upper().split("Q", 1)
                quarter = int(q_text[:1])
            else:
                year_text, q_text = text[:4], text[4:]
                quarter = int(q_text[:1])
            month = (quarter - 1) * 3 + 1
            return pd.Timestamp(int(year_text), month, 1)
        if cycle == "A":
            return pd.Timestamp(int(text[:4]), 1, 1)
    except Exception:
        return None
    return pd.to_datetime(text, errors="coerce")


def _fetch_ecos_series(api_key: str, spec: MacroSeriesSpec, start: pd.Timestamp) -> tuple[pd.Series | None, str | None]:
    if not api_key or not spec.ecos_stat_code or not spec.ecos_cycle:
        return None, None
    cycle = spec.ecos_cycle.upper()
    end = pd.Timestamp.today().normalize()
    url_parts = [
        "https://ecos.bok.or.kr/api/StatisticSearch",
        api_key,
        "json",
        "kr",
        "1",
        "100000",
        spec.ecos_stat_code,
        cycle,
        _ecos_time(start, cycle),
        _ecos_time(end, cycle),
    ]
    for item_code in [spec.ecos_item_code1, spec.ecos_item_code2, spec.ecos_item_code3, spec.ecos_item_code4]:
        if item_code:
            url_parts.append(item_code)
    url = "/".join(url_parts)
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        _log(f"ECOS failed for {spec.series_id} ({spec.ecos_stat_code}): {type(exc).__name__}: {_safe_error_message(exc, api_key)}")
        return None, None

    body = payload.get("StatisticSearch") if isinstance(payload, dict) else None
    rows = body.get("row") if isinstance(body, dict) else None
    if not rows:
        err = payload.get("RESULT") if isinstance(payload, dict) else None
        msg = err.get("MESSAGE") if isinstance(err, dict) else "empty response"
        _log(f"ECOS missing {spec.series_id} ({spec.ecos_stat_code}): {msg}")
        return None, None

    points: list[tuple[pd.Timestamp, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        date = _parse_ecos_date(row.get("TIME"), cycle)
        value = pd.to_numeric(row.get("DATA_VALUE"), errors="coerce")
        if date is None or pd.isna(date) or pd.isna(value):
            continue
        points.append((pd.Timestamp(date).normalize(), float(value)))
    if not points:
        return None, None
    series = pd.Series([value for _, value in points], index=[date for date, _ in points], name=spec.series_id)
    series = normalize_series(series, series_id=spec.series_id, start_date=start)
    if series.empty:
        return None, None
    return series, f"ecos:{spec.ecos_stat_code}:{spec.ecos_cycle}:{spec.ecos_item_code1 or ''}"


def _fetch_yahoo_series(spec: MacroSeriesSpec, start: pd.Timestamp) -> tuple[pd.Series | None, str | None]:
    if yf is None or not spec.yahoo_symbol:
        return None, None
    try:
        raw = yf.download(
            spec.yahoo_symbol,
            start=start.strftime("%Y-%m-%d"),
            progress=False,
            auto_adjust=False,
            threads=False,
        )
    except Exception as exc:
        _log(f"Yahoo failed for {spec.series_id} ({spec.yahoo_symbol}): {type(exc).__name__}: {exc}")
        return None, None
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        return None, None
    close = raw.get("Close")
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0] if not close.empty else None
    if close is None:
        return None, None
    series = normalize_series(pd.Series(close), series_id=spec.series_id, start_date=start)
    if series.empty:
        return None, None
    return series, f"yahoo:{spec.yahoo_symbol}"


@lru_cache(maxsize=4)
def _fetch_treasury_curve_frame(start_year: int, end_year: int) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for year in range(int(start_year), int(end_year) + 1):
        url = (
            "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView"
            f"?type=daily_treasury_yield_curve&field_tdr_date_value={year}"
        )
        try:
            tables = pd.read_html(url)
        except Exception as exc:
            _log(f"Treasury.gov yield curve failed for {year}: {type(exc).__name__}: {exc}")
            continue
        if not tables:
            continue
        frame = tables[0].copy()
        if "Date" not in frame.columns:
            continue
        frame["date"] = pd.to_datetime(frame["Date"], errors="coerce")
        mapping = {"2 Yr": "DGS2", "10 Yr": "DGS10", "30 Yr": "DGS30"}
        keep = ["date", *[col for col in mapping if col in frame.columns]]
        frame = frame[keep].rename(columns=mapping)
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True).dropna(subset=["date"]).sort_values("date")
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    out = out.dropna(subset=["date"]).drop_duplicates(subset=["date"], keep="last")
    return out.set_index("date")


def _fetch_treasury_direct_series(spec: MacroSeriesSpec, start: pd.Timestamp) -> tuple[pd.Series | None, str | None]:
    if spec.series_id not in {"DGS2", "DGS10", "DGS30"}:
        return None, None
    end = pd.Timestamp.today().normalize()
    frame = _fetch_treasury_curve_frame(int(start.year), int(end.year))
    if frame.empty or spec.series_id not in frame.columns:
        return None, None
    series = normalize_series(frame[spec.series_id], series_id=spec.series_id, start_date=start)
    if series.empty:
        return None, None
    return series, "treasury.gov:daily_treasury_yield_curve"


def _fetch_pykrx_index_series(spec: MacroSeriesSpec, start: pd.Timestamp) -> tuple[pd.Series | None, str | None]:
    if spec.series_id != "KOSPI200" or pykrx_stock is None:
        return None, None
    end = pd.Timestamp.today().normalize()
    try:
        raw = pykrx_stock.get_index_ohlcv_by_date(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "1028")
    except Exception as exc:
        _log(f"pykrx index failed for {spec.series_id}: {type(exc).__name__}: {exc}")
        return None, None
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        return None, None
    close_col = "종가" if "종가" in raw.columns else "Close" if "Close" in raw.columns else None
    if close_col is None:
        return None, None
    series = normalize_series(pd.Series(raw[close_col]), series_id=spec.series_id, start_date=start)
    if series.empty:
        return None, None
    return series, "pykrx:index:1028"


def _load_series(spec: MacroSeriesSpec, *, fred: Fred | None, ecos_key: str, start: pd.Timestamp) -> tuple[pd.Series | None, str | None]:
    index_series, index_source = _fetch_pykrx_index_series(spec, start)
    if index_series is not None and not index_series.empty:
        return index_series, index_source

    ecos_series, ecos_source = _fetch_ecos_series(ecos_key, spec, start)
    if ecos_series is not None and not ecos_series.empty:
        return ecos_series, ecos_source

    tried_primary_fred = False
    if spec.series_id in {"DXY", "SP500", "DGS2", "DGS10", "DGS30"}:
        tried_primary_fred = True
        fred_series, fred_source = _fetch_fred_series(fred, spec, start)
        if fred_series is not None and not fred_series.empty:
            return fred_series, fred_source
        treasury_series, treasury_source = _fetch_treasury_direct_series(spec, start)
        if treasury_series is not None and not treasury_series.empty:
            return treasury_series, treasury_source

    yahoo_series, yahoo_source = _fetch_yahoo_series(spec, start)
    if yahoo_series is not None and not yahoo_series.empty:
        return yahoo_series, yahoo_source
    if spec.local_csv:
        local = read_local_series(spec.local_csv, series_id=spec.local_name or spec.series_id, start_date=start)
        if local is not None and not local.empty:
            local = local.rename(spec.series_id)
            if spec.fred_id is None or tried_primary_fred:
                return local, f"local_csv:{spec.local_csv}"
            fred_series, fred_source = _fetch_fred_series(fred, spec, start)
            if fred_series is None or fred_series.empty:
                return local, f"local_csv:{spec.local_csv}"
            merged = pd.concat([local, fred_series]).sort_index()
            merged = merged[~merged.index.duplicated(keep="last")].rename(spec.series_id)
            return merged, f"local_csv:{spec.local_csv}+{fred_source}"
    if tried_primary_fred:
        return None, None
    return _fetch_fred_series(fred, spec, start)


def _upsert_series(conn: sqlite3.Connection, spec: MacroSeriesSpec, series: pd.Series, source: str) -> int:
    clean = normalize_series(series, series_id=spec.series_id)
    if clean.empty:
        return 0
    rows = [
        (spec.series_id, pd.Timestamp(idx).strftime("%Y-%m-%d"), float(value), spec.dataset, spec.frequency, source)
        for idx, value in clean.items()
    ]
    conn.executemany(
        """
        INSERT INTO macro_series (series_id, date, value, dataset, frequency, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(series_id, date) DO UPDATE SET
            value=excluded.value,
            dataset=excluded.dataset,
            frequency=excluded.frequency,
            source=excluded.source,
            updated_at=CURRENT_TIMESTAMP
        """,
        rows,
    )
    conn.execute(
        """
        INSERT INTO macro_metadata (series_id, dataset, frequency, source, min_date, max_date, row_count, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(series_id) DO UPDATE SET
            dataset=excluded.dataset,
            frequency=excluded.frequency,
            source=excluded.source,
            min_date=excluded.min_date,
            max_date=excluded.max_date,
            row_count=excluded.row_count,
            updated_at=CURRENT_TIMESTAMP
        """,
        (
            spec.series_id,
            spec.dataset,
            spec.frequency,
            source,
            pd.Timestamp(clean.index.min()).strftime("%Y-%m-%d"),
            pd.Timestamp(clean.index.max()).strftime("%Y-%m-%d"),
            int(len(clean)),
        ),
    )
    return len(rows)


def refresh_macro_prices(
    *,
    db_path: str | Path | None = None,
    years: int = 5,
    require_fred: bool = False,
    require_ecos: bool = False,
    korea_only: bool = False,
    daily_core: bool = False,
) -> MacroRefreshResult:
    start = _start_date(years)
    target = macro_db_path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fred_key = str(os.getenv("FRED_API_KEY", "")).strip()
    fred = Fred(api_key=fred_key) if Fred is not None and fred_key else None
    if require_fred and fred is None:
        raise RuntimeError("fredapi is not installed or FRED_API_KEY is missing")
    ecos_key = str(os.getenv("KOREA_ECOS_API_KEY", "") or os.getenv("ECOS_API_KEY", "")).strip()
    if require_ecos and not ecos_key:
        raise RuntimeError("KOREA_ECOS_API_KEY or ECOS_API_KEY is missing")

    changed = 0
    missing: list[str] = []
    with sqlite3.connect(target) as conn:
        ensure_macro_schema(conn)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("DELETE FROM macro_series WHERE date < ?", (start.strftime("%Y-%m-%d"),))

        specs = DAILY_MACRO_SPECS if daily_core else KOREA_MACRO_SPECS if korea_only else ALL_SPECS
        for spec in specs:
            series, source = _load_series(spec, fred=fred, ecos_key=ecos_key, start=start)
            if series is None or series.empty or source is None:
                missing.append(spec.series_id)
                _log(f"missing {spec.series_id}")
                continue
            count = _upsert_series(conn, spec, series, source)
            changed += count
            _log(f"{spec.series_id}: rows={count} source={source}")

        row = conn.execute("SELECT MIN(date), MAX(date), COUNT(*), COUNT(DISTINCT series_id) FROM macro_series").fetchone()
        conn.commit()
        conn.execute("VACUUM")

    if require_fred:
        required_specs = GLOBAL_COMPARISON_SPECS if daily_core else FRED_SPECS
        required = {spec.series_id for spec in required_specs if spec.fred_id}
        missing_required = sorted(required.intersection(missing))
        if missing_required:
            raise RuntimeError(f"Required FRED series missing: {', '.join(missing_required)}")
    if require_ecos:
        required = {spec.series_id for spec in KOREA_MACRO_SPECS if spec.ecos_stat_code}
        missing_required = sorted(required.intersection(missing))
        if missing_required:
            raise RuntimeError(f"Required ECOS series missing: {', '.join(missing_required)}")
    if missing:
        _log(f"missing series skipped: {', '.join(missing)}")
    return MacroRefreshResult(
        db_path=target,
        min_date=str(row[0] or "-") if row else "-",
        max_date=str(row[1] or "-") if row else "-",
        inserted_or_replaced_rows=changed,
        total_rows=int(row[2] or 0) if row else 0,
        series_count=int(row[3] or 0) if row else 0,
        size_bytes=target.stat().st_size if target.exists() else 0,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh KRX macro market/Korea ECOS/FRED data into data/macro_prices.sqlite.")
    parser.add_argument("--db-path", default="", help="Output SQLite path. Default: data/macro_prices.sqlite")
    parser.add_argument("--years", type=int, default=5, help="History window to keep. Default: 5 years")
    parser.add_argument("--require-fred", action="store_true", help="Fail if any required FRED series cannot be loaded.")
    parser.add_argument("--require-ecos", action="store_true", help="Fail if any required Korea ECOS series cannot be loaded.")
    parser.add_argument("--korea-only", action="store_true", help="Refresh only Korea ECOS/local macro series.")
    parser.add_argument("--daily-core", action="store_true", help="Refresh Korea ECOS plus DXY, S&P 500, and US Treasury 2Y/10Y/30Y.")
    args = parser.parse_args(argv)
    try:
        result = refresh_macro_prices(
            db_path=args.db_path or None,
            years=args.years,
            require_fred=bool(args.require_fred),
            require_ecos=bool(args.require_ecos),
            korea_only=bool(args.korea_only),
            daily_core=bool(args.daily_core),
        )
    except Exception as exc:
        print(f"Refresh failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    _log(
        "done "
        f"db={result.db_path} min_date={result.min_date} max_date={result.max_date} "
        f"rows={result.total_rows} series={result.series_count} changed={result.inserted_or_replaced_rows} "
        f"size_bytes={result.size_bytes}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
