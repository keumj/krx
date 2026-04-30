from __future__ import annotations

import re
import traceback
from dataclasses import dataclass, field
from datetime import datetime

from bs4 import BeautifulSoup

from pipeline_stock import web_gui as stock_web

from app.web import rewrite_links


STOCK_REWRITES = {
    'href="/forecast"': 'href="/stock/forecast"',
    'href="/page2"': 'href="/stock/financials"',
    'href="/page3"': 'href="/stock/technical"',
    'href="/page4"': 'href="/stock/returns"',
    'href="/page5"': 'href="/stock/risk"',
    'href="/factor-regime"': 'href="/stock/factor-regime"',
    'href="/page6"': 'href="/stock/decision"',
    'href="/page8"': 'href="/stock/walk-forward"',
    'action="/run"': 'action="/stock/run"',
    'action="/run_financial"': 'action="/stock/run-financial"',
    'action="/run_technical"': 'action="/stock/run-technical"',
    'action="/run_returns"': 'action="/stock/run-returns"',
    'action="/run_risk"': 'action="/stock/run-risk"',
    'action="/run_factor"': 'action="/stock/run-factor"',
    'action="/run_decision"': 'action="/stock/run-decision"',
    'action="/run_walk_forward"': 'action="/stock/run-walk-forward"',
}


@dataclass
class StockState:
    forecast_form: dict[str, str] = field(default_factory=lambda: {
        "ticker": "AAPL",
        "forecast_horizon": "10",
        "history_years": "8",
        "start_date": "2025-12-31",
        "end_date": datetime.utcnow().strftime("%Y-%m-%d"),
        "output_dir": "outputs/stock_forecast",
        "prices_csv_path": "",
        "use_sample": "",
        "auto_save": "on",
        "insecure_ssl": "",
        "ca_bundle_path": "",
    })
    forecast_ctx: object | None = None
    forecast_error: str | None = None
    financials_form: dict[str, str] = field(default_factory=lambda: {"ticker": "AAPL", "statement_periods": "4", "output_dir": "outputs/stock_forecast_finance", "auto_save": "on", "insecure_ssl": "", "ca_bundle_path": "", "fmp_api_key": ""})
    financials_ctx: object | None = None
    financials_error: str | None = None
    technical_form: dict[str, str] = field(default_factory=lambda: {"ticker": "AAPL", "output_dir": "outputs/technical_analysis", "use_sample": "", "auto_save": "on", "action": "all"})
    technical_ctx: object | None = None
    technical_error: str | None = None
    technical_cache: object | None = None
    returns_form: dict[str, str] = field(default_factory=lambda: {"ticker": "AAPL"})
    returns_ctx: object | None = None
    returns_error: str | None = None
    risk_form: dict[str, str] = field(default_factory=lambda: {"ticker": "AAPL"})
    risk_ctx: object | None = None
    risk_error: str | None = None
    factor_form: dict[str, str] = field(default_factory=lambda: {"ticker": "AAPL"})
    factor_ctx: object | None = None
    factor_error: str | None = None
    decision_form: dict[str, str] = field(default_factory=lambda: {"ticker": "AAPL"})
    decision_ctx: object | None = None
    decision_error: str | None = None
    wfv_form: dict[str, str] = field(default_factory=lambda: {"ticker": "AAPL", "forecast_horizon": "10", "history_years": "8", "start_date": "2025-12-31", "end_date": datetime.utcnow().strftime("%Y-%m-%d"), "wf_min_train_rows": "252", "wf_step_size": "21", "wf_max_splits": "4", "output_dir": "outputs/walk_forward_validation", "prices_csv_path": "", "use_sample": "", "auto_save": "on", "insecure_ssl": "", "ca_bundle_path": ""})
    wfv_ctx: object | None = None
    wfv_error: str | None = None


state = StockState()


PAGE_SUBTITLES = {
    "forecast": "Run price forecast analysis for the selected S&P 500 ticker.",
    "financials": "Review financial statements, valuation metrics, and provider status.",
    "technical": "Run moving average, candlestick, RSI, and MACD technical analysis.",
    "returns": "Compare ticker returns against its sector and the S&P 500 universe.",
    "risk": "Measure volatility, drawdown, beta, VaR, and relative risk ranks.",
    "factor-regime": "Decompose ticker movement into market, sector, and residual factors.",
    "decision": "Combine return, risk, factor, and trend signals into one decision dashboard.",
    "walk-forward": "Validate forecast quality through repeated historical walk-forward tests.",
}

