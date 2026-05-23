from __future__ import annotations

import argparse
import io
import re
import signal
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests

from pipeline_common.security import configure_ssl

from .db import init_krx_project_db, upsert_krx_quarterly_fundamentals
from .refresh_dart_fundamentals import (
    ACCOUNT_ID_CANDIDATES,
    ACCOUNT_NAME_CANDIDATES,
    DEFAULT_COMPONENTS_CSV,
    DEFAULT_REPORT_CODES,
    DEFAULT_START_YEAR,
    _extract_date_text,
    _normalize_number,
    _normalize_symbol,
    _report_code_to_period_type,
)


BULK_LIST_URL = "https://opendart.fss.or.kr/disclosureinfo/fnltt/dwld/list.do"
DEFAULT_BULK_CACHE_DIR = Path("data/dart_bulk")
DEFAULT_PROGRESS_BATCH_SIZE = 200
REPORT_NAME_TO_CODE = {
    "1분기보고서": "11013",
    "반기보고서": "11012",
    "3분기보고서": "11014",
    "사업보고서": "11011",
}
REPORT_CODE_TO_END_MMDD = {
    "11013": "03-31",
    "11012": "06-30",
    "11014": "09-30",
    "11011": "12-31",
}
STATEMENT_HINTS = {
    "bs": ("재무상태표", "BS"),
    "pl": ("손익계산서", "포괄손익계산서", "IS", "CIS", "PL"),
    "cf": ("현금흐름표", "CF"),
}
_CANCEL_REQUESTED = False


@dataclass(frozen=True)
class KRXDartBulkRefreshResult:
    symbol_count: int
    files_processed: int
    fundamentals_rows: int
    sqlite_path: Path


def _log(message: str) -> None:
    print(f"[refresh-krx-dart-bulk] {message}", flush=True)


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


def _load_component_symbols(path: Path) -> set[str]:
    frame = pd.read_csv(path)
    cols = {str(col).strip().lower(): col for col in frame.columns}
    symbol_col = cols.get("symbol")
    if symbol_col is None:
        raise RuntimeError(f"KRX components CSV has no Symbol column: {path}")
    return {
        _normalize_symbol(value)
        for value in frame[symbol_col].dropna()
        if str(value).strip()
    }


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_bulk_text_frame(data: bytes) -> pd.DataFrame:
    text = _decode_text(data)
    return pd.read_csv(io.StringIO(text), sep="\t", dtype=str)


def _metric_for_row(row: pd.Series) -> str | None:
    account_id = str(row.get("항목코드") or row.get("account_id") or "").strip()
    account_name = str(row.get("항목명") or row.get("account_nm") or "").strip()
    for metric, candidates in ACCOUNT_ID_CANDIDATES.items():
        if account_id in candidates:
            return metric
    for metric, candidates in ACCOUNT_NAME_CANDIDATES.items():
        if account_name in candidates:
            return metric
    return None


def _value_columns(frame: pd.DataFrame, statement_kind: str) -> list[str]:
    cols = [str(col) for col in frame.columns]
    current_cols = [col for col in cols if "당기" in col]
    if statement_kind in {"pl", "cf"}:
        period_cols = [col for col in current_cols if "누적" not in col]
        if period_cols:
            return period_cols
    return current_cols or [col for col in cols if col in {"thstrm_amount", "당기"}]


def _row_value(row: pd.Series, columns: list[str]) -> float | None:
    for column in columns:
        value = _normalize_number(row.get(column))
        if value is not None:
            return value
    return None


def _statement_kind_from_name(name: str) -> str | None:
    lowered = name.lower()
    for kind, hints in STATEMENT_HINTS.items():
        if any(hint.lower() in lowered for hint in hints):
            return kind
    return None


def _fiscal_date_for_report(year: int, report_code: str, frame: pd.DataFrame) -> str:
    for column in ("결산기준일", "thstrm_dt"):
        if column in frame.columns:
            for value in frame[column].dropna().head(50):
                date_text = _extract_date_text(value)
                if date_text:
                    return date_text
    return f"{int(year):04d}-{REPORT_CODE_TO_END_MMDD.get(str(report_code), '12-31')}"


