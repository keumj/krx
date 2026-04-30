from __future__ import annotations

from fastapi import FastAPI

from app.routers import pages, portfolio, refresh, stock, stock_news
from app.settings import settings


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Portfolio-first single-port service with stock and stock-news submodules.",
)

app.include_router(pages.router)
app.include_router(stock.router)
app.include_router(stock_news.router)
app.include_router(refresh.router)
app.include_router(portfolio.router)

