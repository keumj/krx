from __future__ import annotations

import argparse
import io
import json
import os
import re
import signal
import sqlite3
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pandas as pd
import requests

from pipeline_common.security import configure_ssl

from .db import (
    init_krx_project_db,
    upsert_krx_quarterly_fundamentals,
    upsert_krx_securities,
)


CORP_CODES_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
FINANCIALS_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
DEFAULT_COMPONENTS_CSV = Path("data/krx_components_full.csv")
DEFAULT_REPORT_CODES = ("11013", "11012", "11014", "11011")
DEFAULT_START_YEAR = 2019
DEFAULT_PROGRESS_BATCH_SIZE = 200
DEFAULT_PROGRESS_HEARTBEAT_SYMBOLS = 10
DEFAULT_INCREMENTAL_OVERLAP_YEARS = 1
DEFAULT_GUI_CONFIG_PATH = Path("data/krx_web_gui_config.json")
_CANCEL_REQUESTED = False
_DART_REQUEST_FAILURES = 0

ACCOUNT_ID_CANDIDATES = {
    "revenue": (
        "ifrs-full_Revenue",
        "ifrs-full_GrossProfit",
        "ifrs-full_RevenueFromContractsWithCustomers",
        "dart_Revenue",
    ),
    "operating_income": (
        "dart_OperatingIncomeLoss",
        "ifrs-full_ProfitLossFromOperatingActivities",
    ),
    "net_income": (
        "ifrs-full_ProfitLoss",
        "dart_NetIncomeLoss",
    ),
    "total_assets": (
        "ifrs-full_Assets",
        "dart_AssetsTotal",
    ),
    "total_liabilities": (
        "ifrs-full_Liabilities",
        "dart_LiabilitiesTotal",
    ),
    "stockholders_equity": (
        "ifrs-full_Equity",
        "dart_EquityTotal",
    ),
    "operating_cash_flow": (
        "ifrs-full_CashFlowsFromUsedInOperatingActivities",
        "dart_CashFlowsFromUsedInOperatingActivities",
    ),
}

ACCOUNT_NAME_CANDIDATES = {
    "revenue": ("매출액", "영업수익", "수익(매출액)", "수익"),
    "operating_income": ("영업이익", "영업손익"),
    "net_income": ("당기순이익", "분기순이익", "반기순이익", "연결당기순이익"),
    "total_assets": ("자산총계",),
    "total_liabilities": ("부채총계",),
    "stockholders_equity": ("자본총계",),
    "operating_cash_flow": ("영업활동으로 인한 현금흐름", "영업활동현금흐름"),
}
FLOW_METRICS = ("revenue", "operating_income", "net_income", "operating_cash_flow", "free_cash_flow", "capex")


@dataclass(frozen=True)
class KRXDartRefreshResult:
    symbol_count: int
    corp_code_updates: int
    fundamentals_rows: int
    sqlite_path: Path


def _log(message: str) -> None:
    print(f"[refresh-krx-dart] {message}", flush=True)


def _batch_progress_log(
    *,
    batch_start_index: int,
    batch_end_index: int,
    total_symbols: int,
    rows_found: int | None = None,
    skipped_symbols: int | None = None,
    note: str | None = None,
) -> None:
    parts = [f"{batch_start_index}-{batch_end_index}/{total_symbols}"]
    if note:
        parts.append(str(note))
    if rows_found is not None:
        parts.append(f"fundamental_rows_batch={int(rows_found)}")
    if skipped_symbols is not None:
        parts.append(f"skipped_symbols_batch={int(skipped_symbols)}")
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


