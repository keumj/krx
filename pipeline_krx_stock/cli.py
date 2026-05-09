from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pipeline_common.security import configure_ssl, ensure_writable_dir, security_hint

from .forecast import load_price_data_csv, run_ticker_stock_forecast_pipeline
from .technical_analysis import launch_web_gui as launch_technical_web_gui
from .web_gui import launch_web_gui

DEFAULT_OUT_DIR = Path("outputs/stock_forecast")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Unified KRX stock pipeline")
    p.add_argument("--web-gui", action="store_true", help="Launch the unified stock web GUI")
    p.add_argument("--open-browser", action="store_true", help="Open the unified stock web GUI in the default browser")
    p.add_argument("--host", default="localhost", help="Host for --web-gui")
    p.add_argument("--port", type=int, default=8522, help="Port for --web-gui")
    p.add_argument("--technical-web-gui", action="store_true", help="Launch the technical-analysis web GUI")
    p.add_argument("--technical-host", default="localhost", help="Host for --technical-web-gui")
    p.add_argument("--technical-port", type=int, default=8792, help="Port for --technical-web-gui")
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--ticker", help="Optional ticker override")
    p.add_argument("--forecast-horizon", type=int, default=10, help="Forecast horizon in business days")
    p.add_argument("--history-years", type=int, default=8, help="Price history years to download")
    p.add_argument("--start-date", help="Start date (YYYY-MM-DD)")
    p.add_argument("--end-date", help="End date (YYYY-MM-DD)")
    p.add_argument("--prices-csv", type=Path, help="Local prices CSV path (date,close) for offline mode")
    p.add_argument("--insecure-ssl", action="store_true", help="Disable TLS certificate verification (temporary)")
    p.add_argument("--ca-bundle", help="Custom CA bundle path for TLS inspection environments")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    try:
        configure_ssl(insecure_ssl=args.insecure_ssl, ca_bundle=args.ca_bundle)

        if args.web_gui:
            launch_web_gui(host=args.host, port=args.port, open_browser=args.open_browser)
            return

        if args.technical_web_gui:
            launch_technical_web_gui(host=args.technical_host, port=args.technical_port)
            return

        ensure_writable_dir(args.out_dir)

        local_price_data = None
        ticker_for_run = args.ticker

        if args.prices_csv:
            local_price_data = load_price_data_csv(args.prices_csv)
            if not ticker_for_run:
                ticker_for_run = "LOCAL"

        if local_price_data is None and not ticker_for_run:
            raise ValueError("Provide --ticker, or --prices-csv for offline mode")

        result = run_ticker_stock_forecast_pipeline(
            ticker=ticker_for_run,
            horizon_days=args.forecast_horizon,
            start_date=args.start_date,
            end_date=args.end_date,
            history_years=args.history_years,
            output_dir=args.out_dir,
            price_data=local_price_data,
            insecure_ssl=args.insecure_ssl,
            ca_bundle=args.ca_bundle,
        )
        print(f"Saved stock forecast outputs to {args.out_dir}")
        print(result.summary.to_string(index=False))
        print(result.model_scores.to_string(index=False))
    except Exception as exc:
        hint = security_hint(exc, output_dir=getattr(args, "out_dir", None))
        if hint:
            print(hint, file=sys.stderr)
            raise SystemExit(2)
        raise


if __name__ == "__main__":
    main()