PAGE_RUN_LABELS = {
    "forecast": "Run Forecast",
    "financials": "Run Financials",
    "returns": "Run Return Comparison",
    "risk": "Run Risk Dashboard",
    "factor-regime": "Run Factor Regime",
    "decision": "Run Decision Dashboard",
    "walk-forward": "Run Walk-Forward Validation",
}

FIELD_LABELS = {
    "ticker": "Ticker",
    "forecast_horizon": "Forecast Horizon",
    "history_years": "History Years",
    "start_date": "Start Date",
    "end_date": "End Date",
    "output_dir": "Output Folder",
    "prices_csv_path": "Local Prices CSV",
    "ca_bundle_path": "CA Bundle Path",
    "statement_periods": "Statement Periods",
    "fmp_api_key": "FMP API Key",
    "wf_min_train_rows": "Min Training Rows",
    "wf_step_size": "Split Step Size",
    "wf_max_splits": "Max Splits",
}

CHECKBOX_LABELS = {
    "use_sample": "Use sample prices (offline)",
    "auto_save": "Auto-save results",
    "insecure_ssl": "Temporarily disable SSL verification",
}

ACTION_BUTTON_LABELS = {
    "ma": "Moving Average",
    "candle": "Candlestick",
    "rsi": "RSI",
    "macd": "MACD",
    "all": "Run All",
}

PAGE_NOTICE = {
    "forecast": "Enter a ticker and run the forecast. Use sample data if you need an offline check.",
    "financials": "Enter a ticker and run financial analysis. If yfinance is unavailable, SEC/FMP/shared-data fallbacks are used.",
    "technical": "Run technical charts from the latest OHLCV data. Sample data is available for offline preview.",
    "returns": "Compare the selected ticker with its sector and the S&P 500 universe.",
    "risk": "Run risk analysis for volatility, drawdown, beta, and tail-risk metrics.",
    "factor-regime": "Run factor and regime analysis to separate market, sector, and ticker-specific movement.",
    "decision": "Run the decision dashboard after selecting a ticker.",
    "walk-forward": "Run walk-forward validation. For a short date range, the service widens the training window automatically.",
}

PAGE_H3_LABELS = {
    "forecast": ["Price Forecast", "Model Weights", "Data Source Metadata", "Forecast Summary", "Model Scores", "Direction Scores", "Regime Snapshot", "Feature Importance"],
    "financials": ["Data Source Metadata", "Provider Status", "Financial Metrics", "Latest Financial Summary", "Income Statement", "Balance Sheet", "Cash Flow Statement"],
    "technical": ["Data Source Metadata", "Run Summary"],
    "returns": ["YTD Base 100 Index", "Recent Daily Return Comparison", "Period Return Comparison", "Recent Ticker Daily Returns", "Recent Sector Daily Returns", "Data Source Metadata", "Sector YTD Top 10", "Sector YTD Bottom 10", "S&P 500 YTD Top 10", "S&P 500 YTD Bottom 10"],
    "risk": ["Risk Commentary", "1Y Drawdown Comparison", "20D Rolling Annualized Volatility", "Volatility and Drawdown Summary", "Recent Shock Check", "Data Source Metadata", "Sector Highest 1Y Volatility", "Sector Lowest 1Y Volatility", "S&P 500 Highest 1Y Volatility", "S&P 500 Lowest 1Y Volatility"],
    "factor-regime": ["How To Read Factor Regime", "Rolling 60-Day Beta", "Cumulative Residual Return", "Factor Summary", "Interpretation Guide", "Recent Factor Decomposition", "Data Source Metadata"],
    "decision": ["Final Decision", "Decision Score Breakdown", "Trend and Volatility Context", "Bullish Reasons", "Bearish Reasons", "Watch Items", "Score Table", "Signal Details", "Data Source Metadata"],
    "walk-forward": ["How To Read Walk-Forward Validation", "Predicted vs Realized Forward Return", "Error and Rolling Hit Rate", "Validation Summary", "Interpretation Guide", "No-Trade Threshold Summary", "Regime Summary", "Data Source Metadata", "Model Diagnostics", "Split Results"],
}