def _fs_preference(name: str, frame: pd.DataFrame) -> int:
    text = " ".join([name, " ".join(str(value) for value in frame.head(3).to_numpy().ravel())])
    if "연결" in text or "CFS" in text.upper():
        return 0
    return 1


def parse_dart_bulk_zip(
    *,
    zip_bytes: bytes,
    year: int,
    report_code: str,
    component_symbols: set[str],
    source_label: str,
) -> pd.DataFrame:
    rows_by_key: dict[tuple[str, str, str], dict[str, object]] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        members = [member for member in archive.namelist() if member.lower().endswith((".txt", ".tsv", ".csv"))]
        for member in members:
            _raise_if_cancelled()
            statement_kind = _statement_kind_from_name(member)
            if statement_kind is None:
                continue
            frame = _read_bulk_text_frame(archive.read(member))
            if frame.empty:
                continue
            cols = {str(col).strip().lower(): col for col in frame.columns}
            symbol_col = cols.get("종목코드") or cols.get("stock_code") or cols.get("symbol")
            if symbol_col is None:
                continue
            frame = frame.copy()
            frame["_symbol"] = frame[symbol_col].map(_normalize_symbol)
            if component_symbols:
                frame = frame[frame["_symbol"].isin(component_symbols)]
            if frame.empty:
                continue
            fiscal_date = _fiscal_date_for_report(year, report_code, frame)
            period_type = _report_code_to_period_type(report_code)
            value_columns = _value_columns(frame, statement_kind)
            preference = _fs_preference(member, frame)
            for _, row in frame.iterrows():
                metric = _metric_for_row(row)
                if metric is None:
                    continue
                value = _row_value(row, value_columns)
                if value is None:
                    continue
                symbol = _normalize_symbol(row["_symbol"])
                key = (symbol, fiscal_date, period_type)
                existing = rows_by_key.get(key)
                if existing is None or preference <= int(existing.get("_preference", 9)):
                    base = existing if existing is not None and preference == int(existing.get("_preference", 9)) else {
                        "symbol": symbol,
                        "fiscal_date": fiscal_date,
                        "filing_date": None,
                        "period_type": period_type,
                        "revenue": None,
                        "operating_income": None,
                        "net_income": None,
                        "total_assets": None,
                        "total_liabilities": None,
                        "stockholders_equity": None,
                        "operating_cash_flow": None,
                        "free_cash_flow": None,
                        "capex": None,
                        "diluted_eps": None,
                        "source": source_label,
                        "_preference": preference,
                    }
                    base[metric] = value
                    rows_by_key[key] = base
    if not rows_by_key:
        return pd.DataFrame()
    out = pd.DataFrame(rows_by_key.values()).drop(columns=["_preference"], errors="ignore")
    return out.drop_duplicates(subset=["symbol", "fiscal_date", "period_type"], keep="last")


def _download_candidates_from_list(
    *,
    session: requests.Session,
    start_year: int,
    end_year: int,
    report_codes: tuple[str, ...],
) -> list[tuple[int, str, str]]:
    response = session.get(BULK_LIST_URL, timeout=60)
    response.raise_for_status()
    html = response.text
    candidates: list[tuple[int, str, str]] = []
    anchor_pattern = re.compile(r"<a\b[^>]*(?:href|onclick)=['\"]([^'\"]+)['\"][^>]*>\s*다운로드\s*</a>", re.I)
    row_pattern = re.compile(r"(20\d{2}).{0,200}?(1분기보고서|반기보고서|3분기보고서|사업보고서).{0,1200}", re.S)
    for row_match in row_pattern.finditer(html):
        year = int(row_match.group(1))
        report_code = REPORT_NAME_TO_CODE.get(row_match.group(2))
        if report_code not in report_codes or year < start_year or year > end_year:
            continue
        row_html = row_match.group(0)
        for link_match in anchor_pattern.finditer(row_html):
            link = link_match.group(1)
            if not any(hint in link for hint in ("재무상태표", "손익계산서", "포괄손익계산서", "현금흐름표", "BS", "IS", "CIS", "PL", "CF")):
                continue
            candidates.append((year, report_code, urljoin(BULK_LIST_URL, link)))
    return candidates


