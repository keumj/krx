from __future__ import annotations

import argparse
import sys

from .analysis import (
    DEFAULT_DIVERGENCE_TOP_N,
    DEFAULT_EVENT_HORIZON_DAYS,
    DEFAULT_EVENT_KEYWORDS,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_TOPIC_COUNT,
    build_stock_news_dashboard,
)
from .web_gui import launch_web_gui


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Independent stock news analysis tool over the shared SQLite DB.")
    parser.add_argument("--web-gui", action="store_true", help="Launch the stock-news web GUI")
    parser.add_argument("--open-browser", action="store_true", help="Open the stock-news web GUI in the default browser")
    parser.add_argument("--host", default="localhost", help="Host for --web-gui")
    parser.add_argument("--port", type=int, default=8514, help="Port for --web-gui")
    parser.add_argument("--event-keywords", default=DEFAULT_EVENT_KEYWORDS, help="Comma-separated keywords for event study")
    parser.add_argument("--ticker", default="", help="Optional ticker filter")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS, help="Recent window in calendar days")
    parser.add_argument("--horizon-days", type=int, default=DEFAULT_EVENT_HORIZON_DAYS, help="Forward event-study horizon in trading days")
    parser.add_argument("--divergence-top-n", type=int, default=DEFAULT_DIVERGENCE_TOP_N, help="Top divergence alerts to show")
    parser.add_argument("--topic-count", type=int, default=DEFAULT_TOPIC_COUNT, help="Number of topic buckets")
    return parser.parse_args()


def _print_dataframe(title: str, frame, *, max_rows: int = 10) -> None:
    print(f"\n[{title}]")
    if frame is None or frame.empty:
        print("No data")
        return
    text = frame.head(max_rows).to_string(index=False)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe_text)


def main() -> None:
    args = parse_args()
    try:
        if args.web_gui:
            launch_web_gui(host=args.host, port=args.port, open_browser=args.open_browser)
            return
        dashboard = build_stock_news_dashboard(
            event_keywords=args.event_keywords,
            ticker=args.ticker.strip().upper() or None,
            lookback_days=max(int(args.lookback_days), 1),
            horizon_days=max(int(args.horizon_days), 1),
            divergence_top_n=max(int(args.divergence_top_n), 1),
            topic_count=max(int(args.topic_count), 2),
        )
        print(
            "Stock News Lab summary "
            f"(event_matches={dashboard.event_study.article_count}, "
            f"divergence_alerts={len(dashboard.divergence.alerts.index)}, "
            f"topics={len(dashboard.topics.topics.index)}, "
            f"ticker={dashboard.applied_ticker or 'ALL'}, "
            f"keywords={','.join(dashboard.applied_keywords) or 'ALL'})"
        )
        _print_dataframe("Event Study Summary", dashboard.event_study.summary, max_rows=10)
        _print_dataframe("Sector Spillover", dashboard.sector_spillover.summary, max_rows=10)
        _print_dataframe("Divergence Alerts", dashboard.divergence.alerts, max_rows=10)
        _print_dataframe("Expectation Reset", dashboard.expectation_reset.candidates, max_rows=10)
        _print_dataframe("Volatility Regime", dashboard.volatility_regime.summary, max_rows=10)
        _print_dataframe("Topic Model", dashboard.topics.topics, max_rows=10)
    except Exception as exc:
        print(f"Stock news analysis failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