PAGE_METRIC_LABELS = {
    "forecast": ["Ticker", "As Of Date", "Forecast Date", "Forecast Horizon", "Last Close", "Predicted Price", "Expected Return", "Up Probability", "Direction Confidence", "Signal", "Trade Filter", "Ensemble Log Return"],
    "financials": ["Ticker", "Company", "Currency", "PER (Trailing)", "PER (Forward)", "PBR", "Market Cap", "ROE"],
    "technical": ["Ticker", "Data Source", "Rows", "Period", "Action", "Lookback Target"],
    "walk-forward": ["Ticker", "Price Source", "Evaluation Splits", "Forecast Horizon", "Direction Hit Rate", "Classification Hit Rate", "Trade Coverage", "Trade Hit Rate", "MAE", "RMSE", "Skill vs Naive", "Bias", "Return Correlation", "Latest As-Of Date", "Latest Realized Date"],
}

MOJIBAKE_MARKERS = ("?곗", "?섏", "?쒖", "?덉", "?뚯", "媛", "醫", "理", "由", "蹂", "寃", "遺", "湲", "嫄")

PAGE_TEXT_FALLBACK = {
    "forecast": "Forecast outputs are calculated from the selected ticker price history and model ensemble.",
    "financials": "Financial outputs combine available provider data, fallback sources, and derived metrics where needed.",
    "technical": "Technical outputs summarize price trend, momentum, and chart diagnostics.",
    "returns": "Return outputs compare the ticker against its sector and the S&P 500 universe.",
    "risk": "Risk outputs summarize volatility, drawdown, beta, and tail-risk behavior from shared price data.",
    "factor-regime": "Factor-regime outputs separate market, sector, and ticker-specific movement and summarize the current regime.",
    "decision": "Decision outputs combine bullish, bearish, risk, and trend signals into a single dashboard view.",
    "walk-forward": "Walk-forward outputs show repeated historical validation results for forecast quality.",
}

PAGE_TITLES = {
    "forecast": "Stock Analysis Lab | Forecast",
    "financials": "Stock Analysis Lab | Financials",
    "technical": "Stock Analysis Lab | Technical",
    "returns": "Stock Analysis Lab | Returns",
    "risk": "Stock Analysis Lab | Risk",
    "factor-regime": "Stock Analysis Lab | Factor Regime",
    "decision": "Stock Analysis Lab | Decision",
    "walk-forward": "Stock Analysis Lab | Walk Forward",
}

REGIME_FALLBACKS = {
    "Trend Regime": "Neutral trend",
    "Volatility Regime": "Normal volatility",
    "Beta Regime": "Market-like beta",
    "Overall Regime": "Mixed regime",
}


def _has_mojibake(text: str) -> bool:
    return any(marker in text for marker in MOJIBAKE_MARKERS)


def _replace_label_text(label, text: str) -> None:
    input_tag = label.find("input")
    if input_tag is not None and input_tag.parent is label:
        input_tag.extract()
        label.clear()
        label.append(input_tag)
        label.append(" " + text)
        return
    label.clear()
    label.append(text)


