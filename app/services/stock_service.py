from __future__ import annotations

import re
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime

from pipeline_krx_stock import web_gui as stock_web

from app.services.result_cache import load_pickle, save_pickle
from app.web import add_service_top_nav, inject_busy_cursor_overlay, rewrite_links


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
    forecast_form: dict[str, str] = field(
        default_factory=lambda: {
            "ticker": "005930",
            "forecast_horizon": "10",
            "history_years": "8",
            "start_date": stock_web.recommended_forecast_start_date(10),
            "end_date": datetime.utcnow().strftime("%Y-%m-%d"),
            "output_dir": "outputs/stock_forecast",
            "prices_csv_path": "",
            "use_sample": "",
            "auto_save": "on",
            "insecure_ssl": "",
            "ca_bundle_path": "",
        }
    )
    forecast_ctx: object | None = None
    forecast_error: str | None = None
    financials_form: dict[str, str] = field(
        default_factory=lambda: {
            "ticker": "005930",
            "statement_periods": "4",
            "output_dir": "outputs/stock_forecast_finance",
            "auto_save": "on",
            "insecure_ssl": "",
            "ca_bundle_path": "",
        }
    )
    financials_ctx: object | None = None
    financials_error: str | None = None
    technical_form: dict[str, str] = field(
        default_factory=lambda: {
            "ticker": "005930",
            "output_dir": "outputs/technical_analysis",
            "use_sample": "",
            "auto_save": "on",
            "action": "all",
        }
    )
    technical_ctx: object | None = None
    technical_error: str | None = None
    technical_cache: object | None = None
    returns_form: dict[str, str] = field(default_factory=lambda: {"ticker": "005930"})
    returns_ctx: object | None = None
    returns_error: str | None = None
    risk_form: dict[str, str] = field(default_factory=lambda: {"ticker": "005930"})
    risk_ctx: object | None = None
    risk_error: str | None = None
    factor_form: dict[str, str] = field(default_factory=lambda: {"ticker": "005930"})
    factor_ctx: object | None = None
    factor_error: str | None = None
    decision_form: dict[str, str] = field(default_factory=lambda: {"ticker": "005930"})
    decision_ctx: object | None = None
    decision_error: str | None = None
    wfv_form: dict[str, str] = field(
        default_factory=lambda: {
            "ticker": "005930",
            "forecast_horizon": "10",
            "history_years": "8",
            "start_date": stock_web.recommended_walk_forward_start_date(10, 252, 21, 4),
            "end_date": datetime.utcnow().strftime("%Y-%m-%d"),
            "wf_min_train_rows": "252",
            "wf_step_size": "21",
            "wf_max_splits": "4",
            "output_dir": "outputs/walk_forward_validation",
            "prices_csv_path": "",
            "use_sample": "",
            "auto_save": "on",
            "insecure_ssl": "",
            "ca_bundle_path": "",
        }
    )
    wfv_ctx: object | None = None
    wfv_error: str | None = None


def _int_form_value(form: dict[str, str], key: str, default: int) -> int:
    try:
        return int(str(form.get(key, default) or default))
    except Exception:
        return default


def _should_replace_start_date(value: object) -> bool:
    return str(value or "").strip() in {"", "2025-12-31"}


def _migrate_recommended_start_dates(state: StockState) -> None:
    if _should_replace_start_date(state.forecast_form.get("start_date")):
        state.forecast_form["start_date"] = stock_web.recommended_forecast_start_date(
            _int_form_value(state.forecast_form, "forecast_horizon", 10)
        )
    if _should_replace_start_date(state.wfv_form.get("start_date")):
        state.wfv_form["start_date"] = stock_web.recommended_walk_forward_start_date(
            _int_form_value(state.wfv_form, "forecast_horizon", 10),
            _int_form_value(state.wfv_form, "wf_min_train_rows", 252),
            _int_form_value(state.wfv_form, "wf_step_size", 21),
            _int_form_value(state.wfv_form, "wf_max_splits", 4),
        )


_states: dict[str, StockState] = {}
_global_state = load_pickle("stock_last_state.pkl", StockState())
if not isinstance(_global_state, StockState):
    _global_state = StockState()
_migrate_recommended_start_dates(_global_state)
_states_lock = threading.RLock()


def _copy_state(source: StockState) -> StockState:
    copied = StockState()
    for attr in (
        "forecast_form",
        "financials_form",
        "technical_form",
        "returns_form",
        "risk_form",
        "factor_form",
        "decision_form",
        "wfv_form",
    ):
        setattr(copied, attr, dict(getattr(source, attr)))
    for attr in (
        "forecast_ctx",
        "forecast_error",
        "financials_ctx",
        "financials_error",
        "technical_ctx",
        "technical_error",
        "technical_cache",
        "returns_ctx",
        "returns_error",
        "risk_ctx",
        "risk_error",
        "factor_ctx",
        "factor_error",
        "decision_ctx",
        "decision_error",
        "wfv_ctx",
        "wfv_error",
    ):
        setattr(copied, attr, getattr(source, attr))
    _migrate_recommended_start_dates(copied)
    return copied


