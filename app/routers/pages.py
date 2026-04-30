from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.web import shell

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    body = """
    <div class="service-stack">
      <div class="service-card">
        <h1>Portfolio-first Single Port Service</h1>
        <p class="service-muted">포트폴리오를 기본 앱으로 두고, 종목 분석과 뉴스 분석을 하위 모듈로 붙인 웹서비스입니다.</p>
      </div>
      <div class="service-grid">
        <a class="service-card" href="/portfolio/overview?intent=run">
          <h3>Portfolio</h3>
          <p>보유 종목, 성과, 리스크, 최적화 분석을 실행합니다.</p>
        </a>
        <a class="service-card" href="/stock/forecast">
          <h3>Stock</h3>
          <p>개별 종목 예측, 재무, 기술적 분석, 의사결정 화면으로 이동합니다.</p>
        </a>
        <a class="service-card" href="/stock-news/overview">
          <h3>Stock News</h3>
          <p>뉴스 기반 이벤트, 섹터 전이, 토픽, 가격 반응 분석을 실행합니다.</p>
        </a>
      </div>
    </div>
    """
    return HTMLResponse(shell("Keumjm Portfolio Lab", body))


@router.get("/healthz")
def healthz() -> dict[str, object]:
    return {"ok": True, "service": "keumjm-single-port"}


@router.get("/external_command_state")
def external_command_state() -> dict[str, object]:
    return {"command_id": 0, "navigate_url": None}


def _redirect_with_query(request: Request, path: str) -> RedirectResponse:
    query = str(request.url.query)
    target = f"{path}?{query}" if query else path
    return RedirectResponse(target)


@router.get("/overview")
def legacy_overview(request: Request) -> RedirectResponse:
    return _redirect_with_query(request, "/portfolio/overview")


@router.get("/portfolio")
def portfolio_home(request: Request) -> RedirectResponse:
    return _redirect_with_query(request, "/portfolio/overview")


@router.get("/stock")
def stock_home(request: Request) -> RedirectResponse:
    return _redirect_with_query(request, "/stock/forecast")


@router.get("/stock-news")
def stock_news_home(request: Request) -> RedirectResponse:
    return _redirect_with_query(request, "/stock-news/overview")


@router.get("/stock-forecast")
def legacy_stock_forecast(request: Request) -> RedirectResponse:
    return _redirect_with_query(request, "/stock/forecast")


@router.get("/stock-financials")
def legacy_stock_financials(request: Request) -> RedirectResponse:
    return _redirect_with_query(request, "/stock/financials")


@router.get("/stock-technical")
def legacy_stock_technical(request: Request) -> RedirectResponse:
    return _redirect_with_query(request, "/stock/technical")


@router.get("/stock-wfv")
def legacy_stock_walk_forward(request: Request) -> RedirectResponse:
    return _redirect_with_query(request, "/stock/walk-forward")