def _fallback_bulk_urls(year: int, report_code: str) -> list[str]:
    urls: list[str] = []
    for statement in ("BS", "IS", "CIS", "CF"):
        urls.extend(
            [
                f"https://opendart.fss.or.kr/disclosureinfo/fnltt/dwld/download.do?bsns_year={year}&reprt_code={report_code}&fs_div={statement}",
                f"https://opendart.fss.or.kr/disclosureinfo/fnltt/dwld/download.do?year={year}&reprtCode={report_code}&fsDiv={statement}",
            ]
        )
    return urls


def _infer_year_report_from_name(path: Path) -> tuple[int, str] | None:
    name = path.name
    year_match = re.search(r"(20\d{2})", name)
    if year_match is None:
        return None
    report_code = None
    for report_name, candidate_code in REPORT_NAME_TO_CODE.items():
        if report_name in name:
            report_code = candidate_code
            break
    if report_code is None:
        for candidate_code in REPORT_CODE_TO_END_MMDD:
            if candidate_code in name:
                report_code = candidate_code
                break
    if report_code is None:
        lowered = name.lower()
        if "1q" in lowered or "q1" in lowered:
            report_code = "11013"
        elif "half" in lowered or "2q" in lowered:
            report_code = "11012"
        elif "3q" in lowered or "q3" in lowered:
            report_code = "11014"
        elif "annual" in lowered or "fy" in lowered:
            report_code = "11011"
    if report_code is None:
        return None
    return int(year_match.group(1)), report_code


def _download_zip(session: requests.Session, url: str) -> bytes | None:
    try:
        response = session.get(url, timeout=120)
    except requests.RequestException as exc:
        _log(f"download skipped: {type(exc).__name__}: {exc}")
        return None
    if response.status_code >= 400:
        return None
    content = response.content or b""
    if len(content) < 128:
        return None
    if zipfile.is_zipfile(io.BytesIO(content)):
        return content
    return None


