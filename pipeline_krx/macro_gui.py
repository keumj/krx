from __future__ import annotations

import argparse


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KRX macro analysis GUI backend")
    parser.add_argument("--web-gui", action="store_true", help="Accepted for shell compatibility")
    parser.add_argument("--host", default="localhost", help="Host for the KRX macro GUI")
    parser.add_argument("--port", type=int, default=8526, help="Port for the KRX macro GUI")
    parser.add_argument("--open-browser", action="store_true", help="Open browser automatically")
    return parser.parse_args()


def main() -> None:
    from pipeline_krx_macro.web_gui import launch_web_gui

    args = parse_args()
    launch_web_gui(host=str(args.host), port=int(args.port), open_browser=bool(args.open_browser))


if __name__ == "__main__":
    main()
