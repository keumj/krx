from __future__ import annotations

import argparse

from .db import main as init_db_main
from .shell_web_gui import launch_web_gui


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KRX project entrypoint")
    parser.add_argument("--web-gui", action="store_true", help="Launch the unified KRX web GUI")
    parser.add_argument("--host", default="localhost", help="Web GUI host")
    parser.add_argument("--port", type=int, default=8518, help="Web GUI port")
    parser.add_argument("--open-browser", action="store_true", help="Open browser automatically")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.web_gui:
        launch_web_gui(host=str(args.host), port=int(args.port), open_browser=bool(args.open_browser))
        return 0
    return int(init_db_main())


if __name__ == "__main__":
    raise SystemExit(main())
