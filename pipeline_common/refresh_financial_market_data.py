from __future__ import annotations

import argparse
from collections.abc import Callable
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .financial_market_prices_sql import sync_financial_market_prices_from_csvs
from .notebook_data import load_btc_close, load_dxy_series, load_fx_close, load_index_series, load_yield_curve_df
from .security import configure_ssl, security_hint


DEFAULT_DATA_DIR = Path("data")
DEFAULT_START_DATE = "2012-01-02"
YIELD_SERIES_IDS = [
    "DGS1MO",
    "DGS3MO",
    "DGS6MO",
    "DGS1",
    "DGS2",
    "DGS3",
    "DGS5",
    "DGS7",
    "DGS10",
    "DGS20",
    "DGS30",
]


@dataclass(frozen=True)
class RefreshFinancialMarketResult:
    data_dir: Path
    sqlite_path: Path
    rows_written: dict[str, int]
    sqlite_rows_added: int
    dataset_sources: dict[str, str]


def _last_completed_market_day() -> pd.Timestamp:
    return pd.Timestamp.today().normalize() - pd.Timedelta(days=1)


def _ensure_anchor_series(series: pd.Series, *, start_date: str, name: str) -> pd.Series:
    out = pd.Series(series.copy())
    out.index = pd.to_datetime(out.index, errors="coerce").normalize()
    out = out[~out.index.isna()].sort_index()
    if out.empty:
        return out
    anchor = pd.Timestamp(start_date).normalize()
    if out.index.min() > anchor:
        out.loc[anchor] = float(out.iloc[0])
        out = out.sort_index()
    out.name = name
    return out[~out.index.duplicated(keep="first")]


def _ensure_anchor_frame(frame: pd.DataFrame, *, start_date: str) -> pd.DataFrame:
    out = frame.copy()
    out.index = pd.to_datetime(out.index, errors="coerce").normalize()
    out = out[~out.index.isna()].sort_index()
    if out.empty:
        return out
    anchor = pd.Timestamp(start_date).normalize()
    if out.index.min() > anchor:
        out.loc[anchor] = out.iloc[0]
        out = out.sort_index()
    return out[~out.index.duplicated(keep="first")]


def _clip_series_through(series: pd.Series, *, end_date: pd.Timestamp) -> pd.Series:
    out = pd.Series(series.copy())
    out.index = pd.to_datetime(out.index, errors="coerce").normalize()
    out = out[~out.index.isna()].sort_index()
    out = out[out.index <= pd.Timestamp(end_date).normalize()]
    return out[~out.index.duplicated(keep="last")]


def _clip_frame_through(frame: pd.DataFrame, *, end_date: pd.Timestamp) -> pd.DataFrame:
    out = frame.copy()
    out.index = pd.to_datetime(out.index, errors="coerce").normalize()
    out = out[~out.index.isna()].sort_index()
    out = out[out.index <= pd.Timestamp(end_date).normalize()]
    return out[~out.index.duplicated(keep="last")]


def _read_series_csv(path: Path, *, name: str) -> pd.Series:
    if not path.exists() or not path.is_file():
        return pd.Series(dtype=float, name=name)
    raw = pd.read_csv(path)
    if raw.empty:
        return pd.Series(dtype=float, name=name)
    cols = {str(c).strip().lower(): c for c in raw.columns}
    date_col = cols.get("date") or cols.get("datetime") or raw.columns[0]
    value_col = cols.get("close") or cols.get("value") or cols.get("price") or raw.columns[-1]
    out = raw[[date_col, value_col]].copy()
    out.columns = ["date", "value"]
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    out = out.dropna(subset=["date", "value"]).sort_values("date")
    if out.empty:
        return pd.Series(dtype=float, name=name)
    series = pd.Series(out["value"].values, index=out["date"], name=name)
    return series[~series.index.duplicated(keep="last")].sort_index()


def _read_frame_csv(path: Path, *, columns: list[str]) -> pd.DataFrame:
    if not path.exists() or not path.is_file():
        return pd.DataFrame(columns=columns)
    raw = pd.read_csv(path)
    if raw.empty:
        return pd.DataFrame(columns=columns)
    cols = {str(c).strip().lower(): c for c in raw.columns}
    date_col = cols.get("date") or cols.get("datetime") or raw.columns[0]
    out = raw.copy()
    out[date_col] = pd.to_datetime(out[date_col], errors="coerce").dt.normalize()
    out = out.dropna(subset=[date_col]).set_index(date_col).sort_index()
    keep = [c for c in columns if c in out.columns]
    if not keep:
        return pd.DataFrame(columns=columns)
    out = out[keep].apply(pd.to_numeric, errors="coerce")
    return out[~out.index.duplicated(keep="last")].sort_index()