def refresh_krx_dart_bulk_fundamentals(
    *,
    components_csv: Path = DEFAULT_COMPONENTS_CSV,
    db_path: Path | str | None = None,
    start_year: int = DEFAULT_START_YEAR,
    end_year: int | None = None,
    report_codes: tuple[str, ...] = DEFAULT_REPORT_CODES,
    cache_dir: Path = DEFAULT_BULK_CACHE_DIR,
    pause_seconds: float = 0.05,
    insecure_ssl: bool = False,
    ca_bundle: str | None = None,
) -> KRXDartBulkRefreshResult:
    configure_ssl(insecure_ssl=insecure_ssl, ca_bundle=ca_bundle)
    sqlite_result = init_krx_project_db(db_path=Path(db_path) if db_path is not None else None)
    component_symbols = _load_component_symbols(components_csv)
    year_end = int(end_year or pd.Timestamp.today().year)
    years = list(range(int(start_year), year_end + 1))
    if not years:
        raise RuntimeError("No years selected for DART bulk refresh")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://opendart.fss.or.kr/disclosureinfo/fnltt/dwld/main.do",
        }
    )
    all_frames: list[pd.DataFrame] = []
    files_processed = 0
    cache_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(cache_dir.glob("*.zip")):
        inferred = _infer_year_report_from_name(path)
        if inferred is None:
            continue
        year, report_code = inferred
        if year not in years or report_code not in report_codes:
            continue
        frame = parse_dart_bulk_zip(
            zip_bytes=path.read_bytes(),
            year=year,
            report_code=report_code,
            component_symbols=component_symbols,
            source_label=f"dart-bulk-local:{year}:{report_code}",
        )
        if not frame.empty:
            all_frames.append(frame)
            files_processed += 1
    if files_processed:
        rows_seen = sum(len(frame.index) for frame in all_frames)
        _log(f"loaded local bulk zip files={files_processed} rows_seen={rows_seen}")
    try:
        try:
            candidates = _download_candidates_from_list(
                session=session,
                start_year=min(years),
                end_year=max(years),
                report_codes=report_codes,
            )
        except Exception as exc:
            _log(f"bulk list discovery failed; trying known URL patterns: {exc}")
            candidates = []
        if not candidates:
            for year in years:
                for report_code in report_codes:
                    candidates.extend((year, report_code, url) for url in _fallback_bulk_urls(year, report_code))

        seen: set[tuple[int, str, str]] = set()
        total = len(candidates)
        failed_downloads = 0
        for index, (year, report_code, url) in enumerate(candidates, start=1):
            _raise_if_cancelled()
            key = (int(year), str(report_code), str(url))
            if key in seen:
                continue
            seen.add(key)
            if index == 1 or (index - 1) % DEFAULT_PROGRESS_BATCH_SIZE == 0:
                _log(f"{index}-{min(index + DEFAULT_PROGRESS_BATCH_SIZE - 1, total)}/{total} batch_started")
            cache_name = re.sub(r"[^0-9A-Za-z_.-]+", "_", f"{year}_{report_code}_{Path(url).name or index}.zip")
            cache_path = cache_dir / cache_name
            zip_bytes = cache_path.read_bytes() if cache_path.exists() else None
            if zip_bytes is None:
                zip_bytes = _download_zip(session, url)
                if zip_bytes is None:
                    failed_downloads += 1
                    continue
                cache_path.write_bytes(zip_bytes)
            frame = parse_dart_bulk_zip(
                zip_bytes=zip_bytes,
                year=int(year),
                report_code=str(report_code),
                component_symbols=component_symbols,
                source_label=f"dart-bulk:{year}:{report_code}",
            )
            if not frame.empty:
                all_frames.append(frame)
                files_processed += 1
            if index % DEFAULT_PROGRESS_BATCH_SIZE == 0 or index == total:
                rows_seen = sum(len(frame.index) for frame in all_frames)
                _log(
                    f"{max(index - DEFAULT_PROGRESS_BATCH_SIZE + 1, 1)}-{index}/{total} batch_done "
                    f"files_processed={files_processed} failed_downloads={failed_downloads} rows_seen={rows_seen}"
                )
            if pause_seconds > 0 and index < total:
                _interruptible_sleep(pause_seconds)
    finally:
        session.close()

    if not all_frames:
        raise RuntimeError(
            "No DART bulk files were downloaded or parsed. "
            "DART may be closing automated HTTPS requests from this environment; "
            "download one or more financial bulk ZIP files in a browser and place them under data/dart_bulk, then rerun."
        )
    combined = pd.concat(all_frames, axis=0, ignore_index=True)
    combined = combined.drop_duplicates(subset=["symbol", "fiscal_date", "period_type"], keep="last")
    stored = upsert_krx_quarterly_fundamentals(combined, db_path=sqlite_result.db_path)
    return KRXDartBulkRefreshResult(
        symbol_count=len(component_symbols),
        files_processed=files_processed,
        fundamentals_rows=stored,
        sqlite_path=sqlite_result.db_path,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bulk refresh KRX quarterly fundamentals from DART financial TXT ZIP files.")
    parser.add_argument("--components-csv", default=str(DEFAULT_COMPONENTS_CSV), help="KRX components CSV path")
    parser.add_argument("--db-path", default="", help="Optional SQLite DB path override")
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR, help="First business year to pull")
    parser.add_argument("--end-year", type=int, default=0, help="Last business year to pull (default: current year)")
    parser.add_argument(
        "--report-codes",
        default=",".join(DEFAULT_REPORT_CODES),
        help="Comma-separated DART report codes (default: 11013,11012,11014,11011)",
    )
    parser.add_argument("--cache-dir", default=str(DEFAULT_BULK_CACHE_DIR), help="Directory to cache downloaded DART ZIP files")
    parser.add_argument("--pause-seconds", type=float, default=0.05, help="Delay between bulk downloads")
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
        result = refresh_krx_dart_bulk_fundamentals(
            components_csv=Path(args.components_csv),
            db_path=Path(args.db_path) if str(args.db_path).strip() else None,
            start_year=int(args.start_year),
            end_year=int(args.end_year) or None,
            report_codes=tuple(str(item).strip() for item in str(args.report_codes).split(",") if str(item).strip())
            or DEFAULT_REPORT_CODES,
            cache_dir=Path(args.cache_dir),
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
        "refreshed_krx_dart_bulk_fundamentals",
        f"symbols={result.symbol_count}",
        f"files_processed={result.files_processed}",
        f"fundamentals_rows={result.fundamentals_rows}",
        f"sqlite_path={result.sqlite_path}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