def _state(session_key: str) -> StockState:
    with _states_lock:
        if session_key not in _states:
            _states[session_key] = _copy_state(_global_state)
        return _states[session_key]


def _remember_state(state: StockState) -> None:
    global _global_state
    with _states_lock:
        _global_state = _copy_state(state)
        save_pickle("stock_last_state.pkl", _global_state)


def _clean_stock_html(html: str) -> str:
    return inject_busy_cursor_overlay(add_service_top_nav(rewrite_links(html, STOCK_REWRITES), active="stock"))


def render(page: str, ticker: str | None = None, intent: str | None = None, *, session_key: str = "global") -> str:
    state = _state(session_key)
    selected_ticker = _clean_ticker(ticker or "")
    if selected_ticker:
        _sync_ticker(state, selected_ticker)

    if page == "forecast":
        html = stock_web._html_page(
            state.forecast_form,
            ctx=state.forecast_ctx,
            error=state.forecast_error,
            enable_technical_page=True,
        )
    elif page == "financials":
        html = stock_web._html_financial_page(
            state.financials_form,
            ctx=state.financials_ctx,
            error=state.financials_error,
            enable_technical_page=True,
        )
    elif page == "technical":
        html = stock_web._html_technical_page(
            state.technical_form,
            ctx=state.technical_ctx,
            error=state.technical_error,
        )
    elif page == "returns":
        html = stock_web._html_returns_page(
            state.returns_form,
            ctx=state.returns_ctx,
            error=state.returns_error,
        )
    elif page == "risk":
        html = stock_web._html_risk_page(
            state.risk_form,
            ctx=state.risk_ctx,
            error=state.risk_error,
        )
    elif page == "factor-regime":
        html = stock_web._html_factor_page(
            state.factor_form,
            ctx=state.factor_ctx,
            error=state.factor_error,
        )
    elif page == "decision":
        html = stock_web._html_decision_page(
            state.decision_form,
            ctx=state.decision_ctx,
            error=state.decision_error,
        )
    elif page == "walk-forward":
        html = stock_web._html_walk_forward_page(
            state.wfv_form,
            ctx=state.wfv_ctx,
            error=state.wfv_error,
        )
    else:
        html = stock_web._html_page(
            state.forecast_form,
            ctx=state.forecast_ctx,
            error=state.forecast_error,
            enable_technical_page=True,
        )
    return _clean_stock_html(html)


def _clean_ticker(value: str) -> str:
    raw = str(value or "").strip().upper()
    raw = re.split(r"[?&#\s]", raw, maxsplit=1)[0]
    return re.sub(r"[^A-Z0-9.\-]", "", raw)


def _sync_ticker(state: StockState, ticker: str) -> None:
    if not ticker:
        return
    for form in [
        state.forecast_form,
        state.financials_form,
        state.technical_form,
        state.returns_form,
        state.risk_form,
        state.factor_form,
        state.decision_form,
        state.wfv_form,
    ]:
        form["ticker"] = ticker


def _matching_financials_ctx(state: StockState, ticker: str) -> object | None:
    fin_ctx = state.financials_ctx
    if fin_ctx is None:
        return None
    fin_ticker = str(getattr(fin_ctx, "ticker", "")).strip().upper()
    return fin_ctx if fin_ticker == ticker else None


def run(action: str, form: dict[str, str], *, session_key: str = "global") -> str:
    state = _state(session_key)
    try:
        ticker = _clean_ticker(form.get("ticker", ""))
        _sync_ticker(state, ticker)
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
            _remember_state(state)
            return "forecast"
        if action == "financials":
            state.financials_form = {**state.financials_form, **form, "ticker": ticker}
            state.financials_ctx = stock_web._run_financial_once(state.financials_form)
            state.financials_error = None
            _remember_state(state)
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
            _remember_state(state)
            return "technical"
        if action == "returns":
            state.returns_form = {"ticker": ticker}
            state.returns_ctx = stock_web._run_returns_once(state.returns_form)
            state.returns_error = None
            _remember_state(state)
            return "returns"
        if action == "risk":
            state.risk_form = {"ticker": ticker}
            state.risk_ctx = stock_web._run_risk_once(state.risk_form)
            state.risk_error = None
            _remember_state(state)
            return "risk"
        if action == "factor":
            state.factor_form = {"ticker": ticker}
            state.factor_ctx = stock_web._run_factor_once(state.factor_form)
            state.factor_error = None
            _remember_state(state)
            return "factor-regime"
        if action == "decision":
            state.decision_form = {"ticker": ticker}
            if state.returns_ctx is None or getattr(state.returns_ctx, "ticker", "") != ticker:
                state.returns_ctx = stock_web._run_returns_once({"ticker": ticker})
            if state.risk_ctx is None or getattr(state.risk_ctx, "ticker", "") != ticker:
                state.risk_ctx = stock_web._run_risk_once({"ticker": ticker})
            state.decision_ctx = stock_web._run_decision_once(
                state.decision_form,
                returns_ctx=state.returns_ctx,
                risk_ctx=state.risk_ctx,
                fin_ctx=_matching_financials_ctx(state, ticker),
            )
            state.decision_error = None
            _remember_state(state)
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
            _remember_state(state)
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