def _normalize_text(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    return text


def _normalize_number(value: object) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text.lower() in {"nan", "none", "null", "n/a", "-"}:
        return None
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    try:
        return float(text)
    except Exception:
        return None


def _extract_date_text(value: object) -> str | None:
    text = _normalize_text(value)
    if text is None:
        return None
    match = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if match:
        year, month, day = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    try:
        return pd.Timestamp(text).normalize().strftime("%Y-%m-%d")
    except Exception:
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


def _read_gui_config_api_key() -> str:
    for path in (
        DEFAULT_GUI_CONFIG_PATH,
        Path(__file__).resolve().parents[1] / DEFAULT_GUI_CONFIG_PATH,
    ):
        try:
            if not path.exists() or not path.is_file():
                continue
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if isinstance(payload, dict):
            text = str(payload.get("dart_api_key", "")).strip()
            if text:
                return text
    return ""


def _load_api_key(explicit_api_key: str | None = None) -> str:
    candidate = str(explicit_api_key or "").strip()
    if candidate:
        return candidate
    for env_name in ("KEUMJ_DART_API_KEY", "DART_API_KEY", "OPEN_DART_API_KEY"):
        env_value = str(os.getenv(env_name, "")).strip()
        if env_value:
            return env_value
    registry_value = _read_registry_api_key()
    if registry_value:
        return registry_value
    config_value = _read_gui_config_api_key()
    if config_value:
        return config_value
    raise RuntimeError("DART API key is required. Set KEUMJ_DART_API_KEY, pass --api-key, save it in the GUI config, or save it in the registry.")


def _load_components(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if frame.empty:
        raise RuntimeError(f"KRX components CSV is empty: {path}")
    cols = {str(col).strip().lower(): col for col in frame.columns}
    symbol_col = cols.get("symbol")
    if symbol_col is None:
        raise RuntimeError(f"KRX components CSV has no Symbol column: {path}")
    out = frame.copy()
    out["Symbol"] = out[symbol_col].map(_normalize_symbol)
    return out


def _component_symbol_set(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        frame = _load_components(path)
    except Exception:
        return set()
    return {str(symbol) for symbol in frame["Symbol"].dropna().map(_normalize_symbol) if str(symbol).strip()}


def _latest_fiscal_years(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT symbol, MAX(substr(fiscal_date, 1, 4))
            FROM fundamentals_quarterly
            WHERE fiscal_date IS NOT NULL
              AND length(fiscal_date) >= 4
            GROUP BY symbol
            """
        ).fetchall()
    latest: dict[str, int] = {}
    for symbol, fiscal_year in rows:
        try:
            latest[_normalize_symbol(symbol)] = int(fiscal_year)
        except Exception:
            continue
    return latest


def _incremental_years_for_symbol(
    *,
    all_years: list[int],
    latest_year: int | None,
    overlap_years: int,
) -> list[int]:
    if latest_year is None:
        return all_years
    start_year = int(latest_year) - max(int(overlap_years), 0)
    return [year for year in all_years if int(year) >= start_year]


def _request_json(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        _raise_if_cancelled()
        try:
            response = session.get(url, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError(f"Unexpected DART response payload type: {type(payload)!r}")
            return payload
        except (requests.RequestException, RuntimeError) as exc:
            last_exc = exc
            if attempt >= 3:
                break
            _interruptible_sleep(0.8 * attempt)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("DART request failed without an exception")


def download_dart_corp_codes(session: requests.Session, api_key: str) -> pd.DataFrame:
    _log("corpCode download started")
    response = session.get(CORP_CODES_URL, params={"crtfc_key": api_key}, timeout=45)
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        if not names:
            raise RuntimeError("DART corpCode.zip response was empty")
        with archive.open(names[0]) as handle:
            xml_bytes = handle.read()

    root = ElementTree.fromstring(xml_bytes)
    rows: list[dict[str, object]] = []
    for node in root.findall(".//list"):
        symbol = _normalize_symbol(node.findtext("stock_code"))
        if not symbol:
            continue
        rows.append(
            {
                "symbol": symbol,
                "market": "UNKNOWN",
                "name_kr": _normalize_text(node.findtext("corp_name")),
                "corp_code": _normalize_text(node.findtext("corp_code")),
                "is_active": 1,
                "reference_source": "dart:corpCode",
            }
        )
    frame = pd.DataFrame(rows)
    _log(f"corpCode download done rows={len(frame.index)}")
    return frame


def sync_krx_corp_codes(
    *,
    session: requests.Session,
    api_key: str,
    db_path: Path,
    components_csv: Path | None = None,
) -> int:
    _log("corpCode sync started")
    if components_csv is not None and components_csv.exists():
        components = _load_components(components_csv)
        upsert_krx_securities(
            components.rename(
                columns={
                    "Symbol": "symbol",
                    "Market": "market",
                    "NameKR": "name_kr",
                    "NameEN": "name_en",
                    "Sector": "sector",
                    "Industry": "industry",
                    "ListingDate": "listing_date",
                    "ReferenceSource": "reference_source",
                }
            ),
            db_path=db_path,
        )
    try:
        corp_codes = download_dart_corp_codes(session, api_key)
    except requests.RequestException as exc:
        with sqlite3.connect(db_path) as conn:
            existing_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM securities
                    WHERE corp_code IS NOT NULL
                      AND TRIM(corp_code) <> ''
                    """
                ).fetchone()[0]
                or 0
            )
        if existing_count:
            _log(f"DART corpCode download failed; using existing corp_code rows={existing_count}: {exc}")
            return 0
        raise
    if corp_codes.empty:
        _log("corpCode sync done updates=0")
        return 0
    updates = upsert_krx_securities(corp_codes, db_path=db_path)
    _log(f"corpCode sync done updates={updates}")
    return updates


def _rcept_no_to_date_text(value: object) -> str | None:
    text = _normalize_text(value)
    if text is None or len(text) < 8:
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) < 8:
        return None
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def _pick_account_value(rows: list[dict[str, Any]], metric: str, *, amount_column: str = "thstrm_amount") -> float | None:
    id_candidates = ACCOUNT_ID_CANDIDATES.get(metric, ())
    name_candidates = ACCOUNT_NAME_CANDIDATES.get(metric, ())
    for account_id in id_candidates:
        for row in rows:
            if str(row.get("account_id") or "").strip() == account_id:
                value = _normalize_number(row.get(amount_column))
                if value is not None:
                    return value
    for account_name in name_candidates:
        for row in rows:
            if str(row.get("account_nm") or "").strip() == account_name:
                value = _normalize_number(row.get(amount_column))
                if value is not None:
                    return value
    return None


def _report_code_to_period_type(report_code: str) -> str:
    mapping = {
        "11013": "q1",
        "11012": "half_year",
        "11014": "q3",
        "11011": "annual",
    }
    return mapping.get(str(report_code).strip(), "quarterly")


def _fetch_financial_rows_for_report(
    *,
    session: requests.Session,
    api_key: str,
    corp_code: str,
    symbol: str,
    business_year: int,
    report_code: str,
) -> dict[str, Any] | None:
    global _DART_REQUEST_FAILURES
    _raise_if_cancelled()
    for fs_div in ("CFS", "OFS"):
        try:
            payload = _request_json(
                session,
                FINANCIALS_URL,
                {
                    "crtfc_key": api_key,
                    "corp_code": corp_code,
                    "bsns_year": str(business_year),
                    "reprt_code": str(report_code),
                    "fs_div": fs_div,
                },
            )
        except requests.RequestException:
            _DART_REQUEST_FAILURES += 1
            continue
        status = str(payload.get("status", "")).strip()
        rows = payload.get("list") or []
        if status != "000" or not rows:
            continue
        if not isinstance(rows, list):
            continue

        first_row = rows[0] if rows else {}
        fiscal_date = _extract_date_text(first_row.get("thstrm_dt"))
        if fiscal_date is None:
            continue
        row = {
            "symbol": _normalize_symbol(symbol),
            "fiscal_date": fiscal_date,
            "filing_date": _rcept_no_to_date_text(first_row.get("rcept_no")),
            "period_type": _report_code_to_period_type(report_code),
            "revenue": _pick_account_value(rows, "revenue"),
            "operating_income": _pick_account_value(rows, "operating_income"),
            "net_income": _pick_account_value(rows, "net_income"),
            "total_assets": _pick_account_value(rows, "total_assets"),
            "total_liabilities": _pick_account_value(rows, "total_liabilities"),
            "stockholders_equity": _pick_account_value(rows, "stockholders_equity"),
            "operating_cash_flow": _pick_account_value(rows, "operating_cash_flow"),
            "free_cash_flow": None,
            "capex": None,
            "diluted_eps": None,
            "source": f"dart:{report_code}:{fs_div}",
        }
        for metric in FLOW_METRICS:
            row[f"_cumulative_{metric}"] = _pick_account_value(rows, metric, amount_column="thstrm_add_amount")
        return row
    return None


def _normalize_quarterly_flow_values(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    out = frame.copy()
    out["_fiscal_year"] = out["fiscal_date"].astype(str).str.slice(0, 4)
    converted = 0
    for (_symbol, _fiscal_year), group in out.groupby(["symbol", "_fiscal_year"], dropna=False):
        q3 = group[group["period_type"] == "q3"]
        annual = group[group["period_type"] == "annual"]
        if q3.empty or annual.empty:
            continue
        q3_row = q3.iloc[-1]
        for annual_index in annual.index:
            changed_any = False
            for metric in FLOW_METRICS:
                if metric not in out.columns:
                    continue
                annual_value = _normalize_number(out.at[annual_index, metric])
                q3_cumulative = _normalize_number(q3_row.get(f"_cumulative_{metric}"))
                if annual_value is None or q3_cumulative is None:
                    continue
                out.at[annual_index, metric] = annual_value - q3_cumulative
                changed_any = True
            if changed_any:
                out.at[annual_index, "period_type"] = "q4"
                out.at[annual_index, "source"] = f"{out.at[annual_index, 'source']}:q4_from_annual_minus_q3_cumulative"
                converted += 1
            else:
                for metric in FLOW_METRICS:
                    if metric in out.columns:
                        out.at[annual_index, metric] = None
                out.at[annual_index, "source"] = f"{out.at[annual_index, 'source']}:annual_flow_null_no_q3_cumulative"
    helper_cols = [col for col in out.columns if str(col).startswith("_cumulative_") or col == "_fiscal_year"]
    remaining_annual = out["period_type"] == "annual"
    if remaining_annual.any():
        for metric in FLOW_METRICS:
            if metric in out.columns:
                out.loc[remaining_annual, metric] = None
        if "diluted_eps" in out.columns:
            out.loc[remaining_annual, "diluted_eps"] = None
        out.loc[remaining_annual, "source"] = out.loc[remaining_annual, "source"].astype(str) + ":annual_flow_null"
    if helper_cols:
        out = out.drop(columns=helper_cols)
    if converted:
        _log(f"converted annual flow rows to q4 rows={converted}")
    return out


def refresh_krx_dart_quarterly_fundamentals(
    *,
    components_csv: Path = DEFAULT_COMPONENTS_CSV,
    db_path: Path | str | None = None,
    api_key: str | None = None,
    start_year: int = DEFAULT_START_YEAR,
    end_year: int | None = None,
    report_codes: tuple[str, ...] = DEFAULT_REPORT_CODES,
    incremental_overlap_years: int = DEFAULT_INCREMENTAL_OVERLAP_YEARS,
    pause_seconds: float = 0.05,
    insecure_ssl: bool = False,
    ca_bundle: str | None = None,
) -> KRXDartRefreshResult:
    global _DART_REQUEST_FAILURES
    _DART_REQUEST_FAILURES = 0
    _log("loading DART API key")
    resolved_api_key = _load_api_key(api_key)
    _log("DART API key loaded")
    configure_ssl(insecure_ssl=insecure_ssl, ca_bundle=ca_bundle)
    _log("initializing SQLite schema")
    sqlite_result = init_krx_project_db(db_path=Path(db_path) if db_path is not None else None)
    _log(f"SQLite ready db={sqlite_result.db_path}")
    session = requests.Session()
    try:
        corp_code_updates = sync_krx_corp_codes(
            session=session,
            api_key=resolved_api_key,
            db_path=sqlite_result.db_path,
            components_csv=components_csv,
        )

        year_end = int(end_year or pd.Timestamp.today().year)
        years = list(range(int(start_year), year_end + 1))
        if not years:
            raise RuntimeError("No years selected for DART fundamentals refresh")
        _log(
            f"selected_years={years[0]}-{years[-1]} report_codes={','.join(report_codes)} "
            f"overlap_years={incremental_overlap_years}"
        )

        _log("loading active securities with corp_code")
        with sqlite3.connect(sqlite_result.db_path) as conn:
            securities = pd.read_sql_query(
                """
                SELECT symbol, market, corp_code
                FROM securities
                WHERE is_active = 1
                  AND corp_code IS NOT NULL
                  AND TRIM(corp_code) <> ''
                ORDER BY market, symbol
                """,
                conn,
            )
        component_symbols = _component_symbol_set(components_csv)
        if component_symbols and not securities.empty:
            securities = securities[securities["symbol"].map(_normalize_symbol).isin(component_symbols)].reset_index(drop=True)
        _log(f"active securities loaded rows={len(securities.index)}")

        _log("loading latest stored fiscal years")
        latest_years = _latest_fiscal_years(sqlite_result.db_path)
        _log(f"latest fiscal years loaded symbols={len(latest_years)}")
        rows_to_store: list[dict[str, Any]] = []
        total_symbols = len(securities.index)
        batch_size = DEFAULT_PROGRESS_BATCH_SIZE
        batch_start_index = 1
        batch_rows_found = 0
        batch_skipped_symbols = 0
        for index, record in enumerate(securities.itertuples(index=False), start=1):
            _raise_if_cancelled()
            if index == batch_start_index:
                _batch_progress_log(
                    batch_start_index=batch_start_index,
                    batch_end_index=min(batch_start_index + batch_size - 1, total_symbols),
                    total_symbols=total_symbols,
                    note="batch_started",
                )
            symbol = _normalize_symbol(getattr(record, "symbol"))
            corp_code = _normalize_text(getattr(record, "corp_code"))
            if corp_code is None:
                batch_skipped_symbols += 1
                continue
            symbol_years = _incremental_years_for_symbol(
                all_years=years,
                latest_year=latest_years.get(symbol),
                overlap_years=incremental_overlap_years,
            )
            if not symbol_years:
                batch_skipped_symbols += 1
                continue
            for year in symbol_years:
                for report_code in report_codes:
                    row = _fetch_financial_rows_for_report(
                        session=session,
                        api_key=resolved_api_key,
                        corp_code=corp_code,
                        symbol=symbol,
                        business_year=year,
                        report_code=report_code,
                    )
                    if row is not None:
                        rows_to_store.append(row)
                        batch_rows_found += 1
                    if pause_seconds > 0:
                        _interruptible_sleep(pause_seconds)
            if index % DEFAULT_PROGRESS_HEARTBEAT_SYMBOLS == 0 and index % batch_size != 0 and index != total_symbols:
                _batch_progress_log(
                    batch_start_index=batch_start_index,
                    batch_end_index=index,
                    total_symbols=total_symbols,
                    rows_found=batch_rows_found,
                    skipped_symbols=batch_skipped_symbols,
                    note="batch_progress",
                )
            if index % batch_size == 0 or index == total_symbols:
                _batch_progress_log(
                    batch_start_index=batch_start_index,
                    batch_end_index=index,
                    total_symbols=total_symbols,
                    rows_found=batch_rows_found,
                    skipped_symbols=batch_skipped_symbols,
                    note="batch_done",
                )
                batch_start_index = index + 1
                batch_rows_found = 0
                batch_skipped_symbols = 0

        stored = 0
        if rows_to_store:
            frame = pd.DataFrame(rows_to_store)
            frame = _normalize_quarterly_flow_values(frame)
            frame = frame.drop_duplicates(subset=["symbol", "fiscal_date", "period_type"], keep="last")
            stored = upsert_krx_quarterly_fundamentals(frame, db_path=sqlite_result.db_path)
        if _DART_REQUEST_FAILURES:
            _log(f"DART request failures skipped={_DART_REQUEST_FAILURES}")
        if not rows_to_store and _DART_REQUEST_FAILURES:
            raise RuntimeError(
                "DART requests failed before any fundamentals rows were collected. "
                "Try again with pause_seconds >= 0.2 or check network/TLS settings."
            )
        return KRXDartRefreshResult(
            symbol_count=total_symbols,
            corp_code_updates=corp_code_updates,
            fundamentals_rows=stored,
            sqlite_path=sqlite_result.db_path,
        )
    finally:
        try:
            session.close()
        except Exception:
            pass


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh KRX quarterly fundamentals from official DART APIs.")
    parser.add_argument("--components-csv", default=str(DEFAULT_COMPONENTS_CSV), help="KRX components CSV path")
    parser.add_argument("--db-path", default="", help="Optional SQLite DB path override")
    parser.add_argument("--api-key", default="", help="DART API key (or use KEUMJ_DART_API_KEY)")
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR, help="First business year to pull")
    parser.add_argument("--end-year", type=int, default=0, help="Last business year to pull (default: current year)")
    parser.add_argument(
        "--incremental-overlap-years",
        type=int,
        default=DEFAULT_INCREMENTAL_OVERLAP_YEARS,
        help="Years to overlap from each symbol's latest stored fiscal year",
    )
    parser.add_argument(
        "--report-codes",
        default=",".join(DEFAULT_REPORT_CODES),
        help="Comma-separated DART report codes (default: 11013,11012,11014,11011)",
    )
    parser.add_argument("--pause-seconds", type=float, default=0.05, help="Delay between DART requests")
    parser.add_argument("--ca-bundle", default="", help="CA bundle path")
    parser.add_argument("--insecure-ssl", action="store_true", help="Disable TLS verification for temporary testing")
    return parser.parse_args()


def main() -> int:
    global _CANCEL_REQUESTED
    args = _parse_args()
    _CANCEL_REQUESTED = False
    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        result = refresh_krx_dart_quarterly_fundamentals(
            components_csv=Path(args.components_csv),
            db_path=Path(args.db_path) if str(args.db_path).strip() else None,
            api_key=str(args.api_key).strip() or None,
            start_year=int(args.start_year),
            end_year=int(args.end_year) or None,
            report_codes=tuple(
                str(item).strip() for item in str(args.report_codes).split(",") if str(item).strip()
            )
            or DEFAULT_REPORT_CODES,
            incremental_overlap_years=int(args.incremental_overlap_years),
            pause_seconds=float(args.pause_seconds),
            insecure_ssl=bool(args.insecure_ssl),
            ca_bundle=str(args.ca_bundle).strip() or None,
        )
    except KeyboardInterrupt:
        _log("Cancelled by user (Ctrl+C).")
        return 130
    finally:
        signal.signal(signal.SIGINT, previous_handler)
    print(
        "refreshed_krx_dart_quarterly_fundamentals",
        f"symbols={result.symbol_count}",
        f"corp_code_updates={result.corp_code_updates}",
        f"fundamentals_rows={result.fundamentals_rows}",
        f"sqlite_path={result.sqlite_path}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
