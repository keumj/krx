from __future__ import annotations

import argparse
import os
import signal
from pathlib import Path

from .refresh_dart_bulk_fundamentals import DEFAULT_BULK_CACHE_DIR, refresh_krx_dart_bulk_fundamentals
from .refresh_dart_fundamentals import (
    DEFAULT_COMPONENTS_CSV,
    DEFAULT_REPORT_CODES,
    DEFAULT_START_YEAR,
    refresh_krx_dart_quarterly_fundamentals,
)


def _log(message: str) -> None:
    print(f"[refresh-krx-dart-auto] {message}", flush=True)


def _local_bulk_zip_exists(cache_dir: Path) -> bool:
    return any(path.is_file() for path in cache_dir.glob("*.zip"))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh KRX DART fundamentals using local bulk ZIPs when available, otherwise API fallback.")
    parser.add_argument("--components-csv", default=str(DEFAULT_COMPONENTS_CSV), help="KRX components CSV path")
    parser.add_argument("--db-path", default="", help="Optional SQLite DB path override")
    parser.add_argument("--api-key", default="", help="DART API key for API fallback")
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR, help="First business year to pull")
    parser.add_argument("--end-year", type=int, default=0, help="Last business year to pull (default: current year)")
    parser.add_argument(
        "--report-codes",
        default=",".join(DEFAULT_REPORT_CODES),
        help="Comma-separated DART report codes (default: 11013,11012,11014,11011)",
    )
    parser.add_argument("--cache-dir", default=str(DEFAULT_BULK_CACHE_DIR), help="Directory containing downloaded DART ZIP files")
    parser.add_argument("--pause-seconds", type=float, default=0.05, help="Delay between requests/downloads")
    parser.add_argument("--ca-bundle", default="", help="CA bundle path")
    parser.add_argument("--insecure-ssl", action="store_true", help="Disable TLS verification for temporary testing")
    parser.add_argument("--bulk-online", action="store_true", help="Try online DART bulk downloads when no local ZIP files exist")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    previous_handler = signal.getsignal(signal.SIGINT)
    report_codes = tuple(str(item).strip() for item in str(args.report_codes).split(",") if str(item).strip()) or DEFAULT_REPORT_CODES
    components_csv = Path(args.components_csv)
    db_path = Path(args.db_path) if str(args.db_path).strip() else None
    cache_dir = Path(args.cache_dir)
    try:
        if _local_bulk_zip_exists(cache_dir) or bool(args.bulk_online):
            _log("using bulk mode")
            result = refresh_krx_dart_bulk_fundamentals(
                components_csv=components_csv,
                db_path=db_path,
                start_year=int(args.start_year),
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

        api_key = str(args.api_key).strip() or str(os.getenv("KEUMJ_DART_API_KEY", "")).strip()
        if not api_key:
            raise RuntimeError(
                f"No local DART bulk ZIP files found in {cache_dir} and no DART API key is available for fallback."
            )
        _log("no local bulk ZIP files found; using DART API fallback")
        result = refresh_krx_dart_quarterly_fundamentals(
            components_csv=components_csv,
            db_path=db_path,
            api_key=api_key,
            start_year=int(args.start_year),
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
