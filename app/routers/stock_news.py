from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.form import read_form
from app.services import news_service

router = APIRouter(prefix="/stock-news")


@router.get("/{page}", response_class=HTMLResponse)
def news_page(page: str) -> HTMLResponse:
    return HTMLResponse(news_service.render(page))


@router.post("/run-{page}")
async def run_news(page: str, request: Request) -> RedirectResponse:
    form = await read_form(request)
    route_page = {
        "overview": "overview",
        "event-study": "event-study",
        "sector-spillover": "sector-spillover",
        "divergence": "divergence",
        "expectation-reset": "expectation-reset",
        "volatility-regime": "volatility-regime",
        "topic-modeling": "topic-modeling",
    }.get(page, "overview")
    news_service.run(route_page, form)
    return RedirectResponse(f"/stock-news/{route_page}", status_code=303)
