from __future__ import annotations

import argparse
import signal
from pathlib import Path

from scripts.incremental_open_dart_krx_fs import (
    DEFAULT_COMPONENTS_CSV,
    DEFAULT_OVERLAP_YEARS,
    DEFAULT_REPORT_TYPES,
    refresh_open_dart_krx_fs_incremental,
)

from .refresh_dart_bulk_fundamentals import DEFAULT_BULK_CACHE_DIR, refresh_krx_dart_bulk_fundamentals
from .refresh_dart_fundamentals import DEFAULT_REPORT_CODES, refresh_krx_dart_quarterly_fundamentals


def _log(message: str) -> None:
    print(f"[refresh-krx-dart-auto] {message}", flush=True)


def _local_bulk_zip_exists(cache_dir: Path) -> bool:
    return any(path.is_file() for path in cache_dir.glob("*.zip"))


def _report_types_from_codes(report_codes_text: str) -> tuple[tuple[str, str], ...]:
    selected = [item.strip() for item in str(report_codes_text or "").split(",") if item.strip()]
    if not selected:
        return DEFAULT_REPORT_TYPES
    label_by_code = {code: label for label, code in DEFAULT_REPORT_TYPES}
    return tuple((label_by_code.get(code, code), code) for code in selected)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh KRX DART fundamentals. Default mode is incremental OpenDART CSV sync.")
    parser.add_argument("--components-csv", default=str(DEFAULT_COMPONENTS_CSV), help="KRX components CSV path")
    parser.add_argument("--db-path", default="", help="Optional SQLite DB path override")
    parser.add_argument("--api-key", default="", help="DART API key. Falls back to environment variables and registry.")
    parser.add_argument("--start-year", type=int, default=0, help="First business year to pull. Omit/0 for incremental overlap.")
    parser.add_argument("--end-year", type=int, default=0, help="Last business year to pull (default: current year)")
    parser.add_argument("--overlap-years", type=int, default=DEFAULT_OVERLAP_YEARS, help="Years to overlap in incremental mode")
    parser.add_argument(
        "--report-codes",
        default=",".join(DEFAULT_REPORT_CODES),
        help="Comma-separated DART report codes (default: 11013,11012,11014,11011)",
    )
    parser.add_argument("--cache-dir", default=str(DEFAULT_BULK_CACHE_DIR), help="Directory containing downloaded DART ZIP files")
    parser.add_argument("--pause-seconds", type=float, default=0.5, help="Delay between requests/downloads")
    parser.add_argument("--ca-bundle", default="", help="CA bundle path for legacy API/bulk modes")
    parser.add_argument("--insecure-ssl", action="store_true", help="Disable TLS verification for temporary testing")
    parser.add_argument("--bulk-online", action="store_true", help="Try online DART bulk downloads in bulk mode")
    parser.add_argument("--mode", choices=("incremental", "bulk", "api", "auto"), default="incremental")
    parser.add_argument("--force", action="store_true", help="Incremental mode: fetch even when matching local/DB reports exist")
    parser.add_argument("--skip-local-db-sync", action="store_true", help="Incremental mode: skip loading local CSVs absent from SQLite")
    parser.add_argument("--skip-fundamentals-sync", action="store_true", help="Incremental mode: skip fundamentals_quarterly sync")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    previous_handler = signal.getsignal(signal.SIGINT)
    report_codes = tuple(str(item).strip() for item in str(args.report_codes).split(",") if str(item).strip()) or DEFAULT_REPORT_CODES
    components_csv = Path(args.components_csv)
    db_path = Path(args.db_path) if str(args.db_path).strip() else None
    cache_dir = Path(args.cache_dir)
    try:
        mode = str(args.mode)
        if mode == "auto":
            mode = "bulk" if _local_bulk_zip_exists(cache_dir) or bool(args.bulk_online) else "incremental"

        if mode == "incremental":
            _log("using incremental OpenDART CSV mode")
            result = refresh_open_dart_krx_fs_incremental(
                components_csv=components_csv,
                db_path=db_path or Path("data/krx_shared_db/krx_shared_prices.sqlite"),
                api_key=str(args.api_key).strip() or None,
                start_year=int(args.start_year) or None,
                end_year=int(args.end_year) or None,
                overlap_years=int(args.overlap_years),
                report_types=_report_types_from_codes(str(args.report_codes)),
                pause_seconds=float(args.pause_seconds),
                force=bool(args.force),
                sync_local_missing=not bool(args.skip_local_db_sync),
                sync_fundamentals=not bool(args.skip_fundamentals_sync),
            )
            print(
                "refreshed_krx_dart_auto_fundamentals",
                "mode=incremental",
                f"symbols={result.symbols_seen}",
                f"planned={result.requests_planned}",
                f"attempted={result.requests_attempted}",
                f"saved_files={result.saved_files}",
                f"skipped_existing={result.skipped_existing}",
                f"empty={result.empty_reports}",
                f"failed={result.failed_reports}",
                f"sqlite_path={db_path or Path('data/krx_shared_db/krx_shared_prices.sqlite')}",
            )
            return 0

        if mode == "bulk":
            _log("using bulk mode")
            result = refresh_krx_dart_bulk_fundamentals(
                components_csv=components_csv,
                db_path=db_path,
                start_year=int(args.start_year) or 2019,
                end_year=int(args.end_year) or None,
                report_codes=report_codes,
                cache_dir=cache_dir,
                pause_seconds=float(args.pause_seconds),
                insecure_ssl=bool(args.insecure_ssl),
                ca_bundle=str(args.ca_bundle).strip() or None,
            )
            print(
                "refreshed_krx_dart_auto_fundamentals",
                "mode=bulk",
                f"symbols={result.symbol_count}",
                f"files_processed={result.files_processed}",
                f"fundamentals_rows={result.fundamentals_rows}",
                f"sqlite_path={result.sqlite_path}",
            )
            return 0

        _log("using legacy DART API mode")
        result = refresh_krx_dart_quarterly_fundamentals(
            components_csv=components_csv,
            db_path=db_path,
            api_key=str(args.api_key).strip() or None,
            start_year=int(args.start_year) or 2019,
            end_year=int(args.end_year) or None,
            report_codes=report_codes,
            pause_seconds=float(args.pause_seconds),
            insecure_ssl=bool(args.insecure_ssl),
            ca_bundle=str(args.ca_bundle).strip() or None,
        )
        print(
            "refreshed_krx_dart_auto_fundamentals",
            "mode=api",
            f"symbols={result.symbol_count}",
            f"corp_code_updates={result.corp_code_updates}",
            f"fundamentals_rows={result.fundamentals_rows}",
            f"sqlite_path={result.sqlite_path}",
        )
        return 0
    except KeyboardInterrupt:
        _log("Cancelled by user (Ctrl+C).")
        return 130
    except Exception as exc:
        _log(f"[error] {type(exc).__name__}: {exc}")
        return 1
    finally:
        signal.signal(signal.SIGINT, previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())
