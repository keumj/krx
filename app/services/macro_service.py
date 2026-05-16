from __future__ import annotations

from dataclasses import dataclass
import threading
import traceback

from pipeline_krx_macro import web_gui as macro_web

from app.services.result_cache import load_pickle, save_pickle
from app.web import shell


@dataclass
class _LastMacroResult:
    dashboard: object | None = None
    start_date: str | None = None
    lookback_days: int = 504


_last_result = load_pickle("macro_last_result.pkl", _LastMacroResult())
if not isinstance(_last_result, _LastMacroResult):
    _last_result = _LastMacroResult()
_last_result_lock = threading.RLock()


def _remember_dashboard(dashboard: object, *, start_date: str | None, lookback_days: int) -> None:
    with _last_result_lock:
        _last_result.dashboard = dashboard
        _last_result.start_date = start_date
        _last_result.lookback_days = lookback_days
        save_pickle("macro_last_result.pkl", _last_result)


def _load_dashboard() -> _LastMacroResult:
    with _last_result_lock:
        return _LastMacroResult(
            dashboard=_last_result.dashboard,
            start_date=_last_result.start_date,
            lookback_days=_last_result.lookback_days,
        )


def render(page: str, *, start_date: str | None = None, lookback_days: int = 504) -> str:
    page_key = macro_web.normalize_page(page)
    try:
        cached = _load_dashboard()
        if cached.dashboard is not None and start_date is None and lookback_days == cached.lookback_days:
            body = macro_web.render_body(page_key, dashboard=cached.dashboard)
        else:
            dashboard = macro_web.build_macro_dashboard(start_date=start_date, lookback_days=lookback_days)
            _remember_dashboard(dashboard, start_date=start_date, lookback_days=lookback_days)
            body = macro_web.render_body(page_key, dashboard=dashboard)
    except Exception as exc:
        cached = _load_dashboard()
        if cached.dashboard is not None:
            body = macro_web.render_body(page_key, dashboard=cached.dashboard)
            body = """
            <div class="service-card">
              <p class="service-muted">새 매크로 계산에 실패해 이전 실행 결과를 표시합니다.</p>
            </div>
            """ + body
        else:
            body = f"""
            <div class="service-card">
              <h1>거시 분석</h1>
              <p class="service-error">{type(exc).__name__}: {exc}</p>
              <pre class="service-error">{traceback.format_exc(limit=4)}</pre>
            </div>
            """
    return shell("거시 분석 | Keumj KRX Lab", body, active="macro")
