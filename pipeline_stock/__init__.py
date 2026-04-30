"""Unified stock package for forecast and technical analysis."""

from .forecast import (
    StockForecastResult,
    fetch_close_prices,
    load_price_data_csv,
    run_stock_forecast_pipeline,
    run_ticker_stock_forecast_pipeline,
)
from .technical_analysis import launch_web_gui as launch_technical_web_gui, run_web_gui as run_technical_web_gui
try:
    from .web_gui import launch_web_gui, launch_stock_forecast_web_gui, run_web_gui
except Exception:
    def launch_web_gui(*args, **kwargs):
        raise RuntimeError("pipeline_stock.web_gui could not be imported. Use app.main single-port service instead.")

    def launch_stock_forecast_web_gui(*args, **kwargs):
        return launch_web_gui(*args, **kwargs)

    def run_web_gui(*args, **kwargs):
        return launch_web_gui(*args, **kwargs)

__all__ = [
    "StockForecastResult",
    "fetch_close_prices",
    "load_price_data_csv",
    "run_ticker_stock_forecast_pipeline",
    "run_stock_forecast_pipeline",
    "launch_web_gui",
    "launch_stock_forecast_web_gui",
    "run_web_gui",
    "launch_technical_web_gui",
    "run_technical_web_gui",
]
