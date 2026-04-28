from __future__ import annotations
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


DEFAULT_DATA_DIR = Path("data")
DEFAULT_DB_ROOT = DEFAULT_DATA_DIR / "financial_market_prices"
DEFAULT_SQLITE_NAME = "financial_market_prices.sqlite"

TREASURY_DATASET = "treasury_yields"

SINGLE_SERIES_DATASETS: dict[str, dict[str, str]] = {
    "dxy": {"filename": "dxy.csv", "series_id": "DXY"},
    "btc": {"filename": "btc.csv", "series_id": "BTC_Close"},
    "fx_krw_usd": {"filename": "fx_krw_usd.csv", "series_id": "KRW/USD"},
    "fx_jpy_usd": {"filename": "fx_jpy_usd.csv", "series_id": "JPY/USD"},
    "fx_usd_eur": {"filename": "fx_usd_eur.csv", "series_id": "USD/EUR"},
    "fx_usd_gbp": {"filename": "fx_usd_gbp.csv", "series_id": "USD/GBP"},
    "index_us500": {"filename": "index_us500.csv", "series_id": "US500"},
    "index_hsi": {"filename": "index_hsi.csv", "series_id": "HSI"},
}

ALL_DATASET_KEYS = [TREASURY_DATASET, *SINGLE_SERIES_DATASETS]


@dataclass(frozen=True)
class SyncResult:
    db_path: Path
    dataset_count: int
    rows_added: int
    rows_total: int


def _resolve_db_root(db_root: Path | str | None = None) -> Path:
    if db_root is None:
        env_root = str(os.getenv("KEUMJ_FINANCIAL_MARKET_DB_DIR", "")).strip()
        if env_root:
            return Path(env_root)
        return DEFAULT_DB_ROOT
    return Path(db_root)


def financial_market_prices_sqlite_path(db_root: Path | str | None = None) -> Path:
    explicit = str(os.getenv("KEUMJ_FINANCIAL_MARKET_DB_SQLITE_PATH", "")).strip()
    if explicit:
        return Path(explicit)
    return _resolve_db_root(db_root) / DEFAULT_SQLITE_NAME


