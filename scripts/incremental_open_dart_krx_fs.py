from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.load_krx_fs_to_shared_db import (
    DEFAULT_DB_PATH,
    DEFAULT_FS_ROOT,
    DEFAULT_TABLE,
    KRXFsLoadResult,
    load_krx_fs_csv_files_to_shared_db,
)
from scripts.sync_krx_fs_rows_to_fundamentals_quarterly import (
    KRXFsFundamentalsSyncResult,
    sync_krx_fs_rows_to_fundamentals_quarterly,
)


DEFAULT_COMPONENTS_CSV = Path("data/krx_components_full.csv")
DEFAULT_REPORT_TYPES: tuple[tuple[str, str], ...] = (
    ("q1", "11013"),
    ("half_year", "11012"),
    ("q3", "11014"),
    ("annual", "11011"),
)
DEFAULT_OVERLAP_YEARS = 1
MIN_VALID_CSV_BYTES = 200
DEFAULT_PROGRESS_CHUNK_SIZE = 200
DEFAULT_PROGRESS_HEARTBEAT_REQUESTS = 25
DEFAULT_GUI_CONFIG_PATH = Path("data/krx_web_gui_config.json")


@dataclass(frozen=True)
class OpenDartFsIncrementalResult:
    symbols_seen: int
    requests_planned: int
    requests_attempted: int
    saved_files: int
    skipped_existing: int
    empty_reports: int
    failed_reports: int
    locally_loaded_files: int
    load_result: KRXFsLoadResult | None
    fundamentals_result: KRXFsFundamentalsSyncResult | None


def _log(message: str) -> None:
    print(f"[incremental-open-dart-krx-fs] {message}", flush=True)


def _normalize_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    return text.zfill(6) if text.isdigit() else text


def _normalize_amount(value: object) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text.lower() in {"nan", "none", "null", "n/a", "-"}:
        return None
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    try:
        return float(text)
    except ValueError:
        return None


def _read_registry_api_key() -> str:
    try:
        import winreg
    except Exception:
        return ""

    candidates: list[tuple[object, str]] = []
    env_path = str(os.getenv("DART_API_KEY_REGISTRY_PATH", "")).strip()
    if env_path:
        hive_name, _, subkey = env_path.partition("\\")
        hive = {
            "HKCU": winreg.HKEY_CURRENT_USER,
            "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
            "HKLM": winreg.HKEY_LOCAL_MACHINE,
            "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
        }.get(hive_name.upper())
        if hive is not None and subkey:
            candidates.append((hive, subkey))

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for subkey in (
            r"Environment",
            r"Software\Keumj\KRX",
            r"Software\Keumj",
            r"Software\OpenDART",
            r"Software\OpenDartReader",
            r"Software\DART",
            r"Software\WOW6432Node\Keumj\KRX",
            r"Software\WOW6432Node\OpenDART",
            r"Software\WOW6432Node\DART",
        ):
            candidates.append((hive, subkey))

    value_names = (
        "KEUMJ_DART_API_KEY",
        "DART_API_KEY",
        "OPEN_DART_API_KEY",
        "OPEN_DART_KEY",
        "OpenDartApiKey",
        "dart_api_key",
        "api_key",
        "API_KEY",
        "crtfc_key",
    )
    for hive, subkey in candidates:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                for value_name in value_names:
                    try:
                        value, _value_type = winreg.QueryValueEx(key, value_name)
                    except OSError:
                        continue
                    text = str(value or "").strip()
                    if text:
                        return text
        except OSError:
            continue
    return ""


def _load_api_key(explicit_api_key: str | None = None) -> str:
    candidate = str(explicit_api_key or "").strip()
    if candidate:
        return candidate
    for env_name in ("KEUMJ_DART_API_KEY", "DART_API_KEY", "OPEN_DART_API_KEY"):
        value = str(os.getenv(env_name, "")).strip()
        if value:
            return value
    registry_value = _read_registry_api_key()
    if registry_value:
        return registry_value
    for path in (DEFAULT_GUI_CONFIG_PATH, PROJECT_ROOT / DEFAULT_GUI_CONFIG_PATH):
        try:
            if not path.exists() or not path.is_file():
                continue
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if isinstance(payload, dict):
            config_value = str(payload.get("dart_api_key", "")).strip()
            if config_value:
                return config_value
    raise RuntimeError("DART API key is required. Pass --api-key, set KEUMJ_DART_API_KEY, save it in the GUI config, or save it in the registry.")


def _load_open_dart_reader() -> Any:
    try:
        import OpenDartReader  # type: ignore
    except ImportError as exc:
        raise RuntimeError("OpenDartReader is required for this script. Install it in the active environment.") from exc
    return OpenDartReader