def _make_stock_text_readable(page: str, html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if soup.title is not None:
        soup.title.string = PAGE_TITLES.get(page, "Stock Analysis Lab")

    sub = soup.select_one(".sub")
    if sub is not None:
        sub.string = PAGE_SUBTITLES.get(page, page)

    for label in soup.find_all("label"):
        nested_input = label.find("input")
        if nested_input is not None:
            name = nested_input.get("name", "")
            if name in CHECKBOX_LABELS:
                _replace_label_text(label, CHECKBOX_LABELS[name])
            continue
        parent = label.parent
        input_tag = parent.find(["input", "select"]) if parent else None
        name = input_tag.get("name", "") if input_tag else ""
        if name in FIELD_LABELS:
            _replace_label_text(label, FIELD_LABELS[name])

    for button in soup.find_all("button"):
        value = button.get("value", "")
        name = button.get("name", "")
        if name == "action" and value in ACTION_BUTTON_LABELS:
            button.string = ACTION_BUTTON_LABELS[value]
        elif value == "resolve_ticker":
            button.string = "Find Ticker by Company Name"
        elif value == "run" or button.get("type") == "submit":
            button.string = PAGE_RUN_LABELS.get(page, "Run Analysis")

    for notice in soup.select(".notice.ok"):
        code = notice.find("code")
        if code is not None and _has_mojibake(notice.get_text(" ", strip=True)):
            code.extract()
            notice.clear()
            notice.append("Results saved to ")
            notice.append(code)
            notice.append(".")
        elif notice.find(["pre", "code", "table"]) is None:
            notice.clear()
            notice.append(PAGE_NOTICE.get(page, "Ready."))

    for span, text in zip(soup.select(".metric span"), PAGE_METRIC_LABELS.get(page, []), strict=False):
        span.string = text

    for metric in soup.select(".metric"):
        span = metric.find("span")
        strong = metric.find("strong")
        if span is not None and strong is not None:
            label = span.get_text(" ", strip=True)
            value = strong.get_text(" ", strip=True)
            if label in REGIME_FALLBACKS and _has_mojibake(value):
                strong.string = REGIME_FALLBACKS[label]

    for h3, text in zip(soup.find_all("h3"), PAGE_H3_LABELS.get(page, []), strict=False):
        h3.string = text

    for h4 in soup.find_all("h4"):
        text = h4.get_text(" ", strip=True)
        if _has_mojibake(text):
            h4.string = "Result Table"

    fallback_text = PAGE_TEXT_FALLBACK.get(page)
    if fallback_text:
        for tag in soup.find_all(["p", "li"]):
            text = tag.get_text(" ", strip=True)
            if _has_mojibake(text):
                tag.clear()
                tag.append(fallback_text)
        for cell in soup.find_all(["th", "td"]):
            text = cell.get_text(" ", strip=True)
            if _has_mojibake(text):
                cell.clear()
                cell.append(fallback_text)

    return str(soup)


def _clean_stock_html(page: str, html: str) -> str:
    html = rewrite_links(html, STOCK_REWRITES)
    html = html.replace("Stock Forecast</a><a", "Stock Forecast</a><a")
    html = re.sub(r"<title>.*?</title>", f"<title>Stock Analysis Lab | {page}</title>", html, count=1, flags=re.S)
    html = re.sub(
        r'<div class="page-head">.*?</div>\s*</div>',
        '<div class="page-head"><h1>Stock Analysis Lab | S&P 500</h1><div class="page-credit">Keumj service</div></div>',
        html,
        count=1,
        flags=re.S,
    )
    html = re.sub(
        r'<div class="sub">.*?</div>',
        f'<div class="sub">{page}</div>',
        html,
        count=1,
        flags=re.S,
    )
    back = '<div class="nav" style="margin-bottom:12px;"><a href="/portfolio/overview?intent=run">Portfolio로 돌아가기</a></div>'
    back = '<div class="nav" style="margin-bottom:12px;"><a href="/portfolio/overview?intent=run">Portfolio로 돌아가기</a></div>'
    html = html.replace('<div class="wrap">', '<div class="wrap">' + back, 1)
    return _make_stock_text_readable(page, html)


def render(page: str) -> str:
    if page == "forecast":
        html = stock_web._html_page(
            state.forecast_form,
            ctx=state.forecast_ctx,
            error=state.forecast_error,
            enable_technical_page=True,
        )
    elif page == "financials":
        html = stock_web._html_financial_page(state.financials_form, ctx=state.financials_ctx, error=state.financials_error, enable_technical_page=True)
    elif page == "technical":
        html = stock_web._html_technical_page(state.technical_form, ctx=state.technical_ctx, error=state.technical_error)
    elif page == "returns":
        html = stock_web._html_returns_page(state.returns_form, ctx=state.returns_ctx, error=state.returns_error)
    elif page == "risk":
        html = stock_web._html_risk_page(state.risk_form, ctx=state.risk_ctx, error=state.risk_error)
    elif page == "factor-regime":
        html = stock_web._html_factor_page(state.factor_form, ctx=state.factor_ctx, error=state.factor_error)
    elif page == "decision":
        html = stock_web._html_decision_page(state.decision_form, ctx=state.decision_ctx, error=state.decision_error)
    elif page == "walk-forward":
        html = stock_web._html_walk_forward_page(state.wfv_form, ctx=state.wfv_ctx, error=state.wfv_error)
    else:
        html = stock_web._html_page(state.forecast_form, ctx=state.forecast_ctx, error=state.forecast_error, enable_technical_page=True)
    return _clean_stock_html(page, html)


def _clean_ticker(value: str) -> str:
    return str(value or "").strip().upper()


def _sync_ticker(ticker: str) -> None:
    if not ticker:
        return
    for form in [state.forecast_form, state.financials_form, state.technical_form, state.returns_form, state.risk_form, state.factor_form, state.decision_form, state.wfv_form]:
        form["ticker"] = ticker


def run(action: str, form: dict[str, str]) -> str:
    try:
        ticker = _clean_ticker(form.get("ticker", ""))
        _sync_ticker(ticker)
        if action == "forecast":
            for checkbox in ["use_sample", "auto_save", "insecure_ssl"]:
                form.setdefault(checkbox, "")
            state.forecast_form = {**state.forecast_form, **form, "ticker": ticker}
            try:
                state.forecast_ctx = stock_web._run_once(state.forecast_form)
            except ValueError as exc:
                if "Not enough history" not in str(exc):
                    raise
                retry_form = {**state.forecast_form, "start_date": "", "end_date": ""}
                state.forecast_ctx = stock_web._run_once(retry_form)
            state.forecast_error = None
            return "forecast"
        if action == "financials":
            state.financials_form = {**state.financials_form, **form, "ticker": ticker}
            state.financials_ctx = stock_web._run_financial_once(state.financials_form)
            state.financials_error = None
            return "financials"
        if action == "technical":
            form["action"] = stock_web._normalize_technical_action(form.get("action", "all"))
            state.technical_form = {**state.technical_form, **form, "ticker": ticker}
            state.technical_ctx, state.technical_cache = stock_web.ta_web_gui._run_analysis(
                form=state.technical_form,
                action=state.technical_form.get("action", "all"),
                cache=state.technical_cache,
            )
            state.technical_error = None
            return "technical"
        if action == "returns":
            state.returns_form = {"ticker": ticker}
            state.returns_ctx = stock_web._run_returns_once(state.returns_form)
            state.returns_error = None
            return "returns"
        if action == "risk":
            state.risk_form = {"ticker": ticker}
            state.risk_ctx = stock_web._run_risk_once(state.risk_form)
            state.risk_error = None
            return "risk"
        if action == "factor":
            state.factor_form = {"ticker": ticker}
            state.factor_ctx = stock_web._run_factor_once(state.factor_form)
            state.factor_error = None
            return "factor-regime"
        if action == "decision":
            state.decision_form = {"ticker": ticker}
            if state.returns_ctx is None or getattr(state.returns_ctx, "ticker", "") != ticker:
                state.returns_ctx = stock_web._run_returns_once({"ticker": ticker})
            if state.risk_ctx is None or getattr(state.risk_ctx, "ticker", "") != ticker:
                state.risk_ctx = stock_web._run_risk_once({"ticker": ticker})
            state.decision_ctx = stock_web._run_decision_once(state.decision_form, returns_ctx=state.returns_ctx, risk_ctx=state.risk_ctx, fin_ctx=None)
            state.decision_error = None
            return "decision"
        if action == "walk-forward":
            state.wfv_form = {**state.wfv_form, **form, "ticker": ticker}
            try:
                state.wfv_ctx = stock_web._run_walk_forward_validation_once(state.wfv_form)
            except ValueError as exc:
                if "Not enough usable rows" not in str(exc):
                    raise
                retry_form = {
                    **state.wfv_form,
                    "start_date": "",
                    "end_date": "",
                    "wf_min_train_rows": "80",
                }
                state.wfv_ctx = stock_web._run_walk_forward_validation_once(retry_form)
            state.wfv_error = None
            return "walk-forward"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc(limit=3)}"
        target = {
            "forecast": ("forecast_error", "forecast"),
            "financials": ("financials_error", "financials"),
            "technical": ("technical_error", "technical"),
            "returns": ("returns_error", "returns"),
            "risk": ("risk_error", "risk"),
            "factor": ("factor_error", "factor-regime"),
            "decision": ("decision_error", "decision"),
            "walk-forward": ("wfv_error", "walk-forward"),
        }.get(action, ("forecast_error", "forecast"))
        setattr(state, target[0], error)
        return target[1]
    return "forecast"
