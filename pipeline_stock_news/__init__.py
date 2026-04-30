"""Independent stock news analytics over the shared price/news SQLite DB."""

from .analysis import (
    DivergenceResult,
    EventStudyResult,
    ExpectationResetResult,
    NewsOverviewResult,
    SectorSpilloverResult,
    StockNewsDashboard,
    TopicModelResult,
    VolatilityRegimeResult,
    build_news_overview,
    build_stock_news_dashboard,
    heuristic_title_sentiment,
    recommended_capabilities,
    run_divergence_scan,
    run_event_study,
    run_expectation_reset_tracker,
    run_sector_spillover_monitor,
    run_topic_model,
    run_volatility_regime_after_news,
)
try:
    from .web_gui import launch_web_gui, run_web_gui
except ImportError:
    def launch_web_gui(*args, **kwargs):
        from pipeline_portfolio.web_gui import launch_web_gui as _launch_web_gui

        return _launch_web_gui(*args, **kwargs)

    def run_web_gui(*args, **kwargs):
        return launch_web_gui(*args, **kwargs)

__all__ = [
    "DivergenceResult",
    "EventStudyResult",
    "ExpectationResetResult",
    "NewsOverviewResult",
    "SectorSpilloverResult",
    "StockNewsDashboard",
    "TopicModelResult",
    "VolatilityRegimeResult",
    "build_news_overview",
    "build_stock_news_dashboard",
    "heuristic_title_sentiment",
    "launch_web_gui",
    "recommended_capabilities",
    "run_divergence_scan",
    "run_event_study",
    "run_expectation_reset_tracker",
    "run_sector_spillover_monitor",
    "run_topic_model",
    "run_web_gui",
    "run_volatility_regime_after_news",
]