def _fetch_start_from_existing(existing: pd.Series | pd.DataFrame, *, default_start: str) -> str:
    if existing is None or len(existing.index) == 0:
        return default_start
    try:
        return pd.Timestamp(existing.index.max()).normalize().strftime("%Y-%m-%d")
    except Exception:
        return default_start


def _overlay_series(fresh: pd.Series, existing: pd.Series) -> pd.Series:
    if existing.empty:
        return fresh.sort_index()
    if fresh.empty:
        return existing.sort_index()
    out = fresh.combine_first(existing).sort_index()
    return out[~out.index.duplicated(keep="last")]


def _overlay_frame(fresh: pd.DataFrame, existing: pd.DataFrame) -> pd.DataFrame:
    if existing.empty:
        return fresh.sort_index()
    if fresh.empty:
        return existing.sort_index()
    out = fresh.combine_first(existing).sort_index()
    return out[~out.index.duplicated(keep="last")]


def _write_series_csv(path: Path, series: pd.Series) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(pd.Index(series.index), errors="coerce").normalize(),
            "close": pd.to_numeric(series.to_numpy(), errors="coerce"),
        }
    )
    out = out.dropna().sort_values("date")
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    out.to_csv(path, index=False, encoding="utf-8")
    return int(len(out))


def _write_frame_csv(path: Path, frame: pd.DataFrame) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = frame.copy()
    out.index = pd.to_datetime(out.index, errors="coerce").normalize()
    out.index.name = "date"
    out = out.sort_index()
    out.reset_index().to_csv(path, index=False, encoding="utf-8")
    return int(len(out))


