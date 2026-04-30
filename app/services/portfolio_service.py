from __future__ import annotations

import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from pipeline_portfolio import web_gui as portfolio_web
from pipeline_portfolio.analysis import (
    DEFAULT_CASH_BUFFER_PCT,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MAX_POSITION_PCT,
    DEFAULT_OPTIMIZATION_UNIVERSE_SIZE,
    DEFAULT_SECTOR_CAP_PCT,
    add_trade,
    analyze_virtual_trade,
    build_portfolio_dashboard,
    build_portfolio_optimization,
    delete_trade,
)

from app.services.dataframe import frame_records


DEFAULT_START_DATE = "2025-12-31"


@dataclass
class PortfolioRange:
    lookback_days: int
    start_date: str
    end_date: str


def resolve_range(
    start_date: str | None = None,
    end_date: str | None = None,
    lookback_days: int | None = None,
) -> PortfolioRange:
    lookback = max(int(lookback_days or DEFAULT_LOOKBACK_DAYS), 21)
    today = datetime.now().date()
    end = end_date or today.isoformat()
    start = start_date or DEFAULT_START_DATE
    if start > end:
        start = DEFAULT_START_DATE
    return PortfolioRange(lookback_days=lookback, start_date=start, end_date=end)


def dashboard_payload(
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, object]:
    date_range = resolve_range(start_date, end_date, lookback_days)
    dashboard = build_portfolio_dashboard(
        lookback_days=date_range.lookback_days,
        start_date=date_range.start_date,
        end_date=date_range.end_date,
    )
    return {
        "as_of_date": dashboard.as_of_date,
        "range": date_range.__dict__,
        "summary": frame_records(dashboard.portfolio_summary, max_rows=5),
        "positions": frame_records(dashboard.positions, max_rows=100),
        "holdings_performance": frame_records(dashboard.holdings_performance, max_rows=100),
        "attribution": frame_records(dashboard.attribution, max_rows=100),
        "risk_summary": frame_records(dashboard.risk_summary, max_rows=20),
        "scoring": frame_records(dashboard.scoring, max_rows=100),
        "diagnostics": dashboard.diagnostics,
    }


def render_page(
    page: str,
    *,
    run: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    start_date: str | None = None,
    end_date: str | None = None,
    message: str | None = None,
    error: str | None = None,
) -> str:
    date_range = resolve_range(start_date, end_date, lookback_days)
    dashboard = None
    page_error = error
    if run:
        try:
            dashboard = build_portfolio_dashboard(
                lookback_days=date_range.lookback_days,
                start_date=date_range.start_date,
                end_date=date_range.end_date,
            )
        except Exception as exc:
            page_error = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc(limit=3)}"
    ctx = portfolio_web._PageContext(
        dashboard=dashboard,
        lookback_days=date_range.lookback_days,
        start_date=date_range.start_date,
        end_date=date_range.end_date,
        message=message,
        error=page_error,
    )
    renderers: dict[str, Callable[[portfolio_web._PageContext], str]] = {
        "data-entry": portfolio_web._data_entry_page,
        "overview": portfolio_web._overview_page,
        "attribution": portfolio_web._attribution_page,
        "risk": portfolio_web._risk_page,
        "scoring": portfolio_web._scoring_page,
        "virtual-trade": portfolio_web._virtual_trade_page,
    }
    if page == "optimization":
        optimization = None
        if run and page_error is None:
            try:
                optimization = build_portfolio_optimization(lookback_days=date_range.lookback_days)
            except Exception as exc:
                page_error = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc(limit=3)}"
        ctx.error = page_error
        ctx.optimization = optimization
        ctx.optimization_params = {
            "universe_size": DEFAULT_OPTIMIZATION_UNIVERSE_SIZE,
            "sector_cap_pct": DEFAULT_SECTOR_CAP_PCT,
            "max_position_pct": DEFAULT_MAX_POSITION_PCT,
            "cash_buffer_pct": DEFAULT_CASH_BUFFER_PCT,
        }
        return portfolio_web._optimization_page(ctx)
    return renderers.get(page, portfolio_web._overview_page)(ctx)


def create_trade(form: dict[str, str]) -> None:
    add_trade(
        trade_date=form.get("trade_date", ""),
        ticker=form.get("ticker", ""),
        side=form.get("side", ""),
        quantity=float(form.get("quantity", "0") or 0),
        price=float(form.get("price", "0") or 0),
        fees=float(form.get("fees", "0") or 0),
        notes=form.get("notes", ""),
    )


def remove_trade(trade_id: int) -> None:
    delete_trade(int(trade_id))


def virtual_trade_payload(form: dict[str, str]) -> dict[str, object]:
    result = analyze_virtual_trade(
        ticker=form.get("ticker", ""),
        side=form.get("side", ""),
        quantity=float(form.get("quantity", "0") or 0),
        price=float(form["price"]) if str(form.get("price", "")).strip() else None,
        fees=float(form.get("fees", "0") or 0),
        lookback_days=int(form.get("lookback_days", DEFAULT_LOOKBACK_DAYS) or DEFAULT_LOOKBACK_DAYS),
        forecast_horizon_days=int(form.get("forecast_horizon_days", "10") or 10),
    )
    return {
        "input_summary": frame_records(result.input_summary),
        "before_summary": frame_records(result.before_summary),
        "after_summary": frame_records(result.after_summary),
        "position_changes": frame_records(result.position_changes, max_rows=100),
        "risk_changes": frame_records(result.risk_changes),
        "diagnostics": result.diagnostics,
    }


def render_virtual_trade(form: dict[str, str]) -> str:
    date_range = resolve_range(
        form.get("start_date"),
        form.get("end_date"),
        int(form.get("lookback_days", DEFAULT_LOOKBACK_DAYS) or DEFAULT_LOOKBACK_DAYS),
    )
    dashboard = None
    dashboard_error = None
    try:
        dashboard = build_portfolio_dashboard(
            lookback_days=date_range.lookback_days,
            start_date=date_range.start_date,
            end_date=date_range.end_date,
        )
    except Exception as exc:
        dashboard_error = f"{type(exc).__name__}: {exc}"
    result = analyze_virtual_trade(
        ticker=form.get("ticker", ""),
        side=form.get("side", ""),
        quantity=float(form.get("quantity", "0") or 0),
        price=float(form["price"]) if str(form.get("price", "")).strip() else None,
        fees=float(form.get("fees", "0") or 0),
        lookback_days=date_range.lookback_days,
        forecast_horizon_days=int(form.get("forecast_horizon_days", "10") or 10),
    )
    return portfolio_web._virtual_trade_page(
        portfolio_web._PageContext(
            dashboard=dashboard,
            lookback_days=date_range.lookback_days,
            start_date=date_range.start_date,
            end_date=date_range.end_date,
            message="가상 거래 계산이 완료되었습니다.",
            error=dashboard_error,
            virtual_result=result,
        )
    )