def _is_preferred_stock(symbol: object, name: object) -> bool:
    symbol_text = _normalize_symbol(symbol)
    name_text = str(name or "").strip()
    if symbol_text.endswith("0"):
        return False
    return any(token in name_text for token in ("우", "우선", "전환", "종류"))


def _load_components(path: Path, *, db_path: Path, include_preferred: bool) -> pd.DataFrame:
    frame = pd.DataFrame()
    try:
        import FinanceDataReader as fdr

        listing = fdr.StockListing("KRX")
        cols = {str(col).strip().lower(): col for col in listing.columns}
        code_col = cols.get("code") or cols.get("symbol")
        name_col = cols.get("name")
        market_col = cols.get("market")
        if code_col is not None and name_col is not None:
            frame = pd.DataFrame(
                {
                    "symbol": listing[code_col].map(_normalize_symbol),
                    "name": listing[name_col].astype(str).str.strip(),
                    "market": listing[market_col].astype(str).str.strip() if market_col is not None else "UNKNOWN",
                }
            )
    except Exception:
        frame = pd.DataFrame()

    if frame.empty and db_path.exists():
        with sqlite3.connect(db_path) as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='securities'").fetchone()
            if exists is not None:
                frame = pd.read_sql_query(
                    """
                    SELECT symbol, COALESCE(name_kr, name_en, symbol) AS name, market
                    FROM securities
                    WHERE COALESCE(is_active, 1) = 1
                    ORDER BY market, symbol
                    """,
                    conn,
                )

    if frame.empty:
        raw = pd.read_csv(path, dtype={"Symbol": str, "Code": str, "symbol": str, "code": str})
        cols = {str(col).strip().lower(): col for col in raw.columns}
        symbol_col = cols.get("symbol") or cols.get("code")
        name_col = cols.get("namekr") or cols.get("name") or cols.get("company")
        market_col = cols.get("market")
        if symbol_col is None or name_col is None:
            raise RuntimeError(f"Components CSV must include Symbol and NameKR/Name columns: {path}")
        frame = pd.DataFrame(
            {
                "symbol": raw[symbol_col].map(_normalize_symbol),
                "name": raw[name_col].astype(str).str.strip(),
                "market": raw[market_col].astype(str).str.strip() if market_col is not None else "UNKNOWN",
            }
        )

    frame = frame.dropna(subset=["symbol", "name"]).drop_duplicates(subset=["symbol"], keep="first")
    if not include_preferred:
        frame = frame[~frame.apply(lambda row: _is_preferred_stock(row["symbol"], row["name"]), axis=1)].copy()
    return frame.sort_values(["market", "symbol"]).reset_index(drop=True)


def _selected_report_types(report_codes_text: str) -> tuple[tuple[str, str], ...]:
    selected = [item.strip() for item in str(report_codes_text or "").split(",") if item.strip()]
    if not selected:
        return DEFAULT_REPORT_TYPES
    label_by_code = {code: label for label, code in DEFAULT_REPORT_TYPES}
    return tuple((label_by_code.get(code, code), code) for code in selected)


def _source_file_for_path(path: Path, fs_root: Path) -> str:
    try:
        return path.relative_to(fs_root.parent).as_posix()
    except ValueError:
        return path.as_posix()


def _existing_report_file(fs_root: Path, symbol: str, year: int, report_label: str) -> Path | None:
    company_dir = fs_root / symbol
    if not company_dir.exists():
        return None
    for pattern in (f"*_{int(year)}_{report_label}.csv", f"*_{int(year)}*{report_label}.csv"):
        for path in sorted(company_dir.glob(pattern)):
            if path.is_file() and path.stat().st_size > MIN_VALID_CSV_BYTES:
                return path
    return None


def _existing_report_in_sqlite(db_path: Path, table: str, symbol: str, year: int, report_label: str) -> bool:
    if not db_path.exists():
        return False
    with sqlite3.connect(db_path) as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if exists is None:
            return False
        row = conn.execute(
            f"""
            SELECT 1
            FROM "{table}"
            WHERE symbol = ?
              AND report_year = ?
              AND report_name LIKE ?
            LIMIT 1
            """,
            (_normalize_symbol(symbol), int(year), f"%{report_label}%"),
        ).fetchone()
    return row is not None


def _existing_source_files(db_path: Path, table: str) -> set[str]:
    if not db_path.exists():
        return set()
    with sqlite3.connect(db_path) as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if exists is None:
            return set()
        return {str(row[0]) for row in conn.execute(f'SELECT DISTINCT source_file FROM "{table}"')}


