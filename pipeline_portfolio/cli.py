from __future__ import annotations

import argparse
import sys

from .analysis import (
    DEFAULT_CASH_BUFFER_PCT,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MAX_POSITION_PCT,
    DEFAULT_OPTIMIZATION_UNIVERSE_SIZE,
    DEFAULT_SECTOR_CAP_PCT,
    build_portfolio_dashboard,
    build_portfolio_optimization,
)
from .web_gui import launch_web_gui


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Independent portfolio lab over the shared S&P 500 DB.")
    parser.add_argument("--web-gui", action="store_true", help="Launch the portfolio web GUI")
    parser.add_argument("--open-browser", action="store_true", help="Open the portfolio web GUI in a browser")
    parser.add_argument("--host", default="localhost", help="Host for --web-gui")
    parser.add_argument("--port", type=int, default=8515, help="Port for --web-gui")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS, help="Lookback window for analytics")
    parser.add_argument(
        "--optimization-universe-size",
        type=int,
        default=DEFAULT_OPTIMIZATION_UNIVERSE_SIZE,
        help="Universe size for optimization pages",
    )
    parser.add_argument("--sector-cap-pct", type=float, default=DEFAULT_SECTOR_CAP_PCT, help="Sector cap for optimization")
    parser.add_argument("--max-position-pct", type=float, default=DEFAULT_MAX_POSITION_PCT, help="Single-name max weight for optimization")
    parser.add_argument("--cash-buffer-pct", type=float, default=DEFAULT_CASH_BUFFER_PCT, help="Cash buffer for optimization")
    return parser.parse_args()


def _print_frame(title: str, frame, *, max_rows: int = 12) -> None:
    print(f"\n[{title}]")
    if frame is None or frame.empty:
        print("No data")
        return
    text = frame.head(max_rows).to_string(index=False)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe)


def main() -> None:
    args = parse_args()
    try:
        if args.web_gui:
            launch_web_gui(host=args.host, port=args.port, open_browser=args.open_browser)
            return
        dashboard = build_portfolio_dashboard(lookback_days=max(int(args.lookback_days), 21))
        optimization = build_portfolio_optimization(
            lookback_days=max(int(args.lookback_days), 21),
            universe_size=max(int(args.optimization_universe_size), 20),
            sector_cap_pct=max(float(args.sector_cap_pct), 1.0),
            max_position_pct=max(float(args.max_position_pct), 0.5),
            cash_buffer_pct=min(max(float(args.cash_buffer_pct), 0.0), 95.0),
        )
        print(
            "Portfolio Lab summary "
            f"(positions={len(dashboard.positions.index)}, "
            f"trades={len(dashboard.trades.index)}, "
            f"as_of={dashboard.as_of_date or 'N/A'})"
        )
        _print_frame("Portfolio Summary", dashboard.portfolio_summary)
        _print_frame("Holdings", dashboard.holdings_performance)
        _print_frame("Attribution", dashboard.attribution)
        _print_frame("Risk Summary", dashboard.risk_summary)
        _print_frame("Factor Risk", dashboard.factor_risk)
        _print_frame("Scoring", dashboard.scoring)
        _print_frame("SP500 Replication", optimization.replication)
        _print_frame("Aggressive", optimization.aggressive)
        _print_frame("Defensive", optimization.defensive)
    except Exception as exc:
        print(f"Portfolio analysis failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
