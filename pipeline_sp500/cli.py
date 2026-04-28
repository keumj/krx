from __future__ import annotations

import argparse

from .web_gui import launch_web_gui


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unified SP500 GUI shell for stock, stock-news, and portfolio pipelines.")
    parser.add_argument("--web-gui", action="store_true", help="Launch the unified SP500 web GUI")
    parser.add_argument("--open-browser", action="store_true", help="Open the unified SP500 web GUI in the default browser")
    parser.add_argument("--host", default="localhost", help="Host for --web-gui")
    parser.add_argument("--port", type=int, default=8516, help="Port for --web-gui")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    launch_web_gui(host=args.host, port=args.port, open_browser=args.open_browser)