def _local_csvs_missing_from_db(fs_root: Path, db_path: Path, table: str) -> list[Path]:
    existing = _existing_source_files(db_path, table)
    return [
        path
        for path in sorted(fs_root.rglob("*.csv"))
        if path.is_file()
        and path.stat().st_size > MIN_VALID_CSV_BYTES
        and _source_file_for_path(path, fs_root) not in existing
    ]


def _latest_known_report_year(fs_root: Path, db_path: Path, table: str) -> int | None:
    candidates: list[int] = []
    for path in fs_root.rglob("*.csv") if fs_root.exists() else []:
        for part in path.stem.replace("-", "_").split("_"):
            if part.isdigit() and len(part) == 4:
                candidates.append(int(part))
    if db_path.exists():
        with sqlite3.connect(db_path) as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if exists is not None:
                row = conn.execute(f'SELECT MAX(report_year) FROM "{table}"').fetchone()
                if row and row[0] is not None:
                    candidates.append(int(row[0]))
    return max(candidates) if candidates else None


def _save_report_csv(
    *,
    dart: Any,
    fs_root: Path,
    symbol: str,
    year: int,
    report_label: str,
    report_code: str,
) -> Path | None:
    report = dart.finstate(symbol, year, report_code)
    if isinstance(report, dict) or report is None or not isinstance(report, pd.DataFrame) or report.empty:
        return None

    required_cols = ["fs_nm", "account_nm", "thstrm_dt", "thstrm_amount", "sj_nm"]
    if not all(col in report.columns for col in required_cols):
        return None

    submission_date = f"{int(year)}0101"
    if "rcept_no" in report.columns and len(str(report["rcept_no"].iloc[0])) >= 8:
        submission_date = str(report["rcept_no"].iloc[0])[:8]

    output_cols = [
        col
        for col in ["fs_nm", "account_id", "account_nm", "thstrm_dt", "thstrm_amount", "thstrm_add_amount", "sj_nm"]
        if col in report.columns
    ]
    out = report[output_cols].rename(
        columns={
            "fs_nm": "consolidation",
            "account_id": "account_id",
            "account_nm": "account_name",
            "thstrm_dt": "period_label",
            "thstrm_amount": "amount",
            "thstrm_add_amount": "cumulative_amount",
            "sj_nm": "statement_name",
        }
    )
    out["amount"] = out["amount"].map(_normalize_amount)

    company_dir = fs_root / symbol
    company_dir.mkdir(parents=True, exist_ok=True)
    path = company_dir / f"{submission_date}_{int(year)}_{report_label}.csv"
    out.to_csv(path, index=False, encoding="utf-8")
    return path