def refresh_financial_market_data(
    *,
    data_dir: Path | str = DEFAULT_DATA_DIR,
    start_date: str = DEFAULT_START_DATE,
    on_progress: Callable[[str, str, str, int, str, str], None] | None = None,
) -> RefreshFinancialMarketResult:
    root = Path(data_dir)
    rows_written: dict[str, int] = {}
    dataset_sources: dict[str, str] = {}
    last_completed_day = _last_completed_market_day()

    def _emit(dataset: str, status: str, source: str = "", rows: int = 0, saved_csv: str = "", error: str = "") -> None:
        if on_progress is not None:
            on_progress(dataset, status, source, rows, saved_csv, error)

    _emit("treasury_yields", "running")
    yield_path = root / "treasury_yields.csv"
    yield_existing = _read_frame_csv(yield_path, columns=YIELD_SERIES_IDS)
    yield_fetch_start = _fetch_start_from_existing(yield_existing, default_start=start_date)
    yield_df, yield_source = load_yield_curve_df(YIELD_SERIES_IDS, start=yield_fetch_start)
    yield_final = _overlay_frame(yield_df[YIELD_SERIES_IDS], yield_existing)
    yield_final = _ensure_anchor_frame(yield_final, start_date=start_date)
    rows_written["treasury_yields"] = _write_frame_csv(yield_path, yield_final)
    dataset_sources["treasury_yields"] = yield_source
    _emit("treasury_yields", "done", yield_source, rows_written["treasury_yields"], str(yield_path.resolve()), "")

    _emit("dxy", "running")
    dxy_path = root / "dxy.csv"
    dxy_existing = _read_series_csv(dxy_path, name="DXY")
    dxy_existing = _clip_series_through(dxy_existing, end_date=last_completed_day)
    dxy_fetch_start = _fetch_start_from_existing(dxy_existing, default_start=start_date)
    dxy, dxy_source = load_dxy_series(start=dxy_fetch_start)
    dxy_final = _overlay_series(dxy.rename("DXY"), dxy_existing)
    dxy_final = _ensure_anchor_series(dxy_final, start_date=start_date, name="DXY")
    dxy_final = _clip_series_through(dxy_final, end_date=last_completed_day)
    rows_written["dxy"] = _write_series_csv(dxy_path, dxy_final)
    dataset_sources["dxy"] = dxy_source
    _emit("dxy", "done", dxy_source, rows_written["dxy"], str(dxy_path.resolve()), "")

    _emit("btc_usd", "running")
    btc_path = root / "btc.csv"
    btc_existing = _read_series_csv(btc_path, name="BTC_Close")
    btc_existing = _clip_series_through(btc_existing, end_date=last_completed_day)
    btc_fetch_start = _fetch_start_from_existing(btc_existing, default_start=start_date)
    btc, btc_source = load_btc_close(start=btc_fetch_start)
    btc_final = _overlay_series(btc.rename("BTC_Close"), btc_existing)
    btc_final = _ensure_anchor_series(btc_final, start_date=start_date, name="BTC_Close")
    btc_final = _clip_series_through(btc_final, end_date=last_completed_day)
    rows_written["btc_usd"] = _write_series_csv(btc_path, btc_final)
    dataset_sources["btc_usd"] = btc_source
    _emit("btc_usd", "done", btc_source, rows_written["btc_usd"], str(btc_path.resolve()), "")

    fx_specs = [
        ("KRW/USD", "fx_krw_usd", 1300.0, 110),
        ("JPY/USD", "fx_jpy_usd", 80.0, 210),
        ("USD/EUR", "fx_usd_eur", 0.92, 310),
        ("USD/GBP", "fx_usd_gbp", 0.78, 410),
    ]
    for symbol, dataset, base, seed in fx_specs:
        _emit(dataset, "running")
        fx_path = root / f"{dataset}.csv"
        fx_existing = _read_series_csv(fx_path, name=symbol)
        fx_existing = _clip_series_through(fx_existing, end_date=last_completed_day)
        fx_fetch_start = _fetch_start_from_existing(fx_existing, default_start=start_date)
        fx, fx_source = load_fx_close(symbol, invert=False, start=fx_fetch_start, fallback_base=base, seed=seed)
        fx_final = _overlay_series(fx.rename(symbol), fx_existing)
        fx_final = _ensure_anchor_series(fx_final, start_date=start_date, name=symbol)
        fx_final = _clip_series_through(fx_final, end_date=last_completed_day)
        rows_written[dataset] = _write_series_csv(fx_path, fx_final)
        dataset_sources[dataset] = fx_source
        _emit(dataset, "done", fx_source, rows_written[dataset], str(fx_path.resolve()), "")

    for symbol, dataset, seed in [("US500", "index_us500", 11), ("HSI", "index_hsi", 22)]:
        _emit(dataset, "running")
        idx_path = root / f"{dataset}.csv"
        idx_existing = _read_series_csv(idx_path, name=symbol)
        idx_existing = _clip_series_through(idx_existing, end_date=last_completed_day)
        idx_fetch_start = _fetch_start_from_existing(idx_existing, default_start=start_date)
        idx, idx_source = load_index_series(symbol, start=idx_fetch_start, seed=seed)
        idx_final = _overlay_series(idx.rename(symbol), idx_existing)
        idx_final = _ensure_anchor_series(idx_final, start_date=start_date, name=symbol)
        idx_final = _clip_series_through(idx_final, end_date=last_completed_day)
        rows_written[dataset] = _write_series_csv(idx_path, idx_final)
        dataset_sources[dataset] = idx_source
        _emit(dataset, "done", idx_source, rows_written[dataset], str(idx_path.resolve()), "")

    sync_result = sync_financial_market_prices_from_csvs(data_dir=root)
    return RefreshFinancialMarketResult(
        data_dir=root,
        sqlite_path=sync_result.db_path,
        rows_written=rows_written,
        sqlite_rows_added=sync_result.rows_added,
        dataset_sources=dataset_sources,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh common financial market CSV files and sync them into financial_market_prices.sqlite.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="Target directory for CSV outputs")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE, help="Desired unified start date (YYYY-MM-DD)")
    parser.add_argument("--insecure-ssl", action="store_true", help="Disable TLS verification for temporary testing")
    parser.add_argument("--ca-bundle", default="", help="Custom CA bundle path")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        configure_ssl(insecure_ssl=bool(args.insecure_ssl), ca_bundle=str(args.ca_bundle).strip() or None)
        result = refresh_financial_market_data(
            data_dir=Path(args.data_dir),
            start_date=str(args.start_date).strip() or DEFAULT_START_DATE,
        )
    except Exception as exc:
        hint = security_hint(exc, output_dir=Path(args.data_dir))
        if hint:
            print(hint, file=sys.stderr)
        print(f"Refresh failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(
        f"Refreshed financial market CSVs in {result.data_dir} "
        f"(datasets={len(result.rows_written)}, sqlite_rows_added={result.sqlite_rows_added}, sqlite_path={result.sqlite_path})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
