from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.form import read_form
from app.services import stock_service

router = APIRouter(prefix="/stock")


@router.get("/{page}", response_class=HTMLResponse)
def stock_page(page: str, ticker: str | None = None, intent: str | None = None) -> HTMLResponse:
    return HTMLResponse(stock_service.render(page, ticker=ticker, intent=intent))


@router.post("/run")
async def run_forecast(request: Request) -> RedirectResponse:
    form = await read_form(request)
    page = stock_service.run("forecast", form)
    return RedirectResponse(f"/stock/{page}", status_code=303)


@router.post("/run-financial")
async def run_financial(request: Request) -> RedirectResponse:
    form = await read_form(request)
    page = stock_service.run("financials", form)
    return RedirectResponse(f"/stock/{page}", status_code=303)


@router.post("/run-technical")
async def run_technical(request: Request) -> RedirectResponse:
    form = await read_form(request)
    page = stock_service.run("technical", form)
    return RedirectResponse(f"/stock/{page}", status_code=303)


@router.post("/run-returns")
async def run_returns(request: Request) -> RedirectResponse:
    form = await read_form(request)
    page = stock_service.run("returns", form)
    return RedirectResponse(f"/stock/{page}", status_code=303)


@router.post("/run-risk")
async def run_risk(request: Request) -> RedirectResponse:
    form = await read_form(request)
    page = stock_service.run("risk", form)
    return RedirectResponse(f"/stock/{page}", status_code=303)


@router.post("/run-factor")
async def run_factor(request: Request) -> RedirectResponse:
    form = await read_form(request)
    page = stock_service.run("factor", form)
    return RedirectResponse(f"/stock/{page}", status_code=303)


@router.post("/run-decision")
async def run_decision(request: Request) -> RedirectResponse:
    form = await read_form(request)
    page = stock_service.run("decision", form)
    return RedirectResponse(f"/stock/{page}", status_code=303)


@router.post("/run-walk-forward")
async def run_walk_forward(request: Request) -> RedirectResponse:
    form = await read_form(request)
    page = stock_service.run("walk-forward", form)
    return RedirectResponse(f"/stock/{page}", status_code=303)