def refresh_open_dart_krx_fs_incremental(
    *,
    components_csv: Path = DEFAULT_COMPONENTS_CSV,
    fs_root: Path = DEFAULT_FS_ROOT,
    db_path: Path = DEFAULT_DB_PATH,
    table: str = DEFAULT_TABLE,
    api_key: str | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
    overlap_years: int = DEFAULT_OVERLAP_YEARS,
    report_types: tuple[tuple[str, str], ...] = DEFAULT_REPORT_TYPES,
    pause_seconds: float = 0.5,
    max_retries: int = 3,
    max_symbols: int | None = None,
    log_chunk_size: int = DEFAULT_PROGRESS_CHUNK_SIZE,
    log_heartbeat_requests: int = DEFAULT_PROGRESS_HEARTBEAT_REQUESTS,
    include_preferred: bool = False,
    force: bool = False,
    sync_local_missing: bool = True,
    sync_fundamentals: bool = True,
    dry_run: bool = False,
) -> OpenDartFsIncrementalResult:
    fs_root.mkdir(parents=True, exist_ok=True)
    symbols = _load_components(components_csv, db_path=db_path, include_preferred=include_preferred)
    if max_symbols is not None and int(max_symbols) > 0:
        symbols = symbols.head(int(max_symbols)).copy()

    current_year = pd.Timestamp.today().year
    latest_known_year = _latest_known_report_year(fs_root, db_path, table)
    year_start = int(start_year or max(2024, (latest_known_year or current_year) - max(int(overlap_years), 0)))
    year_end = int(end_year or current_year)
    if year_start > year_end:
        raise RuntimeError(f"Invalid year range: start={year_start}, end={year_end}")

    locally_missing = [] if not sync_local_missing else _local_csvs_missing_from_db(fs_root, db_path, table)
    saved_paths: list[Path] = []
    skipped_existing = 0
    empty_reports = 0
    failed_reports = 0

    plan: list[tuple[str, int, str, str]] = []
    for record in symbols.itertuples(index=False):
        symbol = _normalize_symbol(getattr(record, "symbol"))
        for year in range(year_start, year_end + 1):
            for report_label, report_code in report_types:
                existing = _existing_report_file(fs_root, symbol, year, report_label)
                existing_in_db = _existing_report_in_sqlite(db_path, table, symbol, year, report_label)
                if (existing is not None or existing_in_db) and not force:
                    skipped_existing += 1
                    continue
                plan.append((symbol, year, report_label, report_code))

    if dry_run:
        _log(
            f"dry_run symbols={len(symbols)} years={year_start}-{year_end} "
            f"planned_requests={len(plan)} skipped_existing={skipped_existing} "
            f"local_files_missing_db={len(locally_missing)}"
        )
        return OpenDartFsIncrementalResult(
            symbols_seen=len(symbols),
            requests_planned=len(plan),
            requests_attempted=0,
            saved_files=0,
            skipped_existing=skipped_existing,
            empty_reports=0,
            failed_reports=0,
            locally_loaded_files=0,
            load_result=None,
            fundamentals_result=None,
        )

    OpenDartReader = _load_open_dart_reader()
    dart = OpenDartReader(_load_api_key(api_key))
    requests_attempted = 0
    total_requests = len(plan)
    chunk_size = max(1, int(log_chunk_size))
    heartbeat_requests = max(1, int(log_heartbeat_requests))
    _log(
        f"planned_requests={total_requests} skipped_existing={skipped_existing} "
        f"local_files_missing_db={len(locally_missing)} log_chunk_size={chunk_size} "
        f"heartbeat_requests={heartbeat_requests}"
    )
    chunk_attempted_start = 0
    chunk_saved_start = 0
    chunk_empty_start = 0
    chunk_failed_start = 0
    for index, (symbol, year, report_label, report_code) in enumerate(plan, start=1):
        if (index - 1) % chunk_size == 0:
            chunk_attempted_start = requests_attempted
            chunk_saved_start = len(saved_paths)
            chunk_empty_start = empty_reports
            chunk_failed_start = failed_reports
            _log(f"chunk_started {index}-{min(index + chunk_size - 1, total_requests)}/{total_requests}")
        saved_path: Path | None = None
        for attempt in range(1, max(1, int(max_retries)) + 1):
            try:
                requests_attempted += 1
                saved_path = _save_report_csv(
                    dart=dart,
                    fs_root=fs_root,
                    symbol=symbol,
                    year=year,
                    report_label=report_label,
                    report_code=report_code,
                )
                break
            except Exception as exc:
                if attempt >= max(1, int(max_retries)):
                    failed_reports += 1
                    _log(f"failed symbol={symbol} year={year} report={report_label} error={str(exc)[:180]}")
                else:
                    time.sleep(max(float(pause_seconds), 0.0) * 2)
        if saved_path is None:
            empty_reports += 1
        else:
            saved_paths.append(saved_path)
        if pause_seconds > 0:
            time.sleep(float(pause_seconds))
        if index % heartbeat_requests == 0 and index % chunk_size != 0 and index != total_requests:
            _log(
                f"progress {index}/{total_requests} "
                f"attempted_total={requests_attempted} saved_files_total={len(saved_paths)} "
                f"empty_total={empty_reports} failed_total={failed_reports} "
                f"last_symbol={symbol} last_report={year}:{report_label}"
            )
        if index % chunk_size == 0 or index == total_requests:
            _log(
                f"chunk_done {index - ((index - 1) % chunk_size)}-{index}/{total_requests} "
                f"attempted_chunk={requests_attempted - chunk_attempted_start} "
                f"saved_files_chunk={len(saved_paths) - chunk_saved_start} "
                f"empty_chunk={empty_reports - chunk_empty_start} "
                f"failed_chunk={failed_reports - chunk_failed_start} "
                f"attempted_total={requests_attempted} saved_files_total={len(saved_paths)}"
            )

    paths_to_load = sorted({*locally_missing, *saved_paths})
    load_result = None
    if paths_to_load:
        load_result = load_krx_fs_csv_files_to_shared_db(
            fs_root=fs_root,
            db_path=db_path,
            table=table,
            csv_paths=paths_to_load,
        )

    fundamentals_result = None
    if sync_fundamentals and paths_to_load:
        fundamentals_result = sync_krx_fs_rows_to_fundamentals_quarterly(
            db_path=db_path,
            raw_table=table,
        )

    return OpenDartFsIncrementalResult(
        symbols_seen=len(symbols),
        requests_planned=len(plan),
        requests_attempted=requests_attempted,
        saved_files=len(saved_paths),
        skipped_existing=skipped_existing,
        empty_reports=empty_reports,
        failed_reports=failed_reports,
        locally_loaded_files=len(locally_missing),
        load_result=load_result,
        fundamentals_result=fundamentals_result,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Incrementally fetch KRX financial statement CSVs with OpenDartReader and load new rows into shared SQLite."
    )
    parser.add_argument("--components-csv", default=str(DEFAULT_COMPONENTS_CSV), help="KRX components CSV path.")
    parser.add_argument("--fs-root", default=str(DEFAULT_FS_ROOT), help="Local KRX FS CSV root.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Shared SQLite DB path.")
    parser.add_argument("--table", default=DEFAULT_TABLE, help="Raw KRX FS target table.")
    parser.add_argument("--api-key", default="", help="DART API key. Falls back to environment variables and registry.")
    parser.add_argument("--start-year", type=int, default=0, help="First business year to fetch. Default: latest known year - overlap.")
    parser.add_argument("--end-year", type=int, default=0, help="Last business year to fetch. Default: current year.")
    parser.add_argument("--overlap-years", type=int, default=DEFAULT_OVERLAP_YEARS, help="Years to overlap when start-year is omitted.")
    parser.add_argument("--report-codes", default="", help="Optional comma-separated DART report codes.")
    parser.add_argument("--pause-seconds", type=float, default=0.5, help="Delay between DART requests.")
    parser.add_argument("--max-retries", type=int, default=3, help="Retries per report request.")
    parser.add_argument("--max-symbols", type=int, default=0, help="Limit symbols for a trial run.")
    parser.add_argument("--log-chunk-size", type=int, default=DEFAULT_PROGRESS_CHUNK_SIZE, help="Progress log interval in planned requests.")
    parser.add_argument("--log-heartbeat-requests", type=int, default=DEFAULT_PROGRESS_HEARTBEAT_REQUESTS, help="Short progress heartbeat interval in planned requests.")
    parser.add_argument("--include-preferred", action="store_true", help="Include preferred/class shares.")
    parser.add_argument("--force", action="store_true", help="Fetch even when a matching local CSV or DB report already exists.")
    parser.add_argument("--skip-local-db-sync", action="store_true", help="Do not load local CSVs that are absent from SQLite.")
    parser.add_argument("--skip-fundamentals-sync", action="store_true", help="Do not refresh fundamentals_quarterly from krx_fs_rows.")
    parser.add_argument("--dry-run", action="store_true", help="Plan work without calling DART or writing files.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    result = refresh_open_dart_krx_fs_incremental(
        components_csv=Path(args.components_csv),
        fs_root=Path(args.fs_root),
        db_path=Path(args.db_path),
        table=str(args.table).strip() or DEFAULT_TABLE,
        api_key=str(args.api_key).strip() or None,
        start_year=int(args.start_year) or None,
        end_year=int(args.end_year) or None,
        overlap_years=int(args.overlap_years),
        report_types=_selected_report_types(str(args.report_codes)),
        pause_seconds=float(args.pause_seconds),
        max_retries=int(args.max_retries),
        max_symbols=int(args.max_symbols) or None,
        log_chunk_size=int(args.log_chunk_size),
        log_heartbeat_requests=int(args.log_heartbeat_requests),
        include_preferred=bool(args.include_preferred),
        force=bool(args.force),
        sync_local_missing=not bool(args.skip_local_db_sync),
        sync_fundamentals=not bool(args.skip_fundamentals_sync),
        dry_run=bool(args.dry_run),
    )
    load_summary = "load_rows=0"
    if result.load_result is not None:
        load_summary = (
            f"load_files={result.load_result.file_count} "
            f"load_source_rows={result.load_result.source_rows} "
            f"load_changed_rows={result.load_result.changed_rows}"
        )
    fundamentals_summary = "fundamentals_changed_rows=0"
    if result.fundamentals_result is not None:
        fundamentals_summary = (
            f"fundamentals_source_reports={result.fundamentals_result.source_reports} "
            f"fundamentals_rows={result.fundamentals_result.transformed_rows} "
            f"fundamentals_changed_rows={result.fundamentals_result.changed_rows}"
        )
    print(
        "incremental_open_dart_krx_fs_complete",
        f"symbols={result.symbols_seen}",
        f"planned={result.requests_planned}",
        f"attempted={result.requests_attempted}",
        f"saved_files={result.saved_files}",
        f"skipped_existing={result.skipped_existing}",
        f"empty={result.empty_reports}",
        f"failed={result.failed_reports}",
        f"local_missing_loaded={result.locally_loaded_files}",
        load_summary,
        fundamentals_summary,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
