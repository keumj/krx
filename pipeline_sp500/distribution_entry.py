from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _has_runtime_data(root: Path) -> bool:
    data_dir = root / "data"
    return (
        (data_dir / "sp500_components_full.csv").is_file()
        and (data_dir / "sp500_shared_db" / "sp500_shared_prices.sqlite").is_file()
    )


def app_root_dir() -> Path:
    if getattr(sys, "frozen", False):
        exe_root = Path(sys.executable).resolve().parent
        if _has_runtime_data(exe_root):
            return exe_root
        internal_root = exe_root / "_internal"
        if _has_runtime_data(internal_root):
            return internal_root
        return exe_root
    return Path(__file__).resolve().parents[1]


def _activate_app_root() -> Path:
    root = app_root_dir()
    os.chdir(root)
    return root


def _run_module_main(module_name: str, argv: list[str]) -> int:
    old_argv = sys.argv[:]
    try:
        sys.argv = [old_argv[0], *argv]
        module = __import__(module_name, fromlist=["main"])
        main_func = getattr(module, "main")
        result = main_func()
        return int(result) if result is not None else 0
    finally:
        sys.argv = old_argv


def _run_gui(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Launch the Keumj unified SP500 GUI.")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8516)
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--ca-bundle", default="")
    parser.add_argument("--insecure-ssl", action="store_true")
    parser.add_argument("--require-live-data", action="store_true")
    args = parser.parse_args(argv)

    from pipeline_common.security import configure_ssl
    from pipeline_sp500.web_gui import launch_web_gui

    configure_ssl(
        insecure_ssl=bool(args.insecure_ssl),
        ca_bundle=str(args.ca_bundle).strip() or None,
    )
    if args.require_live_data:
        os.environ["KEUMJ_REQUIRE_LIVE_DATA"] = "1"

    host = str(args.host).strip() or "localhost"
    port = int(args.port)
    print(f"Working directory: {Path.cwd()}", flush=True)
    print(f"Starting SP500 GUI at http://{host}:{port}", flush=True)
    launch_web_gui(host=host, port=port, open_browser=bool(args.open_browser))
    return 0


def _run_backend(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Launch an internal backend used by KeumjSP500Lab.")
    parser.add_argument("backend", choices=("stock", "stock_news", "stock-news", "portfolio"))
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)

    backend_key = str(args.backend).strip().lower().replace("-", "_")
    host = str(args.host).strip() or "localhost"
    default_port = {"stock": 8512, "stock_news": 8514, "portfolio": 8515}[backend_key]
    port = int(args.port) if args.port is not None else default_port

    if backend_key == "stock":
        from pipeline_stock.web_gui import launch_web_gui as launch_backend
    elif backend_key == "stock_news":
        from pipeline_stock_news.web_gui import launch_web_gui as launch_backend
    else:
        from pipeline_portfolio.web_gui import launch_web_gui as launch_backend

    print(f"Starting backend {backend_key} at http://{host}:{port}", flush=True)
    launch_backend(host=host, port=port, open_browser=False)
    return 0


def _print_help() -> None:
    print(
        "\n".join(
            [
                "KeumjSP500Lab",
                "",
                "Commands:",
                "  gui [--host localhost] [--port 8516]     Launch the unified SP500 shell GUI.",
                "  backend <stock|stock_news|portfolio>     Launch an internal backend GUI.",
                "  refresh-stock [options]                  Refresh shared S&P 500 prices and market caps.",
                "  refresh-quarterly [options]              Refresh shared quarterly fundamentals.",
                "  refresh-news [options]                   Refresh shared stock news.",
                "",
                "Examples:",
                "  KeumjSP500Lab.exe gui",
                "  KeumjSP500Lab.exe backend stock",
                "  KeumjSP500Lab.exe refresh-news",
            ]
        )
    )


def main(argv: list[str] | None = None) -> int:
    _activate_app_root()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return _run_gui([])

    command = str(args[0]).strip().lower()
    if command in {"-h", "--help", "help"}:
        _print_help()
        return 0
    if command in {"gui", "run", "web"}:
        return _run_gui(args[1:])
    if command in {"backend"}:
        return _run_backend(args[1:])
    if command in {"stock", "stock_news", "stock-news", "portfolio"}:
        return _run_backend(args)
    if command in {"refresh", "refresh-stock", "refresh_stock_data"}:
        return _run_module_main("pipeline_common.refresh_sp500_shared_prices", args[1:])
    if command in {"refresh-quarterly", "refresh_quarterly", "refresh-quarterly-fundamentals"}:
        return _run_module_main("pipeline_common.refresh_shared_quarterly_fundamentals", args[1:])
    if command in {"refresh-news", "refresh_news"}:
        return _run_module_main("pipeline_common.refresh_sp500_news", args[1:])
    if command.startswith("-"):
        return _run_gui(args)

    print(f"Unknown command: {args[0]}", file=sys.stderr)
    _print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