def _sync_target_path(
    *,
    data_dir: Path,
    db_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> Path:
    if db_path is not None:
        return Path(db_path)
    explicit = str(os.getenv("KEUMJ_FINANCIAL_MARKET_DB_SQLITE_PATH", "")).strip()
    if explicit:
        return Path(explicit)
    if db_root is not None:
        return Path(db_root) / DEFAULT_SQLITE_NAME
    env_root = str(os.getenv("KEUMJ_FINANCIAL_MARKET_DB_DIR", "")).strip()
    if env_root:
        return Path(env_root) / DEFAULT_SQLITE_NAME
    return Path(data_dir) / "financial_market_prices" / DEFAULT_SQLITE_NAME


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS series_prices (
            dataset TEXT NOT NULL,
            series_id TEXT NOT NULL,
            date TEXT NOT NULL,
            value REAL NOT NULL,
            PRIMARY KEY (dataset, series_id, date)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_series_prices_dataset_date ON series_prices(dataset, date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_series_prices_series_date ON series_prices(series_id, date)")


def _normalize_single_series_csv(path: Path, *, series_id: str) -> pd.DataFrame:
    raw = pd.read_csv(path)
    if raw.empty:
        return pd.DataFrame(columns=["date", "series_id", "value"])

    cols = {str(c).strip().lower(): c for c in raw.columns}
    date_col = cols.get("date") or cols.get("datetime") or raw.columns[0]
    value_col = cols.get("close") or cols.get("price") or cols.get("value") or raw.columns[-1]

    out = raw[[date_col, value_col]].copy()
    out.columns = ["date", "value"]
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    out["series_id"] = str(series_id).strip()
    out = out.dropna(subset=["date", "value"])
    if out.empty:
        return pd.DataFrame(columns=["date", "series_id", "value"])
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    return out[["date", "series_id", "value"]].drop_duplicates(subset=["series_id", "date"], keep="last")


def _normalize_treasury_csv(path: Path) -> pd.DataFrame:
    raw = pd.read_csv(path)
    if raw.empty:
        return pd.DataFrame(columns=["date", "series_id", "value"])

    cols = {str(c).strip().lower(): c for c in raw.columns}
    date_col = cols.get("date") or cols.get("datetime") or raw.columns[0]
    frame = raw.copy()
    frame[date_col] = pd.to_datetime(frame[date_col], errors="coerce").dt.normalize()
    frame = frame.dropna(subset=[date_col]).set_index(date_col).sort_index()
    if frame.empty:
        return pd.DataFrame(columns=["date", "series_id", "value"])

    parts: list[pd.DataFrame] = []
    for col in frame.columns:
        series = pd.to_numeric(frame[col], errors="coerce").dropna()
        if series.empty:
            continue
        parts.append(
            pd.DataFrame(
                {
                    "date": series.index.strftime("%Y-%m-%d"),
                    "series_id": str(col).strip().upper(),
                    "value": series.astype(float).values,
                }
            )
        )
    if not parts:
        return pd.DataFrame(columns=["date", "series_id", "value"])
    out = pd.concat(parts, axis=0, ignore_index=True)
    return out.drop_duplicates(subset=["series_id", "date"], keep="last")


def _append_rows(
    conn: sqlite3.Connection,
    *,
    dataset: str,
    rows: pd.DataFrame,
    only_after: str | None = None,
) -> int:
    if rows.empty:
        return 0
    out = rows.copy()
    if only_after:
        out = out[out["date"] > str(only_after)]
    if out.empty:
        return 0

    values = [(dataset, str(r.series_id), str(r.date), float(r.value)) for r in out.itertuples(index=False)]
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO series_prices(dataset, series_id, date, value)
        VALUES (?, ?, ?, ?)
        """,
        values,
    )
    return int(conn.total_changes - before)


def sync_financial_market_prices_from_csvs(
    *,
    data_dir: Path | str = DEFAULT_DATA_DIR,
    db_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> SyncResult:
    data_root = Path(data_dir)
    target = _sync_target_path(data_dir=data_root, db_root=db_root, db_path=db_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    dataset_count = 0
    rows_added = 0
    with sqlite3.connect(target) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _ensure_schema(conn)

        treasury_path = data_root / "treasury_yields.csv"
        if treasury_path.exists() and treasury_path.is_file():
            treasury_rows = _normalize_treasury_csv(treasury_path)
            rows_added += _append_rows(
                conn,
                dataset=TREASURY_DATASET,
                rows=treasury_rows,
                only_after=None,
            )
            dataset_count += 1

        for dataset, config in SINGLE_SERIES_DATASETS.items():
            csv_path = data_root / config["filename"]
            if not csv_path.exists() or not csv_path.is_file():
                continue
            series_rows = _normalize_single_series_csv(csv_path, series_id=config["series_id"])
            rows_added += _append_rows(
                conn,
                dataset=dataset,
                rows=series_rows,
                only_after=None,
            )
            dataset_count += 1

        conn.commit()
        total_row = conn.execute("SELECT COUNT(*) FROM series_prices").fetchone()
    return SyncResult(
        db_path=target,
        dataset_count=dataset_count,
        rows_added=rows_added,
        rows_total=int(total_row[0] if total_row else 0),
    )


def _query_series_prices(
    query: str,
    params: list[object],
    *,
    db_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> pd.DataFrame:
    target = Path(db_path) if db_path is not None else financial_market_prices_sqlite_path(db_root)
    if not target.exists() or not target.is_file():
        return pd.DataFrame()
    with sqlite3.connect(target) as conn:
        _ensure_schema(conn)
        return pd.read_sql_query(query, conn, params=params)


def load_financial_market_series(
    dataset: str,
    *,
    series_id: str,
    start_date: str | pd.Timestamp,
    end_date: str | pd.Timestamp | None = None,
    db_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> tuple[pd.Series | None, str | None]:
    start_text = pd.Timestamp(start_date).normalize().strftime("%Y-%m-%d")
    params: list[object] = [str(dataset), str(series_id), start_text]
    query = """
        SELECT date, value
        FROM series_prices
        WHERE dataset = ? AND series_id = ? AND date >= ?
    """
    if end_date is not None:
        end_text = pd.Timestamp(end_date).normalize().strftime("%Y-%m-%d")
        query += " AND date <= ?"
        params.append(end_text)
    query += " ORDER BY date"
    raw = _query_series_prices(query, params, db_root=db_root, db_path=db_path)
    if raw.empty:
        return None, None
    raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
    raw["value"] = pd.to_numeric(raw["value"], errors="coerce")
    raw = raw.dropna(subset=["date", "value"])
    if raw.empty:
        return None, None
    out = pd.Series(raw["value"].values, index=raw["date"].dt.normalize(), name=str(series_id))
    out = out[~out.index.duplicated(keep="last")].sort_index()
    if out.empty:
        return None, None
    target = Path(db_path) if db_path is not None else financial_market_prices_sqlite_path(db_root)
    return out, f"sqlite:{target.as_posix()}"


def load_financial_market_frame(
    dataset: str,
    *,
    series_ids: list[str],
    start_date: str | pd.Timestamp,
    end_date: str | pd.Timestamp | None = None,
    db_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> tuple[pd.DataFrame | None, str | None]:
    normalized = [str(s).strip().upper() for s in series_ids if str(s).strip()]
    if not normalized:
        return None, None
    placeholders = ",".join(["?"] * len(normalized))
    start_text = pd.Timestamp(start_date).normalize().strftime("%Y-%m-%d")
    params: list[object] = [str(dataset), *normalized, start_text]
    query = f"""
        SELECT date, series_id, value
        FROM series_prices
        WHERE dataset = ? AND series_id IN ({placeholders}) AND date >= ?
    """
    if end_date is not None:
        end_text = pd.Timestamp(end_date).normalize().strftime("%Y-%m-%d")
        query += " AND date <= ?"
        params.append(end_text)
    query += " ORDER BY date, series_id"
    raw = _query_series_prices(query, params, db_root=db_root, db_path=db_path)
    if raw.empty:
        return None, None
    raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
    raw["value"] = pd.to_numeric(raw["value"], errors="coerce")
    raw["series_id"] = raw["series_id"].astype(str).str.upper()
    raw = raw.dropna(subset=["date", "series_id", "value"])
    if raw.empty:
        return None, None
    out = raw.pivot_table(index="date", columns="series_id", values="value", aggfunc="last").sort_index()
    keep = [s for s in normalized if s in out.columns]
    if not keep:
        return None, None
    out = out[keep].dropna(how="all")
    if out.empty:
        return None, None
    target = Path(db_path) if db_path is not None else financial_market_prices_sqlite_path(db_root)
    return out, f"sqlite:{target.as_posix()}"
